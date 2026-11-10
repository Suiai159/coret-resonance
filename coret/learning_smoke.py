"""Explicit synthetic learning acceptance check. No device I/O or user-data access."""
from array import array
import json
from pathlib import Path
import time

import numpy as np
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication
from .qc_learning import SLOTS, ProfileStore
from .qc_learning_dialog import LearningDialog
from .storage import save_capture


def synthetic_capture(kind='reference', slot=0):
    """Synthetic QA only; never presented as real hardware evidence."""
    raw = np.full(2000000, 32000 + slot, dtype=np.uint16)
    if kind == 'background':
        raw += np.arange(len(raw), dtype=np.uint32).astype(np.uint16) % 9
    else:
        for event in range(79):
            if kind == 'negative' and 24 <= event < 28:
                continue
            start = round((.02 + event * .25) * 100000)
            raw[start:start + 80] += (1600, 2560, 4000)[slot % 3] + (slot // 3) * 10
    meta = dict(sample_rate=100000, range=4, valve_hz=4, valve_high_us=40000,
                valve_control='measurement_linked', samples=len(raw), target_samples=len(raw),
                end={'stored_samples': len(raw)}, crc_errors=0, bad_headers=0, index_gaps=0,
                garbage_bytes=0, errors=[], events=[{'time':'00:00:00', 'text':'合成验证，不是真实设备记录'}],
                passed=True, measurement_complete=True, early_stop=False, start_ack=0,
                mode='serial', phase='idle')
    return dict(schema='coret.capture/1', metadata=meta, values=array('H', raw),
                indices=array('I', range(len(raw))), rx_hex='', tx_hex='')


class LearningCheck(QObject):
    def __init__(self, window, directory):
        super().__init__(window)
        self.window = window
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.dialog = None
        self.stage = 'initial'
        self.deadline = time.monotonic() + 120
        self.exit_code = 1
        self.result = dict(passed=False, synthetic_only=True, physical_serial_opened=False)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        QApplication.instance().setQuitOnLastWindowClosed(False)
        window.thread.finished.connect(lambda: QApplication.instance().exit(self.exit_code))

    def start(self):
        try:
            w = self.window
            if w.data_dir.resolve() != (self.directory / 'user-data').resolve():
                raise ValueError('Learning check requires isolated user data')
            self.dialog = LearningDialog(w.reference_store, w.active_reference, parent=w)
            refs = {}
            records = self.directory / '中文 学习记录'
            records.mkdir(exist_ok=True)
            for i, slot in enumerate(SLOTS):
                path = records / ('_'.join(slot) + '.coret')
                save_capture(path, synthetic_capture(slot=i))
                refs[slot] = str(path)
            self.replay = Path(refs[SLOTS[1]])
            background = records / '背景.coret'
            negative = records / '中断.coret'
            save_capture(background, synthetic_capture('background'))
            save_capture(negative, synthetic_capture('negative'))
            d = self.dialog
            d.references = refs
            for slot, path in refs.items(): d.slot_buttons[slot].setText(Path(path).name)
            d.backgrounds = [str(background)]
            d.negatives = [str(negative)]
            d.background_button.setText('背景记录：1 条')
            d.negative_button.setText('非合格验证记录：1 条（仅验证）')
            d.name.setText('合成三件三档 · Windows 打包验证')
            d.confirmed.setChecked(True)
            d.show()
            d.start_learning()
            if not d.running():
                raise AssertionError(d.report.toPlainText())
            self.stage = 'learning'
            self.timer.start()
        except Exception as e:
            self.finish(e)

    def poll(self):
        try:
            w = self.window
            if w.worker.controller is None or w.worker.controller.port is not None or w.state.get('connected'):
                raise AssertionError('Expected disconnected controller throughout check')
            if time.monotonic() > self.deadline:
                raise TimeoutError('Synthetic learning acceptance check timed out')
            if self.stage == 'learning':
                d = self.dialog
                if d.running(): return
                if d.candidate is None:
                    raise AssertionError(d.report.toPlainText())
                if w.reference_store.load_active() is not None:
                    raise AssertionError('Candidate activated without explicit activation')
                self.result['candidate_only_until_activation'] = True
                self.result['dialog_screenshot'] = d.grab().save(str(self.directory / 'learning.png'))
                candidate = w.reference_store.load_candidate()
                # Explicit test activation only, after candidate generation is verified.
                w.reference_store.activate(candidate)
                w.reload_reference(); w.update_reference_label()
                persisted = ProfileStore(w.data_dir).load_active()
                if persisted != candidate: raise AssertionError('Persistence mismatch')
                self.result['reference_sha256'] = candidate['sha256']
                self.result['persisted'] = True
                self.result['replay_capture'] = str(self.replay)
                d.close()
                w.load_from(self.replay)
                self.stage = 'replay'
            elif self.stage == 'replay':
                if w.qc_final_pending or w.qc_result is None or not w.qc_result.get('final'): return
                if w.qc_result['result'] != 'REFERENCE_PASS': raise AssertionError(w.qc_result)
                if w.qc_result['reference_sha256'] != self.result['reference_sha256']:
                    raise AssertionError('Replay did not use learned profile')
                self.result['replay_result'] = w.qc_result['result']
                self.result['window_screenshot'] = w.grab().save(str(self.directory / 'learned-replay.png'))
                if not self.result['dialog_screenshot'] or not self.result['window_screenshot']:
                    raise AssertionError('Screenshot failed')
                self.result['passed'] = True
                self.finish()
        except Exception as e:
            self.finish(e)

    def finish(self, error=None):
        self.timer.stop()
        if error is not None:
            self.result['error'] = str(error)
            self.result['passed'] = False
        if self.dialog is not None and self.dialog.running():
            # Never destroy a running QThread. Wait asynchronously, not by killing it.
            self.dialog.job.finished.connect(lambda: self.finish(error))
            return
        if self.dialog is not None: self.dialog.close()
        (self.directory / 'learning-check.json').write_text(
            json.dumps(self.result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.exit_code = 0 if self.result['passed'] else 1
        self.window.dirty = False
        self.window.begin_shutdown()
