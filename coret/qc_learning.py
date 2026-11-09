"""Optional, explicitly activated reference learning. No device I/O or online adaptation."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import numpy as np
from .storage import atomic_write, load_capture, MAX_FILE_BYTES
from .qc_algorithm.coret_offline import Config, validate_capture, sha256
from .qc_algorithm.coret_qc_v2 import Policy, MV_PER_CODE, build_profile, check_profile, detect
from .qc_algorithm.coret_qc_v3 import ContinuousConfig, assess_continuous

SCHEMA = 'coret.reference-learning/1'
CORES = ('core01', 'core02', 'core03')
LEVELS = ('low', 'mid', 'high')
SLOTS = tuple((core, level) for core in CORES for level in LEVELS)
MAX_JSON_BYTES = 512 * 1024
LEARNED_FIELDS = {'event_floor_mv', 'score_threshold', 'interval_tolerance_s'}


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def validate_bundle(bundle):
    """Integrity/contract validation, not a cryptographic claim of trusted provenance."""
    if not isinstance(bundle, dict) or bundle.get('schema') != SCHEMA:
        raise ValueError('不是受支持的参考学习配置')
    body = {k: v for k, v in bundle.items() if k != 'sha256'}
    if digest(body) != bundle.get('sha256'):
        raise ValueError('参考配置校验失败，请恢复默认或重新学习')
    contract = bundle['contract']
    range_code = contract.get('range')
    if type(range_code) is not int or range_code not in MV_PER_CODE or contract != {
            'range': range_code, 'sample_rate': 100000, 'valve_hz': 4, 'duration_ms': 20000}:
        raise ValueError('参考配置采集条件无效')
    if bundle['continuous'] != asdict(ContinuousConfig()):
        raise ValueError('学习不得修改连续时间或中断规则')
    policy, _, _ = check_profile(bundle['detector'])
    defaults = asdict(Policy())
    if any(v != defaults[k] for k, v in asdict(policy).items() if k not in LEARNED_FIELDS):
        raise ValueError('参考配置修改了不可学习的保护参数')
    if policy.event_floor_mv < 5 or policy.score_threshold < 6 or not .005 <= policy.interval_tolerance_s <= .010:
        raise ValueError('学习参数超出保护边界')
    members = bundle['references']
    if len(members) != 9 or {(m['core'], m['level']) for m in members} != set(SLOTS):
        raise ValueError('需要三个核心件各最低、中等、最高三档')
    sources = members + bundle['backgrounds'] + bundle['negative_checks']
    if not bundle['backgrounds'] or len(bundle['backgrounds']) > 12 or len(bundle['negative_checks']) > 24:
        raise ValueError('背景或验证记录数量无效')
    for key in ('sha256', 'waveform_sha256'):
        hashes = [s[key] for s in sources]
        if any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in hashes) or len(set(hashes)) != len(hashes):
            raise ValueError('记录重复或证据哈希无效')
    audit = bundle['validation']
    expected = {core + '/' + level for core, level in SLOTS}
    if set(audit['reference_results']) != expected or any(v != 'REFERENCE_PASS' for v in audit['reference_results'].values()):
        raise ValueError('参考记录未全部通过连续质检验证')
    folds = audit['held_out_core_results']
    if set(folds) != set(CORES):
        raise ValueError('缺少按核心件划分的独立验证')
    for core in CORES:
        if set(folds[core]) != set(LEVELS) or any(v != 'REFERENCE_PASS' for v in folds[core].values()):
            raise ValueError('未参与拟合的核心件未通过验证')
    expected_checks = {s['sha256'] for s in bundle['backgrounds'] + bundle['negative_checks']}
    if set(audit['control_results']) != expected_checks or any(v not in ('RETEST', 'NOT_PASSED') for v in audit['control_results'].values()):
        raise ValueError('背景或验证记录被错误放行或数据无效')
    if not isinstance(bundle['name'], str) or not bundle['name'].strip() or len(bundle['name']) > 80:
        raise ValueError('参考配置名称无效')
    return bundle


def read_record(path):
    """Use the desktop's bounded reader; ignore old saved QC labels."""
    from .qc_engine import feature_arrays
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('会话文件过大：' + path.name)
    before = sha256(path)
    capture = load_capture(path)
    after = sha256(path)
    if before != after:
        raise ValueError('读取过程中记录发生变化：' + path.name)
    meta = capture['metadata']
    if meta.get('mode') != 'serial' or meta.get('range') not in MV_PER_CODE:
        raise ValueError('必须使用真实设备的 ±2.560 V / ±5.120 V 记录：' + path.name)
    raw = np.asarray(capture['values'], dtype=np.uint16)
    indices = np.asarray(capture['indices'], dtype=np.uint32)
    integrity = validate_capture({'format': 'coret.binary/2', 'schema': capture['schema'], 'metadata': meta},
                                 raw, indices, Config(range_code=meta['range'], target_hz=4))
    if not integrity['eligible_for_profile'] or len(raw) != 2000000:
        raise ValueError('需要完整有效的 20 秒 / 4 Hz / 100 kS/s 记录：' + path.name)
    blocks = raw.reshape(-1, 100).astype(float) * MV_PER_CODE[meta['range']]
    f = feature_arrays(blocks.min(1), blocks.max(1), Policy())
    wave_hash = hashlib.sha256(raw.astype('<u2').tobytes() + indices.astype('<u4').tobytes()).hexdigest()
    f.update(file=str(path.resolve()), sha256=before, waveform_sha256=wave_hash,
             metadata=meta, duration_s=20., integrity=integrity)
    return f


