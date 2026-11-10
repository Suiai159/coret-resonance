"""Experimental offline range QC, no physical force/strength regression.

All amplitudes are mV. Use only approved reference records to fit a profile;
never fit to the unknown item under test. Centered median filters and peak
selection require future data: NOT a tested real-time implementation.
"""
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import json

import numpy as np
from scipy import ndimage, signal

from .coret_offline import Config as CaptureConfig, load_capture, validate_capture, sha256

VERSION = 'coret-range-qc-0.2.0-experimental'
# Nominal ADC input spans in mV for every firmware-supported range. Bipolar
# spans cover negative through positive full scale; unipolar spans start at 0.
# Features use peak-to-peak amplitude, so the DC zero-code offset cancels.
MV_PER_CODE = {code: span / 65536 for code, span in {
    0: 24576, 1: 20480, 2: 12288, 3: 10240, 4: 5120,
    8: 12288, 9: 10240, 10: 6144, 11: 5120,
}.items()}
FEATURE_KEYS = ('bin_s', 'local_noise_bins', 'noise_mad_floor_mv', 'target_hz')


@dataclass(frozen=True)
class Policy:
    target_hz: float = 4.0
    bin_s: float = .001
    local_noise_bins: int = 251
    noise_mad_floor_mv: float = 1.0
    score_threshold: float = 6.0
    score_prominence: float = 3.0
    refractory_s: float = .120
    onset_lookback_s: float = .040
    onset_relative_height: float = .50
    event_pre_s: float = .010
    event_post_s: float = .070
    event_floor_mv: float = 46.875
    interval_tolerance_s: float = .010
    min_stable_events: int = 12
    evaluation_s: float = 4.0
    evaluation_step_s: float = .250
    min_event_coverage: float = .85
    min_regular_interval_fraction: float = .85
    interruption_s: float = .800
    range_persistence_s: float = .750
    reference_margin_fraction: float = .10

    def __post_init__(self):
        values=asdict(self)
        if any(not np.isfinite(v) for v in values.values()):
            raise ValueError('policy values must be finite')
        if self.target_hz<=0 or self.bin_s!=.001:
            raise ValueError('profile requires a positive target and 1ms bins')
        if self.local_noise_bins<3 or self.local_noise_bins%2!=1:
            raise ValueError('local window must be positive odd')
        if not isinstance(self.local_noise_bins,int) or not isinstance(self.min_stable_events,int):
            raise ValueError('window sizes and event counts must be integers')
        if self.min_stable_events<2 or not 0<self.interval_tolerance_s<1/self.target_hz:
            raise ValueError('invalid temporal evidence policy')
        if not 0<self.min_event_coverage<=1 or not 0<self.min_regular_interval_fraction<=1:
            raise ValueError('invalid coverage fraction')
        for key in ('noise_mad_floor_mv','score_threshold','score_prominence','refractory_s',
                    'event_post_s','event_floor_mv','evaluation_s','evaluation_step_s','interruption_s','range_persistence_s'):
            if values[key]<=0:raise ValueError('invalid positive parameter: '+key)
        if not 0<=self.onset_lookback_s<self.refractory_s or not 0<self.onset_relative_height<=1:
            raise ValueError('invalid onset localization')
        if self.event_pre_s<0 or not 0<=self.reference_margin_fraction<=.25:
            raise ValueError('invalid reference margin or amplitude window')
        if self.interruption_s<=1/self.target_hz+self.interval_tolerance_s:
            raise ValueError('interruption grace must exceed one cycle')


