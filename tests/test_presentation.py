"""Waveform-first UI regression. No physical serial ports are opened."""
from array import array
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coret.runtime import prepare_headless_fonts
prepare_headless_fonts()
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from coret.theme import apply_theme
from coret.window import MainWindow
from coret.plot import Waveform

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
apply_theme(APP)


def wait(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('GUI wait timed out')


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = MainWindow(self.temp.name)
        self.w.show()
        wait(lambda: self.w.worker.controller is not None)
        self.w.activateWindow(); self.w.plot.setFocus(); APP.processEvents()

    def tearDown(self):
        if self.w.thread.isRunning():
            self.w.dirty = False; self.w.begin_shutdown()
            wait(lambda: self.w.allow_close)
        self.w.deleteLater(); APP.processEvents(); self.temp.cleanup()

    def test_clean_default_usb_no_automatic_connection(self):
        self.assertEqual(self.w.mode.currentData(), 'serial')
        self.assertFalse(self.w.state['connected'])
        self.assertFalse(self.w.diagnostics.isVisible())
        self.assertTrue(self.w.sidebar.isVisible())
        self.assertTrue(self.w.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
        self.assertEqual([self.w.duration.itemData(i) for i in range(5)], [5000,10000,15000,20000,30000])
        self.assertFalse(any('0x' in self.w.range.itemText(i) for i in range(9)))

    def test_valve_defaults_and_only_idle_editing(self):
        self.assertEqual([self.w.valve_frequency.itemData(i) for i in range(6)], list(range(1,7)))
        self.assertEqual(self.w.valve_frequency.currentData(), 4)
        self.assertIn('40 ms', self.w.valve_frequency.toolTip())
        for phase in ('syncing','starting','running','stopping','draining'):
            self.w.state.update(phase=phase,connected=True)
            self.w.update_actions()
            self.assertFalse(self.w.valve_frequency.isEnabled())
        self.w.state.update(phase='idle',connected=True)
        self.w.update_actions()
        self.assertTrue(self.w.valve_frequency.isEnabled())

    def test_start_passes_selected_valve_frequency(self):
        self.w.valve_frequency.setCurrentIndex(self.w.valve_frequency.findData(6))
        with patch.object(self.w, 'command') as command:
            self.w.start_capture()
            self.assertEqual(command.call_args.kwargs['valve_hz'], 6)

    def test_waveform_gets_majority_of_vertical_space(self):
        self.w.resize(1280,800); APP.processEvents()
        self.assertGreater(self.w.plot.height(), 570)
        self.assertGreater(self.w.plot.width(), 1000)
        for w in self.w.metric_values.values(): self.assertFalse(w.isVisible())
        for w in self.w.facts.values(): self.assertFalse(w.isVisible())

    def test_small_window_keeps_controls_reachable(self):
        self.w.resize(900,560); APP.processEvents()
        self.assertEqual((self.w.width(),self.w.height()), (900,560))
        for w in (self.w.start_button,self.w.stop_button,self.w.duration,self.w.valve_frequency):
            self.assertTrue(w.isVisible())
            self.assertTrue(self.w.rect().contains(w.mapTo(self.w,w.rect().center())))
        full = self.w.toolbar.widgetForAction(self.w.fullscreen_action)
        self.assertTrue(full.isVisible())
        self.assertTrue(self.w.rect().contains(full.mapTo(self.w,full.rect().center())))
        self.assertGreaterEqual(self.w.plot.height(),290)

    def test_settings_and_log_toggle_leave_data_untouched(self):
        self.w.resize(1280,800); APP.processEvents()
        self.w.plot.set_data([0,1,2],[100,200,100]); original = self.w.plot.values
        width = self.w.plot.width()
        self.w.settings_action.trigger(); APP.processEvents()
        self.assertGreater(self.w.plot.width(),width)
        self.w.log_action.trigger(); APP.processEvents()
        self.assertTrue(self.w.diagnostics.isVisible())
        self.w.log_action.trigger(); self.w.settings_action.trigger(); APP.processEvents()
        self.assertFalse(self.w.diagnostics.isVisible())
        self.assertEqual(self.w.plot.width(),width)
        self.assertIs(self.w.plot.values,original)

    def test_fullscreen_escape_restores_normal_geometry_and_panels(self):
        # Offscreen Qt has an 800px virtual screen; restoreGeometry clamps to it.
        self.w.resize(900,640); self.w.log_action.trigger(); APP.processEvents()
        size = self.w.size()
        QTest.keyClick(self.w.plot, Qt.Key.Key_F11); APP.processEvents()
        self.assertTrue(self.w.isFullScreen())
        self.assertFalse(self.w.sidebar.isVisible())
        self.assertFalse(self.w.diagnostics.isVisible())
        self.assertTrue(self.w.toolbar.isVisible())
        self.assertEqual(self.w.fullscreen_action.text(),'退出全屏')
        QTest.keyClick(self.w.plot, Qt.Key.Key_Escape); APP.processEvents()
        self.assertFalse(self.w.isFullScreen())
        self.assertFalse(self.w.isMaximized())
        self.assertEqual(self.w.size(),size)
        self.assertTrue(self.w.sidebar.isVisible())
        self.assertTrue(self.w.diagnostics.isVisible())
        self.assertTrue(self.w.log_action.isChecked())

    def test_fullscreen_restores_maximized_and_collapsed_settings(self):
        self.w.settings_action.trigger(); self.w.maximize_action.trigger(); APP.processEvents()
        self.assertTrue(self.w.isMaximized())
        self.w.fullscreen_action.trigger(); APP.processEvents()
        self.assertTrue(self.w.isFullScreen())
        self.w.fullscreen_action.trigger(); APP.processEvents()
        self.assertTrue(self.w.isMaximized())
        self.assertFalse(self.w.sidebar.isVisible())
        self.assertFalse(self.w.diagnostics.isVisible())
        self.w.maximize_action.trigger(); APP.processEvents()
        self.assertFalse(self.w.isMaximized())

    def test_external_fullscreen_exit_restores_controls(self):
        self.w.fullscreen_action.trigger(); APP.processEvents()
        self.w.showNormal(); APP.processEvents()
        self.assertFalse(self.w.isFullScreen())
        self.assertFalse(self.w.fullscreen_action.isChecked())
        self.assertTrue(self.w.sidebar.isVisible())
        self.assertTrue(self.w.settings_action.isEnabled())

    def test_error_visible_without_protocol_clutter_including_replay(self):
        s={'phase':'error','passed':False,'errors':['CRC fault; START ACK missing'],
           'events':[{'time':'12:00','text':'CRC fault; START ACK missing'}]}
        for replay in (False,True):
            self.w.replaying = replay; self.w.render_metadata(s); APP.processEvents()
            self.assertFalse(self.w.diagnostics.isVisible())
            self.assertTrue(self.w.notice.isVisible())
            self.assertTrue(self.w.notice.property('warning'))
            self.assertIn('异常',self.w.notice.text())
            self.assertNotIn('ACK',self.w.notice.text())
            self.assertIn('ACK',self.w.log.toPlainText())
        self.w.replaying=False; self.w.render_metadata({'phase':'idle','passed':True})
        self.assertFalse(self.w.notice.property('warning'))

    def test_operation_error_survives_periodic_state_updates(self):
        self.w.on_result('connect','TEST: permission denied')
        self.assertIn('permission denied',self.w.log.toPlainText())
        self.w.render_metadata({'phase':'disconnected'})
        self.assertTrue(self.w.notice.property('warning'))
        self.assertIn('permission denied',self.w.log.toPlainText())
        self.w.on_result('disconnect',''); self.w.render_metadata({'phase':'disconnected'})
        self.assertFalse(self.w.notice.property('warning'))

    def test_hidden_panels_do_not_stop_measurement_and_f6_still_stops(self):
        self.w.mode.setCurrentIndex(self.w.mode.findData('simulator'))
        self.w.connect_button.click()
        wait(lambda: self.w.state['phase']=='idle' and not self.w.busy)
        self.w.fullscreen_action.trigger(); APP.processEvents()
        QTest.keyClick(self.w.plot,Qt.Key.Key_F5)
        wait(lambda: self.w.state.get('samples',0)>5000)
        self.assertTrue(self.w.stop_button.isVisible())
        QTest.keyClick(self.w.plot,Qt.Key.Key_F6)
        wait(lambda: self.w.capture is not None)
        self.assertTrue(self.w.capture['metadata']['passed'])
        self.assertTrue(self.w.capture['metadata']['early_stop'])
        self.w.exit_fullscreen()


class CursorTests(unittest.TestCase):
    def test_nearest_cursor_edges_ties_and_invalid_order(self):
        w = Waveform()
        self.assertIsNone(w.nearest_sample(0))
        for indices in ([20,21,22,23],[10,20,21,30],[20,4,8,21],[0,1,1,2]):
            w.set_data(indices,[100]*len(indices))
            for target in (-1,0,4,8,10,20,20.5,21,21.6,23,99):
                expected=min(range(len(indices)),key=lambda n:abs(indices[n]-target))
                self.assertEqual(w.nearest_sample(target),expected)

    def test_long_trace_cursor_uses_logarithmic_search(self):
        w=Waveform(); count=3_000_000
        w.set_data(array('I',range(count)),array('H',[32000])*count)
        # Count reads rather than asserting a machine-dependent timing threshold.
        class Counted:
            reads=0
            def __len__(self): return count
            def __getitem__(self,i): self.reads+=1; return i
        seq=Counted(); w.indices=seq
        self.assertEqual(w.nearest_sample(1_800_000.4),1_800_000)
        self.assertLess(seq.reads,32)


if __name__=='__main__': unittest.main(verbosity=2)