def outcome(features, profile):
    policy, _, _ = check_profile(profile)
    return assess_continuous(detect(features, policy), features['duration_s'], profile, ContinuousConfig())['result']


def learn_reference(references, backgrounds, negatives=(), name='合格件参考', progress=lambda text: None):
    """Exactly 3 x 3 labeled references; controls never expand acceptance bounds."""
    if len(references) != 9 or {(r['core'], r['level']) for r in references} != set(SLOTS):
        raise ValueError('请补齐三个核心件各三档，共 9 条记录')
    if not 1 <= len(backgrounds) <= 12 or len(negatives) > 24:
        raise ValueError('需要 1～12 条背景记录，验证记录最多 24 条')
    all_paths = [r['path'] for r in references] + list(backgrounds) + list(negatives)
    loaded = []
    for i, path in enumerate(all_paths):
        progress(f'检查记录 {i + 1}/{len(all_paths)}：{Path(path).name}')
        loaded.append(read_record(path))
    ranges = {f['metadata']['range'] for f in loaded}
    if len(ranges) != 1:
        raise ValueError('同一参考配置的学习、背景和验证记录须使用同一量程；更换量程请单独学习')
    for key in ('sha256', 'waveform_sha256'):
        if len({f[key] for f in loaded}) != len(loaded):
            raise ValueError('记录或原始波形重复，不能把同一记录当作不同核心件证据')
    ref_features = loaded[:9]
    background_features = loaded[9:9 + len(backgrounds)]
    control_features = loaded[9:]
    progress('根据合格件和背景生成候选参数')
    profile = build_profile(ref_features, background_features)
    replay = {r['core'] + '/' + r['level']: outcome(f, profile) for r, f in zip(references, ref_features)}
    if any(v != 'REFERENCE_PASS' for v in replay.values()):
        raise ValueError('参考样本未通过连续 10 秒规则，请检查：' + str(replay))
    folds = {}
    for core in CORES:
        progress('独立验证未参与学习的核心件：' + core)
        train = [f for r, f in zip(references, ref_features) if r['core'] != core]
        held_out_profile = build_profile(train, background_features)
        folds[core] = {r['level']: outcome(f, held_out_profile) for r, f in zip(references, ref_features) if r['core'] == core}
        if any(v != 'REFERENCE_PASS' for v in folds[core].values()):
            raise ValueError('跨核心件验证未通过，不启用新配置：' + core + ' ' + str(folds[core]))
        for f in control_features:
            if outcome(f, held_out_profile) not in ('RETEST', 'NOT_PASSED'):
                raise ValueError('独立验证错误放行背景或非合格记录：' + f['file'])
    controls = {f['sha256']: outcome(f, profile) for f in control_features}
    if any(v not in ('RETEST', 'NOT_PASSED') for v in controls.values()):
        raise ValueError('候选参数错误放行背景或非合格验证记录，旧配置不变')
    def evidence(f):
        return {k: f[k] for k in ('file', 'sha256', 'waveform_sha256')}
    bundle = {'schema': SCHEMA, 'name': name.strip(), 'created_at': datetime.now(timezone.utc).isoformat(),
              'contract': {'range': ranges.pop(), 'sample_rate': 100000, 'valve_hz': 4, 'duration_ms': 20000},
              'continuous': asdict(ContinuousConfig()), 'detector': profile,
              'references': [dict(evidence(f), core=r['core'], level=r['level']) for r, f in zip(references, ref_features)],
              'backgrounds': [evidence(f) for f in background_features],
              'negative_checks': [evidence(f) for f in loaded[9 + len(backgrounds):]],
              'validation': {'reference_results': replay, 'held_out_core_results': folds, 'control_results': controls},
              'limitations': ['核心件编号与合格标签由操作者确认，软件不能核实物理身份。',
                             '三档合并为允许范围，不推算物理力值，也不判定具体档位。',
                             '仅为试运行参考；未提供的缺陷类型没有验证。',
                             '本功能不自动切换 ADC 量程，不在待检件上继续学习。']}
    bundle['sha256'] = digest(bundle)
    validate_bundle(bundle)
    progress('学习及验证完成；尚未启用')
    return bundle


class ProfileStore:
    """Atomic candidate/active files; a failed fit/save never overwrites active state."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.candidate_path = self.directory / 'qc-reference-candidate.json'
        self.active_path = self.directory / 'qc-reference-active.json'

    @staticmethod
    def read(path):
        path = Path(path)
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError('参考配置文件过大')
        return json.loads(path.read_text(encoding='utf-8'))

    def load_active(self):
        if not self.active_path.exists():
            return None
        envelope = self.read(self.active_path)
        if not isinstance(envelope, dict) or envelope.get('schema') != SCHEMA or 'active' not in envelope:
            raise ValueError('启用状态文件无效，请明确恢复默认配置')
        return deepcopy(validate_bundle(envelope['active'])) if envelope['active'] is not None else None

    def load_candidate(self):
        return validate_bundle(self.read(self.candidate_path))

    def save_candidate(self, bundle):
        validate_bundle(bundle)
        data = canonical(bundle)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError('参考配置文件过大')
        atomic_write(self.candidate_path, data)

    def activate(self, bundle):
        validate_bundle(bundle)
        data = canonical({'schema': SCHEMA, 'active': bundle})
        if len(data) > MAX_JSON_BYTES:
            raise ValueError('参考配置文件过大')
        atomic_write(self.active_path, data)

    def restore_default(self):
        atomic_write(self.active_path, canonical({'schema': SCHEMA, 'active': None}))