def read_features(path, policy=Policy()):
    """Validate raw file, retain compact 1ms extrema, not the whole raw capture."""
    path=Path(path);before=sha256(path)
    manifest,raw,indices=load_capture(path);meta=manifest['metadata']
    range_code=meta.get('range')
    validation=validate_capture(manifest,raw,indices,
                                CaptureConfig(range_code=range_code,target_hz=policy.target_hz))
    if range_code not in MV_PER_CODE:
        validation['problems'].append('unsupported_voltage_range')
        validation['eligible_for_profile']=False
    obj={'file':path.name,'source_path':str(path.resolve()),'sha256':before,
         'metadata':{k:v for k,v in meta.items() if k!='events'},'integrity':validation,
         'duration_s':len(raw)/meta['sample_rate'] if meta.get('sample_rate',0)>0 else None,
         'feature_policy':{k:getattr(policy,k) for k in FEATURE_KEYS}}
    if validation['eligible_for_profile']:
        n=round(meta['sample_rate']*policy.bin_s)
        if len(raw)%n:raise ValueError('capture must contain whole 1ms bins')
        blocks=raw.astype(float).reshape(-1,n)*MV_PER_CODE[range_code]
        obj['lo_mv']=blocks.min(1);obj['hi_mv']=blocks.max(1)
        speed=obj['hi_mv']-obj['lo_mv']
        baseline=ndimage.median_filter(speed,size=policy.local_noise_bins,mode='nearest')
        deviation=ndimage.median_filter(abs(speed-baseline),size=policy.local_noise_bins,mode='nearest')
        obj['noise_scale_mv']=np.maximum(deviation,policy.noise_mad_floor_mv)
        obj['baseline_mv']=baseline
        obj['score']=(speed-baseline)/obj['noise_scale_mv']
        obj['speed_mv']=speed
    if sha256(path)!=before:raise RuntimeError('Source changed: '+str(path))
    return obj


def detect(features, policy):
    if not features['integrity']['eligible_for_profile']:
        return []
    if features.get('feature_policy')!={k:getattr(policy,k) for k in FEATURE_KEYS}:
        raise ValueError('feature preprocessing does not match profile; reload capture with profile policy')
    score=features['score'];lo=features['lo_mv'];hi=features['hi_mv']
    # Local SNR gates eligibility; physical short-window excursion ranks peaks.
    # Ranking by the ratio alone can move timing to a smaller secondary peak.
    height=features['baseline_mv']+policy.score_threshold*features['noise_scale_mv']
    prominence=policy.score_prominence*features['noise_scale_mv']
    peaks,_=signal.find_peaks(features['speed_mv'],height=height,prominence=prominence,
                             distance=round(policy.refractory_s/policy.bin_s))
    # Pick the earliest comparable qualified lobe within the same short burst.
    # This uses observed peaks, never snaps times to the expected 4Hz lattice.
    lobes,_=signal.find_peaks(features['speed_mv'],height=height,prominence=prominence)
    excess=features['speed_mv']-features['baseline_mv']
    lookback=round(policy.onset_lookback_s/policy.bin_s)
    localized=[]
    for seed in peaks:
        candidates=lobes[(lobes>=seed-lookback)&(lobes<=seed)]
        candidates=candidates[excess[candidates]>=policy.onset_relative_height*excess[seed]]
        localized.append((int(candidates[0]),int(seed)))
    events=[]
    pre=round(policy.event_pre_s/policy.bin_s);post=round(policy.event_post_s/policy.bin_s)
    for i,(peak,seed) in enumerate(localized):
        complete=peak>=pre and peak+post<=len(score)
        amp=float(hi[max(0,peak-pre):min(len(score),peak+post)].max()-lo[max(0,peak-pre):min(len(score),peak+post)].min())
        events.append({'id':i,'time_s':float(peak*policy.bin_s),'p2p_mv':amp,
                       'score':float(score[peak]),'seed_time_s':float(seed*policy.bin_s),
                       'window_complete':bool(complete),
                       'eligible':bool(complete and amp>=policy.event_floor_mv)})
    return events


def validate_events(events,duration):
    if not np.isfinite(duration) or duration<=0:raise ValueError('invalid duration')
    for i,e in enumerate(events):
        if not np.isfinite(e['time_s']) or not 0<=e['time_s']<=duration or not np.isfinite(e['p2p_mv']) or e['p2p_mv']<0:
            raise ValueError('invalid event')
        if i and e['time_s']<=events[i-1]['time_s']:raise ValueError('events must be strictly ordered')


