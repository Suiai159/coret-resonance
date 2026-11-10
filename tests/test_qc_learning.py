"""Optional learning tests: synthetic references, no device connection or original data writes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QMessageBox
from test_qc import APP, fixture, feed, wait
from coret.storage import save_capture, load_capture
from coret.qc_learning import (SLOTS, CORES, LEVELS, ProfileStore, learn_reference, read_record,
                               validate_bundle, digest)
from coret.qc_learning_dialog import LearningDialog
from coret.qc_engine import QCEngine
from coret.qc_worker import QCWorker
from coret.window import MainWindow


def seal(bundle):
    bundle['sha256'] = digest({k: v for k, v in bundle.items() if k != 'sha256'})
    return bundle


class LearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        cls.references = []
        for i, (core, level) in enumerate(SLOTS):
            capture = fixture()
            raw = np.frombuffer(capture['values'], dtype=np.uint16)
            peak = raw > 32000
            raw[:] = 32000 + i
            raw[peak] += (1600, 2560, 4000)[LEVELS.index(level)] + (i // 3) * 10
            path = cls.root / f'{core}_{level}.coret'; save_capture(path, capture)
            cls.references.append(dict(core=core, level=level, path=str(path)))
        background = fixture('noise')
        # Realistic nonconstant baseline, far below any reference impact.
        noise = np.frombuffer(background['values'], dtype=np.uint16)
        noise += np.arange(len(noise), dtype=np.uint32).astype(np.uint16) % 9
        cls.background = cls.root / 'background.coret'; save_capture(cls.background, background)
        cls.negative = cls.root / 'interrupted.coret'; save_capture(cls.negative, fixture('interrupted'))
        cls.bundle = learn_reference(cls.references, [cls.background], [cls.negative], '三件三档试验')

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.out = tempfile.TemporaryDirectory(); self.store = ProfileStore(self.out.name)

    def tearDown(self): self.out.cleanup()

    def test_three_by_three_and_held_out_validation(self):
        b = validate_bundle(self.bundle)
        self.assertEqual(len(b['references']), 9)
        self.assertEqual(set(b['validation']['held_out_core_results']), set(CORES))
        self.assertEqual(set(b['validation']['reference_results'].values()), {'REFERENCE_PASS'})
        self.assertEqual(b['continuous'], {'required_continuous_s': 10., 'max_silence_periods': 1.5})
        self.assertEqual(b['contract']['range'], 4)
        self.assertEqual(len(b['negative_checks']), 1)

    def test_missing_slot_or_background_rejected(self):
        with self.assertRaises(ValueError): learn_reference(self.references[:-1], [self.background])
        with self.assertRaises(ValueError): learn_reference(self.references, [])
        refs = deepcopy(self.references); refs[0]['level'] = 'mid'
        with self.assertRaises(ValueError): learn_reference(refs, [self.background])

    def test_duplicate_waveform_under_changed_metadata_rejected(self):
        duplicate = fixture()
        # Exactly the first reference waveform; different filename and metadata are not independent evidence.
        raw = np.frombuffer(duplicate['values'], dtype=np.uint16); raw[raw > 32000] = 33600
        duplicate['metadata']['timestamp'] = 'different'
        path = Path(self.out.name) / 'duplicate.coret'; save_capture(path, duplicate)
        refs = deepcopy(self.references); refs[3]['path'] = path
        with self.assertRaisesRegex(ValueError, '重复'): learn_reference(refs, [self.background])

    def test_bad_integrity_or_simulator_not_training_data(self):
        for key, value in [('mode', 'simulator'), ('crc_errors', 1), ('range', 7), ('valve_hz', 5)]:
            c = fixture(); c['metadata'][key] = value
            path = Path(self.out.name) / 'bad.coret'; save_capture(path, c)
            with self.assertRaises(ValueError): read_record(path)

    def test_clipped_reference_rejected(self):
        c = fixture(); c['values'][123] = 65535
        path = Path(self.out.name) / 'clipped.coret'; save_capture(path, c)
        with self.assertRaises(ValueError): read_record(path)

    def test_mixed_ranges_rejected(self):
        c = fixture(); c['metadata']['range'] = 3
        path = Path(self.out.name) / 'range3.coret'; save_capture(path, c)
        refs = deepcopy(self.references); refs[0]['path'] = path
        with self.assertRaisesRegex(ValueError, '同一量程'): learn_reference(refs, [self.background])

    def test_learning_at_range3_uses_that_contract(self):
        refs = deepcopy(self.references)
        for r in refs:
            c = load_capture(r['path']); c['metadata']['range'] = 3
            raw = np.frombuffer(c['values'], dtype=np.uint16)
            raw[:] = 32000 + (raw.astype(np.int32) - 32000) // 2
            path = Path(self.out.name) / Path(r['path']).name
            save_capture(path, c); r['path'] = str(path)
        c = load_capture(self.background); c['metadata']['range'] = 3
        path = Path(self.out.name) / 'background3.coret'; save_capture(path, c)
        b = learn_reference(refs, [path])
        self.assertEqual(b['contract']['range'], 3)
        self.assertEqual(QCEngine(b).finish(load_capture(refs[1]['path']))['result'], 'REFERENCE_PASS')

    def test_all_nine_ranges_learn_from_unicode_paths(self):
        from coret.protocol import RANGES
        from coret.qc_algorithm.coret_qc_v2 import MV_PER_CODE
        for code in RANGES:
            with self.subTest(range_code=code):
                directory = Path(self.out.name) / f'中文 学习量程 {code}'
                directory.mkdir()
                refs = deepcopy(self.references)
                controls = []
                for i, source in enumerate([r['path'] for r in refs] + [self.background, self.negative]):
                    c = load_capture(source)
                    raw = np.frombuffer(c['values'], dtype=np.uint16)
                    raw[:] = np.rint(32000 + (raw.astype(float) - 32000) * MV_PER_CODE[4] / MV_PER_CODE[code]).astype(np.uint16)
                    c['metadata']['range'] = code
                    path = directory / f'记录 {i}.coret'
                    save_capture(path, c)
                    if i < 9: refs[i]['path'] = str(path)
                    else: controls.append(path)
                b = learn_reference(refs, controls[:1], controls[1:], f'中文 参考量程 {code}')
                self.assertEqual(b['contract']['range'], code)
                self.assertEqual(QCEngine(b).finish(load_capture(refs[1]['path']))['result'], 'REFERENCE_PASS')
                self.assertEqual(QCEngine(b).finish(load_capture(controls[1]))['result'], 'NOT_PASSED')

    def test_unicode_candidate_export_import_and_restore_default(self):
        from PySide6.QtWidgets import QFileDialog
        path = Path(self.out.name) / '中文 空格配置.json'
        d = LearningDialog(self.store, None)
        try:
            d.completed(self.bundle)
            with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(path), 'JSON')):
                d.export_candidate()
            self.assertEqual(validate_bundle(ProfileStore.read(path)), self.bundle)
            d.candidate = None
            with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), 'JSON')):
                d.import_candidate()
            self.assertEqual(d.candidate, self.bundle)
            self.assertIsNone(self.store.load_active())
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                d.activate()
                d.restore_default()
            self.assertIsNone(self.store.load_active())
            self.assertEqual(self.store.load_candidate(), self.bundle)
        finally: d.deleteLater(); APP.processEvents()

    def test_good_reference_falsely_labeled_negative_blocks_candidate(self):
        c = fixture(); c['values'][0] += 1
        path = Path(self.out.name) / 'not_negative.coret'; save_capture(path, c)
        with self.assertRaisesRegex(ValueError, '错误放行'):
            learn_reference(self.references, [self.background], [path])

    def test_held_out_outlier_blocks_candidate(self):
        c = fixture(); raw = np.frombuffer(c['values'], dtype=np.uint16); raw[raw > 32000] = 32800
        path = Path(self.out.name) / 'outlier.coret'; save_capture(path, c)
        refs = deepcopy(self.references); refs[6]['path'] = path
        with self.assertRaisesRegex(ValueError, '跨核心件验证未通过'):
            learn_reference(refs, [self.background])

    def test_candidate_does_not_activate_and_reset_keeps_candidate(self):
        self.assertIsNone(self.store.load_active())
        self.store.save_candidate(self.bundle)
        self.assertIsNone(self.store.load_active())
        self.store.activate(self.store.load_candidate())
        self.assertEqual(self.store.load_active()['sha256'], self.bundle['sha256'])
        self.store.restore_default(); self.assertIsNone(self.store.load_active())
        self.assertEqual(self.store.load_candidate()['sha256'], self.bundle['sha256'])

    def test_failed_activation_or_write_does_not_replace_active(self):
        self.store.activate(self.bundle); old = self.store.active_path.read_bytes()
        bad = deepcopy(self.bundle); bad['contract']['range'] = 3
        with self.assertRaises(ValueError): self.store.activate(bad)
        self.assertEqual(self.store.active_path.read_bytes(), old)
        with patch('coret.qc_learning.atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.store.restore_default()
        self.assertEqual(self.store.active_path.read_bytes(), old)

    def test_corrupt_active_is_not_silent_default(self):
        self.store.active_path.write_text('{invalid')
        with self.assertRaises(ValueError): self.store.load_active()

    def test_modified_protection_rejected_even_with_new_checksum(self):
        changes = [('continuous', 'required_continuous_s', 4.), ('continuous', 'max_silence_periods', 1.9),
                   ('contract', 'valve_hz', 3)]
        for section, field, value in changes:
            b = deepcopy(self.bundle); b[section][field] = value
            with self.assertRaises(ValueError): validate_bundle(seal(b))
        for field, value in [('min_stable_events', 3), ('score_threshold', 2), ('interval_tolerance_s', .08)]:
            b = deepcopy(self.bundle); b['detector']['policy'][field] = value
            with self.assertRaises(ValueError): validate_bundle(seal(b))

    def test_missing_or_failed_validation_cannot_activate(self):
        b = deepcopy(self.bundle); b['validation']['held_out_core_results']['core01']['low'] = 'RETEST'
        with self.assertRaises(ValueError): validate_bundle(seal(b))
        b = deepcopy(self.bundle); b['validation']['control_results'] = {}
        with self.assertRaises(ValueError): validate_bundle(seal(b))

    def test_engine_uses_snapshot_and_never_changes_candidate(self):
        b = deepcopy(self.bundle); e = QCEngine(b); b['detector']['allowed_window_median_mv'][0] = 1000
        c = fixture(); previews = feed(e, c)
        self.assertNotIn('REFERENCE_PASS', [r['result'] for r in previews])
        result = e.finish(c)
        self.assertEqual(result['result'], 'REFERENCE_PASS')
        self.assertEqual(result['reference_profile'], self.bundle)
        self.assertEqual(self.bundle, validate_bundle(self.bundle))
        self.assertEqual(QCEngine(self.bundle).finish(fixture('interrupted'))['result'], 'NOT_PASSED')

    def test_learned_range_mismatch_never_passes(self):
        c = fixture(); c['metadata']['range'] = 3
        self.assertEqual(QCEngine(self.bundle).finish(c)['result'], 'NOT_SUPPORTED')
        # Same mV waveform and parameters, profile contract explicitly range 3.
        b = deepcopy(self.bundle); b['contract']['range'] = 3; seal(b)
        raw = np.frombuffer(c['values'], dtype=np.uint16); raw[raw > 32000] = 33280
        self.assertEqual(QCEngine(b).finish(c)['result'], 'REFERENCE_PASS')

    def test_worker_replay_does_not_reset_to_builtin_profile(self):
        w = QCWorker(); results = []; w.updated.connect(lambda generation, result: results.append(result))
        w.reset(3, self.bundle); w.finish(3, fixture(), True)
        self.assertEqual(results[-1]['reference_sha256'], self.bundle['sha256'])
        self.assertEqual(results[-1]['result'], 'REFERENCE_PASS')
        w.reset(4, {'_load_error': 'bad active profile'}); w.finish(4, fixture(), True)
        self.assertEqual(results[-1]['result'], 'INVALID_DATA')

    def test_gui_default_is_optional_and_corrupt_profile_blocks_only_qc(self):
        self.store.active_path.write_text('bad')
        w = MainWindow(self.out.name)
        try:
            wait(lambda: w.worker.controller is not None)
            w.state.update(phase='idle', connected=True, mode='serial'); w.update_actions()
            self.assertFalse(w.qc_action.isEnabled()); self.assertTrue(w.start_action.isEnabled())
            self.assertFalse(w.learning_action.isEnabled())
            with patch.object(w, 'command') as command: w.start_qc(); command.assert_not_called()
            w.state.update(phase='disconnected', connected=False); w.update_actions()
            self.assertTrue(w.learning_action.isEnabled())
            self.store.restore_default(); w.reload_reference(); w.update_reference_label()
            self.assertIsNone(w.active_reference); self.assertFalse(w.reference_error)
        finally:
            w.dirty = False; w.begin_shutdown(); wait(lambda: w.allow_close); w.deleteLater(); APP.processEvents()

    def test_gui_range_and_replay_use_active_profile(self):
        b = deepcopy(self.bundle); b['contract']['range'] = 3; seal(b); self.store.activate(b)
        w = MainWindow(self.out.name)
        try:
            wait(lambda: w.worker.controller is not None)
            w.state.update(phase='idle', connected=True, mode='serial')
            with patch.object(w, 'command') as command:
                w.range.setCurrentIndex(w.range.findData(4))
                w.start_qc(); command.assert_not_called()
                self.assertEqual(w.range.currentData(), 4)
                w.range.setCurrentIndex(w.range.findData(3))
                w.start_qc(); command.assert_called_once_with('start', range=3, duration_ms=20000, valve_hz=4)
            w.state.update(phase='disconnected', connected=False)
            w.load_from(self.references[0]['path']); wait(lambda: not w.qc_final_pending)
            self.assertEqual(w.qc_result['result'], 'NOT_SUPPORTED')
            self.assertEqual(w.qc_result['reference_sha256'], b['sha256'])
        finally:
            w.dirty = False; w.begin_shutdown(); wait(lambda: w.allow_close); w.deleteLater(); APP.processEvents()

    def test_dialog_incomplete_learning_cannot_start_and_candidate_is_optional(self):
        d = LearningDialog(self.store, None)
        try:
            self.assertFalse(d.activate_button.isEnabled()); d.start_learning(); self.assertIsNone(d.job)
            self.assertIn('补齐', d.report.toPlainText())
            d.completed(self.bundle); self.assertIsNone(self.store.load_active())
            d.set_busy(False); self.assertTrue(d.activate_button.isEnabled())
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes): d.activate()
            self.assertTrue(d.changed); self.assertEqual(self.store.load_active()['sha256'], self.bundle['sha256'])
        finally: d.deleteLater(); APP.processEvents()

    def test_dialog_worker_learning_saves_only_candidate_and_shuts_down(self):
        d = LearningDialog(self.store, None)
        try:
            d.references = {(r['core'], r['level']): r['path'] for r in self.references}
            d.backgrounds = [str(self.background)]; d.negatives = [str(self.negative)]
            d.confirmed.setChecked(True); d.start_learning()
            self.assertTrue(d.running()); self.assertFalse(d.activate_button.isEnabled())
            d.reject(); self.assertTrue(d.running())
            wait(lambda: not d.running(), timeout=20)
            wait(lambda: d.candidate is not None)
            self.assertIsNone(self.store.load_active()); self.assertTrue(self.store.candidate_path.exists())
            self.assertFalse(d.job.isRunning())
        finally:
            if d.running(): d.job.wait(20000)
            d.deleteLater(); APP.processEvents()


if __name__ == '__main__': unittest.main()
