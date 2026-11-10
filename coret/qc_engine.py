"""Host QC adapter: bounded-lookahead preview; only complete validated data pass.

No serial I/O or automatic parameter fitting. Future support is withheld from
live decisions. A frozen-prefix consistency check fails closed if an earlier
committed detection changes. Physical contact and jam cause are not inferred.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy import ndimage
from .qc_algorithm.coret_offline import Config as CaptureConfig, validate_capture
from .qc_algorithm.coret_qc_v2 import FEATURE_KEYS, MV_PER_CODE, check_profile, detect
from .qc_algorithm.coret_qc_v3 import VERSION, ContinuousConfig, assess_continuous

RESOURCES = Path(__file__).resolve().parent/'qc_algorithm'
LOOKAHEAD_S = .600
UPDATE_S = .500
MAX_SAMPLES = 3000000


def load_configuration():
    raw=(RESOURCES/'profile.json').read_bytes()
    profile=json.loads(raw);check_profile(profile)
    config=ContinuousConfig(**json.loads((RESOURCES/'continuous.json').read_text(encoding='utf-8')))
    return profile,config,hashlib.sha256(raw).hexdigest()


def feature_arrays(lo,hi,p):
    speed=hi-lo
    baseline=ndimage.median_filter(speed,size=p.local_noise_bins,mode='nearest')
    deviation=ndimage.median_filter(abs(speed-baseline),size=p.local_noise_bins,mode='nearest')
    noise=np.maximum(deviation,p.noise_mad_floor_mv)
    return {'integrity':{'eligible_for_profile':True},'lo_mv':lo,'hi_mv':hi,
            'speed_mv':speed,'baseline_mv':baseline,'noise_scale_mv':noise,
            'score':(speed-baseline)/noise,
            'feature_policy':{k:getattr(p,k) for k in FEATURE_KEYS}}


def metadata_problem(meta,p):
    if meta.get('mode')=='simulator':return '演示数据不能作为核心件质检结果'
    if meta.get('sample_rate')!=100000:return '质检仅支持 100 kS/s 采样'
    if type(meta.get('range')) is not int or meta['range'] not in MV_PER_CODE:return '不支持的输入量程，无法换算质检幅度'
    if meta.get('valve_hz')!=p.target_hz:return '当前参考配置仅支持 4 Hz'
    return None


class QCEngine:
    def __init__(self):
        self.profile,self.config,self.profile_hash=load_configuration()
        self.p,_,_=check_profile(self.profile)
        self.reset()

    def reset(self):
        self.count=0;self.last_update=0.;self.carry=np.array([],dtype=np.uint16)
        self.lo=[];self.hi=[];self.committed=[];self.result=None
        self.fault=None;self.finalized=False;self.frozen_trial=None

    def _wrap(self,r,final=False):
        return dict(r,final=final,algorithm_version=VERSION,profile_sha256=self.profile_hash,
                    required_continuous_s=self.config.required_continuous_s,
                    preview_lookahead_s=LOOKAHEAD_S,physical_contact_verified=False,jam_confirmed=False)

    def error(self,message,final=False):
        self.fault=message
        self.result=self._wrap({'result':'INVALID_DATA','reason':'QC_INPUT_INVALID','message':message},final)
        return self.result

    @staticmethod
    def signature(events):
        return [(e['time_s'],e['p2p_mv'],e['eligible'],e['window_complete']) for e in events]

    def feed(self,meta,delta):
        if self.finalized:return None
        reason=metadata_problem(meta,self.p)
        if reason:
            return self._wrap({'result':'NOT_SUPPORTED','reason':'UNSUPPORTED_CAPTURE','message':reason})
        if self.fault:return self.error(self.fault)
        if meta.get('phase')=='error' or meta.get('errors') or any(meta.get(k,0) for k in ('crc_errors','bad_headers','index_gaps','garbage_bytes')):
            return self.error('通信或采集完整性异常，不能判合格')
        offset,indices,values=delta
        raw=np.asarray(values,dtype=np.uint16);idx=np.asarray(indices,dtype=np.int64)
        if offset!=self.count or len(raw)!=len(idx) or len(raw)+self.count>MAX_SAMPLES:
            return self.error('质检输入批次不连续或超出上限')
        if len(idx) and (idx[0]!=self.count or np.any(np.diff(idx)!=1)):
            return self.error('样本序号不连续')
        if np.any((raw==0)|(raw==65535)):
            return self.error('检测到 ADC 削顶，数据无效')
        self.count+=len(raw)
        data=np.r_[self.carry,raw];n=len(data)//100
        if n:
            blocks=data[:n*100].reshape(-1,100).astype(float)*MV_PER_CODE[meta['range']]
            self.lo.append(blocks.min(1));self.hi.append(blocks.max(1))
        self.carry=data[n*100:].copy()
        duration=self.count/100000
        if duration-self.last_update<UPDATE_S-1e-9:return None
        self.last_update=duration
        if self.frozen_trial is not None:
            return self.result
        watermark=np.floor(duration*1000)/1000-LOOKAHEAD_S
        if watermark<=0 or not self.lo:
            self.result=self._wrap({'result':'PREPARING','message':'请保持接触，等待有效出击确认','continuous_evidence_s':0.})
            return self.result
        f=feature_arrays(np.concatenate(self.lo),np.concatenate(self.hi),self.p)
        # Drop both incomplete edge windows and the entire unfinalized lookahead.
        events=[e for e in detect(f,self.p) if e['time_s']<=watermark+1e-9]
        if self.signature(events[:len(self.committed)])!=self.signature(self.committed):
            return self.error('检测结果的已确认前缀发生变化，请重测；本轮禁止放行')
        self.committed=events
        r=assess_continuous(events,watermark+self.p.event_post_s,self.profile,self.config)
        if r['reason']=='NO_CONFIRMED_OUTPUT':
            r.update(result='PREPARING',message='请保持接触，等待有效出击确认')
        elif r['reason']=='CAPTURE_ENDED_BEFORE_COMPLETION':
            elapsed=max(0.,watermark-r['output_confirmed_at_s'])
            r.update(result='TESTING',continuous_evidence_s=min(elapsed,self.config.required_continuous_s),
                     message='连续测试中，请勿抬起；中断后必须重新开始')
        elif r['result']=='REFERENCE_PASS':
            self.frozen_trial=r.copy()
            r.update(result='AWAITING_VALIDATION',message='连续测试完成，可以移开；等待本次采集自动结束及完整性校验')
        elif r['result']=='NOT_PASSED':
            self.frozen_trial=r.copy();r['message']=r['reminders'][-1]['message']
        else:
            r.update(message=r['reminders'][-1]['message'] if r.get('reminders') else '请重测')
        self.result=self._wrap(r)
        return self.result

    def finish(self,capture):
        self.finalized=True
        meta=capture['metadata'];reason=metadata_problem(meta,self.p)
        if reason:return self._wrap({'result':'NOT_SUPPORTED','reason':'UNSUPPORTED_CAPTURE','message':reason},True)
        if self.fault:return self.error(self.fault,True)
        raw=np.asarray(capture['values'],dtype=np.uint16);idx=np.asarray(capture['indices'],dtype=np.uint32)
        integrity=validate_capture({'format':'coret.binary/2','schema':capture.get('schema'),'metadata':meta},raw,idx,
                                   CaptureConfig(range_code=meta.get('range'),target_hz=self.p.target_hz))
        if not integrity['eligible_for_profile']:
            r=self.error('采集未完整完成、通信异常或削顶；本轮不能判合格',True)
            r['integrity']=integrity;return r
        if len(raw)%100:return self.error('样本不满足完整 1 ms 分块',True)
        blocks=raw.reshape(-1,100).astype(float)*MV_PER_CODE[meta['range']]
        f=feature_arrays(blocks.min(1),blocks.max(1),self.p);events=detect(f,self.p)
        if self.committed:
            stable=[e for e in events if e['time_s']<=self.committed[-1]['time_s']+1e-9]
            if self.signature(stable)!=self.signature(self.committed):
                return self.error('完整数据与实时已确认事件不一致，请重测',True)
        r=assess_continuous(events,len(raw)/100000,self.profile,self.config)
        if self.frozen_trial is not None and r['result']!=self.frozen_trial['result']:
            return self.error('实时判定与完整回放不一致，本轮禁止放行',True)
        r.update(integrity=integrity,event_count=len(events),config=asdict(self.config),
                 message=r['reminders'][-1]['message'] if r.get('reminders') else '请检查测试状态')
        self.result=self._wrap(r,True)
        return self.result