def session_windows(events,duration,policy):
    """No best-window search. First 12-event confirmation anchors each session.

    4s assessment starts at confirmation. Subsequent rolling windows remain in
    the audit and may downgrade, never upgrade a failed first assessment.
    A >800ms gap separates contact episodes, with cause explicitly unknown.
    """
    validate_events(events,duration)
    end=duration-policy.event_post_s;eps=1e-9
    strong=[e for e in events if e['window_complete'] and e['p2p_mv']>=policy.event_floor_mv and e['time_s']<=end+eps]
    groups=[]
    for event in strong:
        if not groups or event['time_s']-groups[-1][-1]['time_s']>policy.interruption_s+eps:groups.append([])
        groups[-1].append(event)
    sessions=[]
    period=1/policy.target_hz
    for group in groups:
        run=1;confirmed=None;run_start=group[0]['time_s'];longest=1
        for previous,current in zip(group,group[1:]):
            if abs(current['time_s']-previous['time_s']-period)<=policy.interval_tolerance_s+eps:
                run+=1
            else:
                run=1;run_start=current['time_s']
            longest=max(longest,run)
            if run>=policy.min_stable_events and confirmed is None:
                confirmed=current['time_s'];evidence_start=run_start
        last=group[-1]['time_s']
        interrupted=last+policy.interruption_s<=end+eps
        session={'start_s':group[0]['time_s'],'last_event_s':last,'event_count':len(group),
                 'longest_regular_run':longest,'confirmation_s':confirmed,
                 'evidence_start_s':evidence_start if confirmed is not None else None,
                 'interruption_s':last+policy.interruption_s if interrupted else None,
                 'end_reason':'response_interrupted_cause_unknown' if interrupted else 'recording_boundary',
                 'windows':[]}
        if confirmed is not None:
            # Do not pad an indefinitely absent item with synthetic missing hits.
            limit=min(end,last+period+policy.interval_tolerance_s)
            first=confirmed+policy.evaluation_s
            n=max(0,int(np.floor((limit-first+eps)/policy.evaluation_step_s))+1)
            for j in range(n):
                right=first+j*policy.evaluation_step_s;left=right-policy.evaluation_s
                chosen=[e for e in group if left-eps<=e['time_s']<right-eps]
                dt=np.diff([e['time_s'] for e in chosen]);amp=np.array([e['p2p_mv'] for e in chosen])
                expected=policy.evaluation_s*policy.target_hz
                regular=float(np.mean(abs(dt-period)<=policy.interval_tolerance_s+eps)) if len(dt) else 0.
                coverage=len(chosen)/expected
                # A single missed event is tolerated, but repeated irregularity is not.
                credible=coverage>=policy.min_event_coverage and coverage<=1.15 and regular>=policy.min_regular_interval_fraction
                session['windows'].append({'start_s':left,'end_s':right,'event_count':len(chosen),
                    'event_ids':[e['id'] for e in chosen], 'coverage':coverage,'regular_fraction':regular,
                    'cadence_ok':bool(credible), 'amplitude_median_mv':float(np.median(amp)) if len(amp) else None,
                    'amplitude_p10_p90_mv':np.quantile(amp,[.1,.9]).tolist() if len(amp) else [],
                    'interval_median_ms':float(np.median(dt)*1000) if len(dt) else None})
        sessions.append(session)
    return sessions


