"""Continuous-output trial, experimental offline v0.3.

The v0.2 waveform detector and its frozen profile are reused unchanged.
One invocation represents ONE explicitly armed trial. A detected interruption
latches NOT_PASSED; later recovery cannot retry or accumulate held time.
This detects loss of output evidence, NOT physical contact or the cause of loss.
"""
from dataclasses import asdict, dataclass
import math

import numpy as np
from .coret_qc_v2 import check_profile, detect, validate_events

VERSION = 'coret-continuous-qc-0.3.0-experimental'
EPS = 1e-9


@dataclass(frozen=True)
class ContinuousConfig:
    required_continuous_s: float = 10.0
    max_silence_periods: float = 1.5

    def __post_init__(self):
        if not all(math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('continuous config must be finite')
        if not 4.0 <= self.required_continuous_s <= 120.0:
            raise ValueError('continuous duration must be 4..120 seconds')
        if not 1.0 < self.max_silence_periods < 2.0:
            raise ValueError('silence threshold must detect one missing nominal beat')


def assess_continuous(events, duration, profile, config=ContinuousConfig(), armed_at_s=0.):
    """Assess one trial using finalized event times and an observed raw duration.

    Preparation: first 12 regular events confirm output, not physical contact.
    Test: at least 10s AFTER confirmation; no 85%-coverage allowance for a gap.
    Timing jitter/isolated extras still use 4s cadence statistics (85% regular).
    Pass is emitted only on an observed beat spanning the full required time.
    Post-terminal data remain in the audit but cannot reverse PASS or NOT_PASSED.
    Calling again with an explicit new arm time is a NEW trial, not auto retry.
    """
    p, low, high = check_profile(profile)
    validate_events(events, duration)
    if not math.isfinite(armed_at_s) or not 0 <= armed_at_s < duration:
        raise ValueError('invalid explicit arm time')
    if config.required_continuous_s < p.evaluation_s:
        raise ValueError('continuous test must include a complete cadence window')
    end = max(0., duration-p.event_post_s)
    period = 1./p.target_hz
    max_gap = period*config.max_silence_periods
    eligible = [e for e in events if e.get('window_complete',False)
                and e.get('eligible',True) and e['p2p_mv'] >= p.event_floor_mv
                and armed_at_s-EPS <= e['time_s'] <= end+EPS]
    out = {'version':VERSION, 'armed_at_s':armed_at_s,
           'config':asdict(config), 'max_silence_s':max_gap,
           'observed_until_s':end, 'output_confirmed_at_s':None,
           'required_until_s':None, 'terminal_at_s':None,
           'continuous_evidence_s':0., 'windows':[], 'transitions':[],
           'reminders':[], 'requires_explicit_new_trial':True,
           'physical_contact_verified':False, 'jam_confirmed':False,
           'response_reference_gate_preserved':True,
           'qualification_scope':'experimental continuous output check, not certified product quality'}
    out['transitions'].append({'time_s':armed_at_s,'state':'PREPARING','message':'请保持接触，等待有效出击确认'})

    def terminal(result, reason, time_s, message):
        out.update(result=result, reason=reason, terminal_at_s=time_s)
        out['transitions'].append({'time_s':time_s,'state':result,'message':message})
        out['reminders'].append({'time_s':time_s,'kind':reason,'message':message})
        out['post_terminal_event_count']=sum(e['time_s']>time_s+EPS for e in eligible)
        return out

    run=0; previous=None; confirmation_index=None
    for i,e in enumerate(eligible):
        if previous is not None and abs(e['time_s']-previous['time_s']-period)<=p.interval_tolerance_s+EPS:
            run+=1
        else:run=1
        previous=e
        if run>=p.min_stable_events:
            confirmation_index=i
            break
    if confirmation_index is None:
        return terminal('RETEST','NO_CONFIRMED_OUTPUT',duration,
                        '未建立持续有效出击，本轮不能通过；请检查接触和设备状态')

    active=eligible[confirmation_index:]
    start=active[0]['time_s'];deadline=start+config.required_continuous_s
    out.update(output_confirmed_at_s=start,required_until_s=deadline)
    out['transitions'].append({'time_s':start,'state':'TESTING',
                              'message':f'开始连续测试，至少保持 {config.required_continuous_s:g} 秒，中途不要抬起'})
    seen=[active[0]]
    range_state=None;range_since=None;initial_range_uncertain=False
    for e in active[1:]:
        last=seen[-1]['time_s'];now=e['time_s']
        if now-last>max_gap+EPS:
            return terminal('NOT_PASSED','OUTPUT_INTERRUPTED',last+max_gap,
                            '出击中断，本轮未通过；保持接触时请检查是否卡弹，若抬起请明确重新开始')
        seen.append(e)
        out['continuous_evidence_s']=now-start
        if now-start>=p.evaluation_s-EPS:
            window=[x for x in seen if x['time_s']>now-p.evaluation_s+EPS]
            dt=np.diff([x['time_s'] for x in window])
            regular=float(np.mean(abs(dt-period)<=p.interval_tolerance_s+EPS)) if len(dt) else 0.
            coverage=len(window)/(p.evaluation_s*p.target_hz)
            amplitude=float(np.median([x['p2p_mv'] for x in window]))
            cadence_ok=coverage>=p.min_event_coverage and coverage<=1.15 and regular>=p.min_regular_interval_fraction
            out['windows'].append({'start_s':now-p.evaluation_s,'end_s':now,
                                   'event_ids':[x['id'] for x in window], 'coverage':coverage,
                                   'regular_fraction':regular,'cadence_ok':bool(cadence_ok),
                                   'amplitude_median_mv':amplitude,
                                   'reference_band_note':'LOW' if amplitude<low else 'HIGH' if amplitude>high else 'IN_RANGE'})
            if len(out['windows'])==1 and not low<=amplitude<=high:
                initial_range_uncertain=True
            if not cadence_ok:
                return terminal('NOT_PASSED','CADENCE_UNSTABLE',now,
                                '出击节拍不稳定，本轮未通过；请保持接触并检查状态')
            state='LOW' if amplitude<low else 'HIGH' if amplitude>high else None
            if state is None:
                range_state=None;range_since=None
            else:
                if range_state!=state:range_state=state;range_since=now
                if now-range_since>=p.range_persistence_s-EPS:
                    return terminal('NOT_PASSED','RESPONSE_OUTSIDE_REFERENCE',now,
                                    '响应持续超出原有参考范围，本轮未通过；请检查测试状态')
        if now>=deadline-EPS:
            if range_state is not None or initial_range_uncertain:
                return terminal('RETEST','RESPONSE_REFERENCE_UNCERTAIN',now,
                                '已达到连续时长，但响应范围证据不充分，请重新测试')
            return terminal('REFERENCE_PASS','CONTINUOUS_OUTPUT_COMPLETE',now,
                            f'连续有效出击已达到 {config.required_continuous_s:g} 秒，本轮参考通过，可以移开')
    # Unlike v0.2, do not truncate evaluation at the last detected event.
    # Silence inside the required trial is positive evidence of lost output.
    missing_deadline=seen[-1]['time_s']+max_gap
    if end>missing_deadline+EPS:
        return terminal('NOT_PASSED','OUTPUT_INTERRUPTED',missing_deadline,
                        '出击中断，本轮未通过；无法仅凭波形区分抬起和卡弹，恢复出击不自动续测')
    return terminal('RETEST','CAPTURE_ENDED_BEFORE_COMPLETION',duration,
                    '记录在连续测试完成前结束，本轮不能通过；请重新完成一轮完整测试')


def evaluate_continuous(features, profile, config=ContinuousConfig(), armed_at_s=0.):
    p,_,_=check_profile(profile)
    result={k:v for k,v in features.items() if not isinstance(v,np.ndarray)}
    if not features['integrity']['eligible_for_profile']:
        result.update(version=VERSION,result='INVALID_DATA',reason='CAPTURE_INTEGRITY',events=[],
                      reminders=[],physical_contact_verified=False,jam_confirmed=False)
        return result
    events=detect(features,p)
    result.update(assess_continuous(events,features['duration_s'],profile,config,armed_at_s),events=events)
    return result
