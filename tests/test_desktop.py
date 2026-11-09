import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QMessageBox, QGroupBox
from coret.storage import SCHEMA, load_capture, save_capture, export_csv
from coret.plot import envelope
from coret.window import MainWindow
from coret.theme import apply_theme


def sample():
    return {'schema':SCHEMA,'indices':[0,1,2],'values':[32768,33259,33749],
            'metadata':{'sample_rate':100000,'samples':3,'crc_errors':0,'index_gaps':0,
                'bad_headers':0,'garbage_bytes':0,'passed':True,'errors':[],'events':[],
                'mode':'simulator','timestamp':'2026-10-30T00:00:00+08:00'},'rx_hex':'','tx_hex':''}


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def test_roundtrip_and_csv(self):
        obj=sample(); p=self.root/'test.coret'; save_capture(p,obj)
        loaded=load_capture(p); loaded['indices']=list(loaded['indices']); loaded['values']=list(loaded['values']); self.assertEqual(loaded,obj)
        export_csv(self.root/'test.csv',obj)
        self.assertEqual((self.root/'test.csv').read_text().splitlines()[2],'1,0.00001000,33259')
    def test_invalid_schema(self):
        p=self.root/'bad.coret'; p.write_text('{"schema":"future"}')
        with self.assertRaises(ValueError): load_capture(p)
    def test_reject_nan(self):
        obj=sample(); obj['metadata']['sample_rate']=float('nan')
        with self.assertRaises(ValueError): save_capture(self.root/'x',obj)
    def test_reject_wrong_sample_count(self):
        obj=sample(); obj['values'].append(1)
        with self.assertRaises(ValueError): save_capture(self.root/'x',obj)
    def test_reject_code_and_bool(self):
        for value in (65536,-1,True,1.1):
            obj=sample(); obj['values'][0]=value
            with self.assertRaises(ValueError): save_capture(self.root/'x',obj)
    def test_invalid_hex(self):
        obj=sample(); obj['rx_hex']='no'
        with self.assertRaises(ValueError): save_capture(self.root/'x',obj)
    def test_failed_save_does_not_destroy_old_file(self):
        p=self.root/'keep.coret'; save_capture(p,sample()); previous=p.read_bytes()
        with patch('coret.storage.os.replace',side_effect=OSError('simulated disk error')):
            with self.assertRaises(OSError): save_capture(p,sample())
        self.assertEqual(p.read_bytes(),previous)
        self.assertEqual(list(self.root.iterdir()),[p])
    def test_failed_capture_remains_inspectable(self):
        obj=sample(); obj['indices']=[0,1,5];obj['metadata']['passed']=False;obj['metadata']['index_gaps']=1
        p=self.root/'gap.coret';save_capture(p,obj)
        self.assertEqual(list(load_capture(p)['indices']),[0,1,5])


class PlotTests(unittest.TestCase):
    def test_envelope_keeps_spike(self):
        values=[10]*20000;values[4999]=65535
        result=envelope(list(range(len(values))),values,0,len(values),500)
        self.assertIn((4999,65535),result[0]);self.assertLess(len(result[0]),510)
    def test_gap_not_joined(self):
        result=envelope([0,1,20,21],[1,2,3,4],0,21,2)
        self.assertEqual(len(result),2)
        self.assertEqual(result[0][-1],(1,2));self.assertEqual(result[1][0],(20,3))
    def test_zoom_window_includes_bracketing_samples(self):
        result=envelope(list(range(100)),list(range(100)),20.1,25.9)
        self.assertEqual(result[0][0][0],20);self.assertEqual(result[0][-1][0],26)


APP=QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
apply_theme(APP)


def wait_until(predicate,timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        APP.processEvents()
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('GUI condition timed out')


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.w=MainWindow(self.root);self.w.show()
        wait_until(lambda:self.w.worker.controller is not None)
    def tearDown(self):
        if self.w.thread.isRunning():
            self.w.dirty=False;self.w.begin_shutdown()
            wait_until(lambda:not self.w.thread.isRunning())
        self.w.deleteLater();APP.processEvents();self.tmp.cleanup()
    def connect(self):
        self.w.mode.setCurrentIndex(self.w.mode.findData('simulator'))
        self.w.connect_button.click();wait_until(lambda:self.w.state['phase']=='idle' and not self.w.busy)
    def test_initial_state_no_auto_device_or_start(self):
        self.assertFalse(self.w.state['connected'])
        self.assertFalse(self.w.start_button.isEnabled())
        self.assertFalse(self.w.save_action.isEnabled())
    def test_controls_stop_and_save(self):
        self.connect();self.w.duration.setCurrentIndex(1);self.w.start_button.click()
        wait_until(lambda:self.w.state.get('samples',0)>=5000)
        self.assertFalse(self.w.connect_button.isEnabled())
        self.assertFalse(self.w.start_button.isEnabled())
        self.w.stop_button.click();wait_until(lambda:self.w.capture is not None)
        self.assertTrue(self.w.capture['metadata']['passed'])
        self.w.save_to(self.root/'saved.coret');self.assertFalse(self.w.dirty)
    def test_replay_does_not_connect(self):
        p=self.root/'replay.coret';save_capture(p,sample())
        self.w.load_from(p);APP.processEvents()
        self.assertTrue(self.w.replaying);self.assertFalse(self.w.state['connected'])
        self.assertIn('离线',self.w.badge.text())
    def test_cancel_does_not_replace_unsaved_data(self):
        self.w.capture=sample();self.w.dirty=True
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Cancel):
            self.w.connect_source()
        APP.processEvents();self.assertFalse(self.w.state['connected']);self.assertTrue(self.w.dirty)
    def test_active_exit_recovers_capture_and_stops(self):
        self.connect();self.w.duration.setCurrentIndex(1);self.w.start_button.click()
        wait_until(lambda:self.w.state.get('samples',0)>=5000)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes): self.w.close()
        wait_until(lambda:self.w.allow_close)
        files=list(self.root.glob('*-exit-recovery.coret'));self.assertEqual(len(files),1)
        capture=load_capture(files[0]);self.assertTrue(capture['metadata']['passed'])
        self.assertFalse(self.w.worker.controller.snapshot()['connected'])
    def test_sidebar_widgets_do_not_overlap_at_min_size(self):
        self.w.resize(990,740);APP.processEvents()
        for box in self.w.findChildren(QGroupBox):
            layout=box.layout(); previous=None
            for i in range(layout.count()):
                item=layout.itemAt(i)
                widget=item.widget()
                if widget and widget.isVisible():
                    rect=widget.geometry()
                    if previous is not None:self.assertGreaterEqual(rect.top(),previous.bottom(),box.title())
                    previous=rect


if __name__=='__main__': unittest.main(verbosity=2)