def build_profile(references, backgrounds):
    """Fit ONLY to approved in-range references + explicit idle controls.

    No setpoint regression, no per-item normalization to its own output.
    Fixed engineering guards are separately named; learned bounds remain
    experimental until real outside-range items have been tested.
    """
    if not references or not backgrounds:raise ValueError('references and backgrounds are required')
    if any(not f['integrity']['eligible_for_profile'] for f in references+backgrounds):
        raise ValueError('invalid training capture; no partial profile activation')
    p=Policy();noise=[]
    for f in backgrounds:
        hi=f['hi_mv'];lo=f['lo_mv'];size=80
        amps=np.lib.stride_tricks.sliding_window_view(hi,size).max(1)-np.lib.stride_tricks.sliding_window_view(lo,size).min(1)
        noise.append({'file':f['file'],'score_q999':float(np.quantile(f['score'],.999)),
                      'amp80_q99_mv':float(np.quantile(amps,.99))})
    p=replace(p,score_threshold=max(6.,1.2*max(n['score_q999'] for n in noise)),
              event_floor_mv=max(5.,1.5*max(n['amp80_q99_mv'] for n in noise)))
    event_sets=[detect(f,p) for f in references]
    interval_errors=[]
    for events in event_sets:
        ts=[e['time_s'] for e in events if e['eligible']]
        err=abs(np.diff(ts)-1/p.target_hz)
        interval_errors.extend(err[err<=.020+1e-9])
    if len(interval_errors)<100:raise ValueError('insufficient cadence evidence')
    tolerance=float(np.clip(np.quantile(interval_errors,.99)+.002,.005,.010))
    p=replace(p,interval_tolerance_s=tolerance)
    support=[]
    for f,events in zip(references,event_sets):
        sessions=session_windows(events,f['duration_s'],p)
        windows=[w for s in sessions for w in s['windows'] if w['cadence_ok']]
        if len(windows)<3:raise ValueError('insufficient reference windows: '+f['file'])
        medians=[w['amplitude_median_mv'] for w in windows]
        q=np.quantile(medians,[.1,.5,.9]).tolist()
        support.append({'file':f['file'],'sha256':f['sha256'],'credible_window_count':len(windows),
                        'window_median_p10_median_p90_mv':q})
    low=min(s['window_median_p10_median_p90_mv'][0] for s in support)*(1-p.reference_margin_fraction)
    high=max(s['window_median_p10_median_p90_mv'][2] for s in support)*(1+p.reference_margin_fraction)
    if not 0<p.event_floor_mv<low<high:raise ValueError('noise floor and allowed range are not separated')
    profile={'version':VERSION,'status':'EXPERIMENTAL_NOT_PRODUCTION_VALIDATED','amplitude_unit':'mV',
            'strength_estimation':False,'policy':asdict(p),
            'allowed_window_median_mv':[float(low),float(high)],
            'learned_parameters':['event_floor_mv','score_threshold_with_fixed_floor','interval_tolerance_s_with_fixed_limits','allowed_window_median_mv'],
            'fit_method':'per-reference credible 4s-window median P10/P90 envelope, 10% fixed margin; pooled allowed settings, no setpoint classification',
            'reference_support':support,'background_support':noise,
            'background_hashes':{f['file']:f['sha256'] for f in backgrounds},
            'validation_limits':['No measured force boundary. No real outside-range validation.',
                                 'No unknown item is used to update this profile. Rejected references prevent profile activation.']}
    audit={f['file']:evaluate(f,profile)['result'] for f in references}
    profile['reference_replay']=audit
    rejected=[name for name,result in audit.items() if result!='REFERENCE_PASS']
    if rejected:raise ValueError('reference replay rejected; profile not activated: '+', '.join(rejected))
    return profile


def check_profile(profile):
    if profile.get('version')!=VERSION or profile.get('amplitude_unit')!='mV' or profile.get('strength_estimation') is not False:
        raise ValueError('unsupported profile schema')
    p=Policy(**profile['policy'])
    low,high=profile['allowed_window_median_mv']
    if not np.isfinite(low) or not np.isfinite(high) or not 0<p.event_floor_mv<low<high:
        raise ValueError('invalid response bounds')
    return p,float(low),float(high)


