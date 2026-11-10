"""QC integration tests. No serial connections or physical outputs."""
from array import array
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from coret.runtime import prepare_headless_fonts
prepare_headless_fonts()
from PySide6.QtWidgets import QApplication
from coret.qc_engine import QCEngine
from coret.storage import save_capture, load_capture
from coret.window import MainWindow

APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def fixture(kind='normal'):
    times=.02+np.arange(79)*.25
    if kind=='interrupted':times=np.r_[times[:24],times[28:]]
    if kind=='removed_after_pass':times=times[:52]
    if kind=='noise':times=[]
    raw=np.full(2000000,32000,dtype=np.uint16)
    for t in times:
        start=round(t*100000);raw[start:start+80]+=2560
    m={'sample_rate':100000,'range':4,'valve_hz':4,'valve_high_us':40000,'valve_control':'measurement_linked',
       'samples':len(raw),'target_samples':len(raw),'end':{'stored_samples':len(raw)},
       'crc_errors':0,'bad_headers':0,'index_gaps':0,'garbage_bytes':0,'errors':[],'events':[],
       'passed':True,'measurement_complete':True,'early_stop':False,'start_ack':0,'mode':'serial','phase':'idle'}
    return {'schema':'coret.capture/1','metadata':m,'values':array('H',raw),'indices':array('I',range(len(raw))),
            'rx_hex':'','tx_hex':''}


def feed(engine,capture,chunk=8000):
    results=[]
    m=dict(capture['metadata'],phase='running',passed=None,measurement_complete=False)
    for i in range(0,len(capture['values']),chunk):
        r=engine.feed(m,(i,capture['indices'][i:i+chunk],capture['values'][i:i+chunk]))
        if r:results.append(r)
    return results


def wait(predicate,timeout=5):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        APP.processEvents()
        if predicate():return
        time.sleep(.005)
    raise AssertionError('QC GUI wait timed out')


class EngineTests(unittest.TestCase):
    def test_preview_never_returns_final_pass(self):
        c=fixture();e=QCEngine();results=feed(e,c)
        self.assertTrue(any(r['result']=='TESTING' for r in results))
        self.assertTrue(any(r['result']=='AWAITING_VALIDATION' for r in results))
        self.assertFalse(any(r['result']=='REFERENCE_PASS' for r in results))
        self.assertEqual(e.finish(c)['result'],'REFERENCE_PASS')

    def test_irregular_chunk_boundaries_same_final_as_whole_capture(self):
        c=fixture();e=QCEngine();feed(e,c,7777)
        r=e.finish(c);direct=QCEngine().finish(c)
        self.assertEqual(r['result'],direct['result'])
        self.assertEqual(r['terminal_at_s'],direct['terminal_at_s'])
        self.assertEqual(r['windows'],direct['windows'])

    def test_interruption_latched_after_recovery(self):
        c=fixture('interrupted');e=QCEngine();results=feed(e,c)
        first=next(i for i,r in enumerate(results) if r['result']=='NOT_PASSED')
        self.assertTrue(all(r['result']=='NOT_PASSED' for r in results[first:]))
        self.assertEqual(e.finish(c)['result'],'NOT_PASSED')

    def test_removal_after_completion_does_not_reverse(self):
        c=fixture('removed_after_pass');e=QCEngine();feed(e,c)
        self.assertEqual(e.finish(c)['result'],'REFERENCE_PASS')

    def test_integrity_failure_overrides_preview_completion(self):
        c=fixture();e=QCEngine();feed(e,c);c['metadata']['passed']=False
        self.assertEqual(e.finish(c)['result'],'INVALID_DATA')

    def test_early_stop_is_not_final_pass(self):
        c=fixture();c['metadata'].update(early_stop=True,measurement_complete=False)
        self.assertEqual(QCEngine().finish(c)['result'],'INVALID_DATA')

    def test_noise_cannot_pass(self):
        c=fixture('noise');e=QCEngine();feed(e,c)
        self.assertEqual(e.finish(c)['result'],'RETEST')

    def test_simulator_and_unsupported_settings_never_pass(self):
        for key,value in [('mode','simulator'),('range',0),('valve_hz',5),('sample_rate',50000)]:
            c=fixture();c['metadata'][key]=value
            self.assertEqual(QCEngine().finish(c)['result'],'NOT_SUPPORTED')

    def test_missing_chunk_and_clipping_fail_closed(self):
        c=fixture();e=QCEngine()
        r=e.feed(c['metadata'],(100,c['indices'][100:200],c['values'][100:200]))
        self.assertEqual(r['result'],'INVALID_DATA')
        self.assertEqual(e.finish(c)['result'],'INVALID_DATA')
        c['values'][5]=65535
        self.assertEqual(QCEngine().finish(c)['result'],'INVALID_DATA')

    def test_reset_is_required_for_new_trial(self):
        e=QCEngine();c=fixture('interrupted');feed(e,c);self.assertEqual(e.finish(c)['result'],'NOT_PASSED')
        self.assertIsNone(e.feed(c['metadata'],(0,c['indices'][:100],c['values'][:100])))
        e.reset();self.assertEqual(e.finish(fixture())['result'],'REFERENCE_PASS')

    def test_stored_forged_pass_is_ignored(self):
        c=fixture('noise');c['metadata']['product_qc']={'result':'REFERENCE_PASS'}
        self.assertEqual(QCEngine().finish(c)['result'],'RETEST')

    def test_prefix_change_fails_closed(self):
        c=fixture();e=QCEngine();feed(e,c)
        e.committed[0]['time_s']+=.01
        self.assertEqual(e.finish(c)['result'],'INVALID_DATA')


class QCWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.w=MainWindow(self.temp.name);self.w.show()
        wait(lambda:self.w.worker.controller is not None)

    def tearDown(self):
        if self.w.thread.isRunning():
            self.w.dirty=False;self.w.begin_shutdown();wait(lambda:self.w.allow_close)
        self.assertFalse(self.w.qc_thread.isRunning())
        self.w.deleteLater();APP.processEvents();self.temp.cleanup()

    def test_fixed_start_does_not_use_operator_values(self):
        self.w.state.update(phase='idle',mode='serial',connected=True)
        self.w.duration.setCurrentIndex(0);self.w.valve_frequency.setCurrentIndex(5)
        with patch.object(self.w,'command') as command:
            self.w.start_qc();command.assert_called_once_with('start',range=4,duration_ms=20000,valve_hz=4)
        self.assertTrue(self.w._next_qc_requested)

    def test_simulator_qc_start_disabled(self):
        self.w.state.update(phase='idle',mode='simulator',connected=True);self.w.update_actions()
        self.assertFalse(self.w.qc_button.isEnabled())
        with patch.object(self.w,'command') as command:self.w.start_qc();command.assert_not_called()

    def test_stale_result_does_not_replace_new_trial(self):
        self.w.reset_qc(True);generation=self.w.qc_generation;self.w.reset_qc(True)
        self.w.on_qc_update(generation,{'result':'REFERENCE_PASS','final':True})
        self.assertIsNone(self.w.qc_result)
        self.assertNotIn('合格',self.w.qc_status.text())

    def test_preview_pass_is_not_green_final_verdict(self):
        self.w.reset_qc(True)
        self.w.on_qc_update(self.w.qc_generation,{'result':'REFERENCE_PASS','final':False})
        self.assertIn('待校验',self.w.qc_status.text())

    def test_replay_async_result_and_saved_metadata(self):
        c=fixture();source=Path(self.temp.name)/'source.coret';save_capture(source,c)
        self.w.load_from(source);self.assertTrue(self.w.qc_final_pending)
        wait(lambda:not self.w.qc_final_pending)
        self.assertEqual(self.w.qc_result['result'],'REFERENCE_PASS')
        self.assertIn('回放',self.w.qc_status.text());self.assertIn('合格',self.w.qc_status.text())
        saved=Path(self.temp.name)/'result.coret';self.w.save_to(saved)
        self.assertEqual(load_capture(saved)['metadata']['product_qc']['result'],'REFERENCE_PASS')
        self.assertNotIn('product_qc',load_capture(source)['metadata'])

    def test_failure_label_and_no_operator_threshold_widgets(self):
        self.w.reset_qc(True)
        self.w.on_qc_update(self.w.qc_generation,{'result':'NOT_PASSED','final':True,'message':'出击中断；原因未知'})
        self.assertIn('不合格',self.w.qc_status.text());self.assertIn('原因未知',self.w.qc_hint.text())
        self.assertFalse(hasattr(self.w,'qc_threshold_spinbox'))

    def test_pending_final_prevents_new_trial_and_save(self):
        self.w.capture=fixture();self.w.qc_final_pending=True
        self.w.state.update(phase='idle',connected=True,mode='serial');self.w.update_actions()
        self.assertFalse(self.w.qc_button.isEnabled());self.assertFalse(self.w.save_action.isEnabled())
        with self.assertRaises(ValueError):self.w.save_to(Path(self.temp.name)/'premature.coret')

    def test_stream_error_cannot_be_overwritten_by_success(self):
        self.w.reset_qc(True);self.w.qc_stream_error=True
        self.w.on_qc_update(self.w.qc_generation,{'result':'REFERENCE_PASS','final':True})
        self.assertEqual(self.w.qc_result['result'],'INVALID_DATA')


if __name__=='__main__':unittest.main()
