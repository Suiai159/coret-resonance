"""Opt-in, PC-only acceptance checks; never open a physical COM port."""
import argparse
from array import array
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def reference_capture(interrupted=False, range_code=4):
    # Synthetic reference only. Matches the existing QC regression fixture.
    import numpy as np
    from coret.qc_algorithm.coret_qc_v2 import MV_PER_CODE
    raw = np.full(2000000, 32000, dtype=np.uint16)
    for event in range(79):
        if interrupted and 24 <= event < 28:
            continue
        start = round((.02 + event * .25) * 100000)
        raw[start:start+80] += round(200 / MV_PER_CODE[range_code])
    metadata = dict(sample_rate=100000, range=range_code, valve_hz=4, valve_high_us=40000,
                    valve_control='measurement_linked', samples=len(raw), target_samples=len(raw),
                    end={'stored_samples': len(raw)}, crc_errors=0, bad_headers=0, index_gaps=0,
                    garbage_bytes=0, errors=[], events=[{'time': '00:00:00', 'text': '合成回放验证，不是真实测量'}],
                    passed=True, measurement_complete=True, early_stop=False, start_ack=0,
                    mode='serial', phase='idle')
    return dict(schema='coret.capture/1', metadata=metadata, values=array('H', raw),
                indices=array('I', range(len(raw))), rx_hex='', tx_hex='')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs' / 'Windows 验证')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    package = ROOT / 'dist' / 'CoreT-Desktop'
    # Exercise both the executable and all DLL/data loading from a Unicode/space path.
    relocated = out / '中文 空格目录' / 'CoreT-Desktop'
    if relocated.exists():
        raise SystemExit('Use a new --output directory; an old relocated package already exists.')
    shutil.copytree(package, relocated)
    executable = relocated / 'CoreT-Desktop.exe'
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')

    def run(*arguments, timeout=210):
        # CWD intentionally differs from both source and the EXE directory.
        subprocess.run([str(executable), *map(str, arguments)], cwd=out,
                       env=env, check=True, timeout=timeout)

    smoke = out / '模拟采集'
    run('--smoke-test', smoke)
    report = json.loads((smoke / 'gui-smoke.json').read_text(encoding='utf-8'))
    if not report.get('passed') or report.get('physical_serial_opened') is not False:
        raise AssertionError(report)
    from coret.storage import save_capture
    replays = {}
    from coret.protocol import RANGES
    cases = [(f'量程{code}-合格参考', False, code, 'REFERENCE_PASS') for code in RANGES]
    cases += [(f'量程{code}-中断参考', True, code, 'NOT_PASSED') for code in RANGES]
    for name, interrupted, range_code, expected in cases:
        capture = out / (name + '.coret')
        save_capture(capture, reference_capture(interrupted, range_code))
        evidence = out / name
        run('--open', capture, '--qc-replay-check', evidence, timeout=60)
        result = json.loads((evidence / 'result.json').read_text(encoding='utf-8'))
        if not (result['completed'] and result['no_device_connection'] and result['replaying']
                and result['screenshot_saved'] and result['qc']['result'] == expected):
            raise AssertionError(result)
        replays[name] = result['qc']['result']
    learning = out / '参考学习'
    run('--learning-check', learning, timeout=180)
    learned = json.loads((learning / 'learning-check.json').read_text(encoding='utf-8'))
    if not learned['passed'] or not learned['candidate_only_until_activation'] or not learned['persisted']:
        raise AssertionError(learned)
    restart = out / '学习配置重启回放'
    run('--open', learned['replay_capture'], '--data-dir', learning / 'user-data',
        '--qc-replay-check', restart, timeout=60)
    resumed = json.loads((restart / 'result.json').read_text(encoding='utf-8'))
    if not (resumed['completed'] and resumed['no_device_connection']
            and resumed['qc']['result'] == 'REFERENCE_PASS'
            and resumed['qc']['reference_sha256'] == learned['reference_sha256']):
        raise AssertionError(resumed)
    (out / 'package-check.json').write_text(json.dumps(
        dict(passed=True, unicode_relocation=True, gui_smoke=True, offline_qc=replays,
             learning=True, learned_profile_restart=True, physical_serial_opened=False), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Package checks passed:', out)


if __name__ == '__main__':
    main()