def assess_events(events,duration,profile):
    p,low,high=check_profile(profile)
    sessions=session_windows(events,duration,p)
    reminders=[];qualified=[]
    for session in sessions:
        windows=session['windows']
        if session['confirmation_s'] is None:
            session['result']='NO_CONFIRMED_ACTIVITY'
        elif not windows:
            # An aborted placement is retained, but is not a completed failed test.
            session['result']='RETEST';session['reason']='insufficient_complete_evaluation_window'
        else:
            qualified.append(session);pending=None;pending_start=None;latched_fail=False
            ever_irregular=False;first_result=None
            for w in windows:
                if not w['cadence_ok']:state='UNSTABLE'
                elif w['amplitude_median_mv']<low:state='LOW'
                elif w['amplitude_median_mv']>high:state='HIGH'
                else:state='IN_RANGE'
                w['state']=state
                if first_result is None:first_result=state
                if state=='UNSTABLE':ever_irregular=True
                if state in ('LOW','HIGH','UNSTABLE'):
                    if pending!=state:pending=state;pending_start=w['end_s']
                    if w['end_s']-pending_start>=p.range_persistence_s-1e-9:
                        if state in ('LOW','HIGH'):latched_fail=True
                        if not any(r['kind']==state and r['session_start_s']==session['start_s'] for r in reminders):
                            reminders.append({'time_s':w['end_s'],'kind':state,'session_start_s':session['start_s'],
                                'message':{'LOW':'响应持续偏弱，请检查接触位置','HIGH':'响应持续偏强，请检查测试状态','UNSTABLE':'节拍持续不稳定，请保持位置'}[state]})
                else:
                    if pending is not None:
                        reminders.append({'time_s':w['end_s'],'kind':'RECOVERED','session_start_s':session['start_s'],
                                          'message':'响应已恢复；既有异常记录保留'})
                    pending=None;pending_start=None
            if latched_fail:
                session['result']='REFERENCE_FAIL';session['reason']='persistent_response_outside_reference'
            elif first_result!='IN_RANGE' or pending is not None or ever_irregular:
                session['result']='RETEST';session['reason']='unresolved_or_initial_assessment_uncertainty'
            else:
                session['result']='REFERENCE_PASS';session['reason']='complete_assessment_within_reference'
            session['first_evaluation_state']=first_result
        if session['interruption_s'] is not None and session['confirmation_s'] is not None:
            reminders.append({'time_s':session['interruption_s'],'kind':'INTERRUPTED','session_start_s':session['start_s'],
                              'message':'打击响应中断，请检查是否移开或接触松动；原因未知'})
    if any(s['result']=='REFERENCE_FAIL' for s in qualified):result='REFERENCE_FAIL'
    elif not qualified or any(s['result']=='RETEST' for s in qualified):result='RETEST'
    else:result='REFERENCE_PASS'
    if not qualified:
        confirmed=any(s['confirmation_s'] is not None for s in sessions)
        reminders.append({'time_s':duration,'kind':'INSUFFICIENT_EVALUATION' if confirmed else 'NO_VALID_ACTIVITY',
                          'message':'有效评估时间不足，请保持接触后重测' if confirmed else '未建立持续有效打击，请确认接触位置'})
    return {'result':result,'sessions':sessions,'reminders':sorted(reminders,key=lambda x:x['time_s']),
            'stable_activity_found':any(s['confirmation_s'] is not None for s in sessions),
            'complete_evaluation_windows':sum(len(s['windows']) for s in sessions),
            'qualification_scope':'experimental waveform/reference check; not certified product quality',
            'range_estimation':None}


def evaluate(features,profile):
    p,_,_=check_profile(profile)
    result={k:v for k,v in features.items() if not isinstance(v,np.ndarray)}
    if not features['integrity']['eligible_for_profile']:
        result.update(result='INVALID_DATA',events=[],sessions=[],reminders=[])
        return result
    events=detect(features,p)
    result.update(events=events,**assess_events(events,features['duration_s'],profile))
    return result


def save_profile(profile,path):
    check_profile(profile)
    Path(path).write_text(json.dumps(profile,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def load_profile(path):
    profile=json.loads(Path(path).read_text(encoding='utf-8'));check_profile(profile);return profile
