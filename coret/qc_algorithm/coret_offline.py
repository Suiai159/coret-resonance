"""CoreT offline response segmentation, NOT product-quality acceptance.

Detector uses whole-record statistics and centered filters. All times are signal
coordinates, NOT demonstrated streaming/UI decision latency. No physical force,
actual drive phase, or fault cause is inferred. No firmware/desktop dependencies.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
from scipy import ndimage, signal


@dataclass(frozen=True)
class Config:
    version: str = 'coret-offline-segments-0.1.0'
    sample_rate_hz: int = 100000
    range_code: int = 3
    target_hz: float = 4.0
    amplitude_floor_code: float = 300.0
    interval_tolerance_s: float = 0.010
    min_stable_events: int = 12
    bin_s: float = 0.001
    baseline_bins: int = 101
    smoothing_bins: int = 5
    peak_distance_s: float = 0.120
    prominence_mad_factor: float = 6.0
    prominence_quantile: float = 0.995
    prominence_fraction: float = 0.12
    prominence_floor_code: float = 8.0
    amplitude_pre_s: float = 0.010
    amplitude_post_s: float = 0.070

    def __post_init__(self):
        if self.sample_rate_hz <= 0 or self.target_hz <= 0:
            raise ValueError('sample rate and frequency must be positive')
        if not 0 <= self.interval_tolerance_s < self.period_s:
            raise ValueError('invalid period tolerance')
        if self.min_stable_events < 2 or self.amplitude_floor_code <= 0:
            raise ValueError('invalid stable count or amplitude floor')
        if self.bin_s <= 0 or self.peak_distance_s <= 0:
            raise ValueError('bin and peak distance must be positive')
        if self.baseline_bins < 1 or self.baseline_bins % 2 != 1 or self.smoothing_bins < 1:
            raise ValueError('invalid filter widths')
        if self.amplitude_pre_s < 0 or self.amplitude_post_s <= 0:
            raise ValueError('invalid amplitude window')
        if not 0 <= self.prominence_quantile <= 1:
            raise ValueError('invalid quantile')
        if min(self.prominence_mad_factor, self.prominence_fraction, self.prominence_floor_code) < 0:
            raise ValueError('invalid prominence parameters')
        samples_per_bin = self.sample_rate_hz * self.bin_s
        if abs(samples_per_bin - round(samples_per_bin)) > 1e-9 or samples_per_bin < 1:
            raise ValueError('bin must contain a whole positive number of samples')

    @property
    def period_s(self):
        return 1.0 / self.target_hz

    def to_dict(self):
        return asdict(self)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_capture(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        samples = np.frombuffer(archive.read('samples.u16le'), dtype='<u2').copy()
        indices = np.frombuffer(archive.read('indices.u32le'), dtype='<u4').copy()
    return manifest, samples, indices


def validate_capture(manifest, samples, indices, config):
    """Saved-array + metadata checks only; no independent raw-protocol CRC replay."""
    metadata = manifest.get('metadata', {})
    problems = []
    if manifest.get('format') != 'coret.binary/2' or manifest.get('schema') != 'coret.capture/1':
        problems.append('unsupported_capture_format')
    if metadata.get('sample_rate') != config.sample_rate_hz:
        problems.append('sample_rate_mismatch')
    if metadata.get('range') != config.range_code:
        problems.append('range_outside_frozen_profile')
    if metadata.get('valve_hz') != config.target_hz:
        problems.append('configured_drive_frequency_mismatch')
    if not len(samples) or len(samples) != len(indices):
        problems.append('empty_or_array_length_mismatch')
    index_errors = int(np.count_nonzero(np.diff(indices.astype(np.int64)) != 1))
    if index_errors or (len(indices) and indices[0] != 0):
        problems.append('indices_not_contiguous_from_zero')
    end = metadata.get('end') or {}
    for field, value in [('samples', metadata.get('samples')),
                         ('target_samples', metadata.get('target_samples')),
                         ('end.stored_samples', end.get('stored_samples'))]:
        if value != len(samples):
            problems.append('count_mismatch:' + field)
    for field in ('crc_errors', 'bad_headers', 'index_gaps', 'garbage_bytes'):
        if metadata.get(field) != 0:
            problems.append('metadata_error_or_missing:' + field)
    if metadata.get('errors') != []:
        problems.append('metadata_errors_or_missing')
    if metadata.get('passed') is not True or metadata.get('measurement_complete') is not True:
        problems.append('capture_not_reported_complete_and_valid')
    if metadata.get('early_stop') is not False or metadata.get('start_ack') != 0:
        problems.append('capture_start_stop_status_invalid_or_missing')
    clipped = int(np.count_nonzero((samples == 0) | (samples == 65535)))
    if clipped:
        problems.append('adc_rail_samples')
    return {'eligible_for_profile': not problems, 'problems': problems,
            'clipped_samples': clipped, 'nonunit_index_steps': index_errors,
            'scope': 'saved arrays and metadata only; no independent rx.bin CRC replay'}


def detect_events(samples, config):
    """Baseline detector preserved except no fixed first-60ms exclusion.

    Events whose 80ms amplitude windows cross a recording boundary stay in the
    audit list, but cannot qualify or reset the temporal state machine.
    """
    x = np.asarray(samples, dtype=np.float64)
    fs = config.sample_rate_hz
    per_bin = round(fs * config.bin_s)
    usable = len(x) // per_bin * per_bin
    if usable < per_bin * max(config.baseline_bins, config.smoothing_bins):
        raise ValueError('capture_too_short_for_detector')
    b = x[:usable].reshape(-1, per_bin)
    base = ndimage.median_filter(np.median(b, axis=1), size=config.baseline_bins, mode='nearest')
    lo, hi = b.min(axis=1) - base, b.max(axis=1) - base
    envelope = ndimage.uniform_filter1d(np.maximum(hi, -lo), size=config.smoothing_bins)
    center = float(np.median(envelope))
    prominence = max(config.prominence_mad_factor * float(np.median(abs(envelope - center))),
                     config.prominence_fraction * (float(np.quantile(envelope, config.prominence_quantile)) - center),
                     config.prominence_floor_code)
    peaks, _ = signal.find_peaks(envelope, distance=max(1, round(config.peak_distance_s / config.bin_s)),
                                 prominence=prominence, height=center + prominence)
    events = []
    pre, post = round(config.amplitude_pre_s * fs), round(config.amplitude_post_s * fs)
    for index, peak in enumerate(peaks):
        sample_index = int(peak * per_bin)
        left, right = sample_index - pre, sample_index + post
        complete = left >= 0 and right <= len(x)
        amplitude = float(np.ptp(x[max(0, left):min(len(x), right)]))
        eligible = complete and amplitude >= config.amplitude_floor_code
        events.append({'id': index, 'time_s': sample_index / fs, 'p2p_code': amplitude,
                       'window_complete': complete, 'eligible': eligible,
                       'reason': 'eligible' if eligible else ('boundary_window' if not complete else 'below_amplitude_floor')})
    diagnostics = {'center_code': center, 'prominence_code': prominence, 'candidate_count': len(events),
                   'amplitude_eligible_count': sum(e['eligible'] for e in events),
                   'below_floor_count': sum(e['reason'] == 'below_amplitude_floor' for e in events),
                   'boundary_candidate_count': sum(not e['window_complete'] for e in events),
                   'discarded_partial_bin_samples': len(x) - usable,
                   'time_resolution_s': config.bin_s}
    return events, diagnostics, (lo, hi, envelope)


def event_stats(events):
    times = np.array([e['time_s'] for e in events])
    amplitudes = np.array([e['p2p_code'] for e in events])
    dt = np.diff(times)
    return {'event_count': len(events),
            'frequency_hz': float(1 / np.mean(dt)) if len(dt) else None,
            'interval_median_ms': float(np.median(dt) * 1000) if len(dt) else None,
            'interval_mad_ms': float(np.median(abs(dt - np.median(dt))) * 1000) if len(dt) else None,
            'amplitude_p10_median_p90_code': np.quantile(amplitudes, [.1, .5, .9]).tolist() if len(amplitudes) else [],
            'amplitude_min_max_code': [float(amplitudes.min()), float(amplitudes.max())] if len(amplitudes) else []}


def segment_events(events, duration_s, config):
    """Pure deterministic event-state machine, using inclusive time tolerance.

    A weak/early event breaks a run; late/missing response ends it at the first
    overdue deadline. There is no infinite counting of misses after withdrawal.
    Every attempt survives in output. A stable run is retrospectively bounded by
    first/last evidence; confirmation is at event 12 (not retroactively at 1).
    """
    if not np.isfinite(duration_s) or duration_s <= 0:
        raise ValueError('invalid duration')
    for i, event in enumerate(events):
        if (not np.isfinite(event['time_s']) or not np.isfinite(event['p2p_code']) or
                event['time_s'] < 0 or event['time_s'] > duration_s or event['p2p_code'] < 0):
            raise ValueError('invalid event')
        if i and event['time_s'] <= events[i - 1]['time_s']:
            raise ValueError('events must be strictly time ordered')
    observable_end = max(0.0, duration_s - config.amplitude_post_s)
    lower, upper = config.period_s - config.interval_tolerance_s, config.period_s + config.interval_tolerance_s
    eps = 1e-9
    transitions = [{'time_s': 0.0, 'state': 'WAITING', 'reason': 'capture_start_no_evidence_yet'}]
    attempts, breaks = [], []
    run = []
    confirmation = None
    ever_stable = False

    def transition(time, state, reason):
        if transitions[-1]['state'] != state:
            transitions.append({'time_s': float(time), 'state': state, 'reason': reason})

    def finish(time, reason):
        nonlocal run, confirmation
        if run:
            attempts.append({'start_s': run[0]['time_s'], 'last_event_s': run[-1]['time_s'],
                             'end_s': float(time), 'confirmation_s': confirmation,
                             'confirmed': confirmation is not None, 'end_reason': reason,
                             'event_ids': [e['id'] for e in run], **event_stats(run)})
        run, confirmation = [], None

    for event in events:
        t = event['time_s']
        if not event.get('window_complete', True) or t > observable_end + eps:
            continue
        if run:
            deadline = run[-1]['time_s'] + upper
            if t > deadline + eps:
                was_stable = confirmation is not None
                finish(deadline, 'response_deadline_missed')
                breaks.append({'time_s': deadline, 'reason': 'response_deadline_missed', 'after_confirmed_run': was_stable})
                transition(deadline, 'INTERRUPTED' if ever_stable else 'WAITING', 'response_deadline_missed')
        # Derive eligibility from amplitude, not any external classifier label.
        if event['p2p_code'] < config.amplitude_floor_code:
            if run:
                was_stable = confirmation is not None
                finish(t, 'weak_detected_candidate')
                breaks.append({'time_s': t, 'reason': 'weak_detected_candidate', 'after_confirmed_run': was_stable})
            transition(t, 'INTERRUPTED' if ever_stable else 'WAITING', 'weak_detected_candidate')
            continue
        if run and t - run[-1]['time_s'] < lower - eps:
            was_stable = confirmation is not None
            finish(t, 'interval_too_short')
            breaks.append({'time_s': t, 'reason': 'interval_too_short', 'after_confirmed_run': was_stable})
            transition(t, 'CANDIDATE', 'unexpected_early_candidate_new_attempt')
        if not run:
            transition(t, 'CANDIDATE', 'eligible_candidate')
        run.append(event)
        if len(run) == config.min_stable_events:
            confirmation = float(t)
            ever_stable = True
            transition(t, 'STABLE', 'consecutive_cadence_evidence_confirmed')
    if run:
        deadline = run[-1]['time_s'] + upper
        if deadline <= observable_end + eps:
            was_stable = confirmation is not None
            finish(deadline, 'response_deadline_missed')
            breaks.append({'time_s': deadline, 'reason': 'response_deadline_missed', 'after_confirmed_run': was_stable})
            transition(deadline, 'INTERRUPTED' if ever_stable else 'WAITING', 'response_deadline_missed')
        else:
            finish(observable_end, 'capture_boundary_next_response_unobservable')
    timeline = []
    for i, tr in enumerate(transitions):
        end = transitions[i + 1]['time_s'] if i + 1 < len(transitions) else observable_end
        if end > tr['time_s'] + eps:
            timeline.append({'start_s': tr['time_s'], 'end_s': end, 'state': tr['state'], 'reason': tr['reason']})
    if observable_end < duration_s:
        timeline.append({'start_s': observable_end, 'end_s': duration_s, 'state': 'BOUNDARY_UNASSESSED',
                         'reason': 'insufficient_future_samples_for_full_amplitude_window'})
    stable = [attempt for attempt in attempts if attempt['confirmed']]
    return {'product_qc': 'NOT_EVALUATED', 'stable_segment_exists': bool(stable),
            'observation': 'STABLE_SEGMENT_OBSERVED' if stable else 'NO_STABLE_SEGMENT',
            'stable_segments': stable, 'attempts': attempts, 'transitions': transitions, 'timeline': timeline,
            'breaks': breaks, 'confirmed_run_break_count': sum(b['after_confirmed_run'] for b in breaks),
            'analysis_end_s': observable_end, 'boundary_unassessed_tail_s': duration_s - observable_end,
            'earliest_confirmation_s': stable[0]['confirmation_s'] if stable else None,
            'all_eligible_event_stats': event_stats([e for e in events if e.get('window_complete', True)
                                                    and e['p2p_code'] >= config.amplitude_floor_code
                                                    and e['time_s'] <= observable_end + eps])}


def analyze_capture(path, config):
    path = Path(path)
    before = sha256(path)
    manifest, samples, indices = load_capture(path)
    metadata = manifest.get('metadata', {})
    integrity = validate_capture(manifest, samples, indices, config)
    result = {'file': path.name, 'source_path': str(path.resolve()), 'sha256': before,
              'metadata': {k: v for k, v in metadata.items() if k != 'events'},
              'integrity': integrity, 'sample_count': len(samples), 'product_qc': 'NOT_EVALUATED',
              'duration_s': len(samples) / metadata['sample_rate'] if metadata.get('sample_rate', 0) > 0 else None,
              'full_p2p_code': float(np.ptp(samples.astype(float))) if len(samples) else None}
    trace = None
    if integrity['eligible_for_profile']:
        try:
            events, detector, trace = detect_events(samples, config)
            result.update(events=events, detector=detector, **segment_events(events, result['duration_s'], config))
        except ValueError as error:
            integrity['eligible_for_profile'] = False
            integrity['problems'].append(str(error))
            result['observation'] = 'INVALID_DATA_FOR_PROFILE'
    else:
        result['observation'] = 'INVALID_DATA_FOR_PROFILE'
    if sha256(path) != before:
        raise RuntimeError('Source changed while being analyzed: ' + str(path))
    return result, trace
