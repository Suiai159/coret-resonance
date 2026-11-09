"""Opt-in integration test of the actual desktop controller, widgets and storage.
Use QT_QPA_PLATFORM=offscreen: renders an app-owned surface, not the user's desktop.
"""
import json
from pathlib import Path
import time
import traceback
from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest
from .simulator import SINE
from .storage import load_capture, export_csv
from .protocol import Parser, START_TIMED_VALVE


class SmokeTest(QObject):
    def __init__(self,window,out):
        super().__init__(window)
        self.w=window; self.out=Path(out); self.stage=0; self.started=time.monotonic()
        self.timer=QTimer(self); self.timer.setInterval(25); self.timer.timeout.connect(self.tick)
        self.report={}
        self.runs=[]
        self.status_at=0.0
        self.last_tick=time.monotonic()
        self.max_live_tick_gap=0.0
        self.failed=False

    def start(self):
        self.out.mkdir(parents=True,exist_ok=True)
        self.timer.start()

    def tick(self):
        w=self.w
        try:
            if time.monotonic()-self.started>150: raise TimeoutError('GUI smoke test timeout')
            now=time.monotonic()
            if self.stage==2 and w.state.get('phase')=='running':
                self.max_live_tick_gap=max(self.max_live_tick_gap, now-self.last_tick)
            self.last_tick=now
            if self.stage==2 and now-self.status_at>=1.0:
                print('STATUS',w.state.get('phase'),w.state.get('samples'),w.state.get('passed'),flush=True)
                self.status_at=now
            if self.stage==2 and w.state.get('phase')=='error':
                raise RuntimeError(str(w.state.get('errors')))
            if self.stage==0 and w.worker.controller is not None:
                assert not w.state['connected'] and not w.start_action.isEnabled()
                w.mode.setCurrentIndex(w.mode.findData('simulator'))
                w.connect_button.click(); self.stage=1
            elif self.stage==1 and w.state['phase']=='idle' and not w.busy:
                assert w.start_action.isEnabled()
                assert w.valve_frequency.currentData()==4
                w.valve_frequency.setCurrentIndex(w.valve_frequency.findData(1))
                w.duration.setCurrentIndex(0)
                w.start_button.click(); self.stage=2
            elif self.stage==2 and w.capture and w.state['phase']=='idle':
                capture=w.capture
                if capture['metadata']['run_id'] != len(self.runs)+1: return
                assert capture['metadata']['passed']
                expected=(5,10,15,20,30)[len(self.runs)]*100000
                assert len(capture['values'])==expected
                assert capture['metadata']['measurement_complete']
                assert capture['metadata']['stop_ack'] is None
                hz=(1,2,3,5,6)[len(self.runs)]
                assert capture['metadata']['valve_hz']==hz
                assert capture['metadata']['valve_high_us']==40000
                assert Parser().feed(bytes.fromhex(capture['tx_hex']))[0]==(
                    START_TIMED_VALVE,bytes((capture['metadata']['range'],expected//100000,hz)))
                self.runs.append({'seconds':expected//100000,'samples':expected,'valve_hz':hz,'passed':True})
                print('PRESET PASSED',expected//100000,expected,flush=True)
                if len(self.runs)<5:
                    w.valve_frequency.setCurrentIndex(w.valve_frequency.findData((1,2,3,5,6)[len(self.runs)]))
                    w.duration.setCurrentIndex(len(self.runs));w.dirty=False
                    w.start_button.click();return
                assert all(v==SINE[i&255] for i,v in zip(capture['indices'],capture['values']))
                assert w.stop_action.isEnabled() is False
                assert w.save_action.isEnabled()
                w.save_to(self.out/'desktop-sine.coret')
                export_csv(self.out/'desktop-sine.csv',capture)
                assert load_capture(self.out/'desktop-sine.coret')==capture
                rows=(self.out/'desktop-sine.csv').read_text().splitlines()
                assert len(rows)==len(capture['values'])+1
                assert w.grab().save(str(self.out/'desktop-sine.png'))
                self.report={'passed':True,'presets':self.runs,'max_live_gui_tick_gap_s':self.max_live_tick_gap,'samples':len(capture['values']),'sine_mismatches':0,
                    'crc_errors':capture['metadata']['crc_errors'],'index_gaps':capture['metadata']['index_gaps'],
                    'start_ack':capture['metadata']['start_ack'],'stop_ack':capture['metadata']['stop_ack'],
                    'end':capture['metadata']['end'],'save_load_equal':True,'csv_rows':len(rows)-1,
                    'rendering':f'Qt {QApplication.platformName()} actual application surface','physical_serial_opened':False}
                w.disconnect_button.click(); self.stage=3
            elif self.stage==3 and not w.state['connected'] and not w.busy:
                w.load_from(self.out/'desktop-sine.coret')
                assert w.replaying and w.badge.text()=='离线回放'
                assert w.plot.values==w.capture['values']
                before=w.plot.xmax-w.plot.xmin; w.plot.zoom(.5)
                assert w.plot.xmax-w.plot.xmin < before
                w.plot.fit(); assert w.plot.xmin==0
                w.span.setCurrentIndex(1); w.plot.set_span(1000)
                assert w.grab().save(str(self.out/'desktop-replay.png'))
                self.report.update(replay=True,zoom=True,serial_released=True)
                w.range.showPopup(); self.stage=4
            elif self.stage==4:
                assert w.range.view().isVisible() and w.range.count()==9
                assert w.range.view().window().grab().save(str(self.out/'range-dropdown.png'))
                w.range.hidePopup(); w.span.showPopup(); self.stage=5
            elif self.stage==5:
                assert w.span.view().isVisible() and w.span.count()==5
                assert w.span.view().window().grab().save(str(self.out/'span-dropdown.png'))
                w.span.hidePopup()
                self.dialog=QMessageBox(QMessageBox.Icon.Question,'保存当前会话',
                    '当前会话尚未保存，是否先保存？',
                    QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,w)
                self.dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
                self.dialog.show(); self.stage=6
            elif self.stage==6:
                assert self.dialog.isVisible()
                assert all(button.text() for button in self.dialog.buttons())
                assert self.dialog.grab().save(str(self.out/'save-confirmation.png'))
                self.dialog.reject(); self.dialog.deleteLater()
                self.report.update(expanded_dropdowns=True,save_dialog_rendered=True)
                self.stage=8
            elif self.stage==8:
                assert not w.diagnostics.isVisible()
                w.settings_action.trigger(); w.log_action.trigger()
                self.pre_fullscreen=(w.settings_action.isChecked(),w.log_action.isChecked())
                if not w.isMaximized(): w.maximize_action.trigger()
                self.ui_deadline=time.monotonic()+5; self.stage=9
            elif self.stage==9:
                if not w.isMaximized():
                    assert time.monotonic()<self.ui_deadline, 'window manager did not maximize'
                    return
                w.activateWindow(); w.plot.setFocus(); self.stage=12
            elif self.stage==12:
                QTest.keyClick(w.plot,Qt.Key.Key_F11)
                self.ui_deadline=time.monotonic()+5; self.stage=10
            elif self.stage==10:
                if not w.isFullScreen():
                    assert time.monotonic()<self.ui_deadline, 'F11 did not enter fullscreen'
                    return
                assert not w.sidebar.isVisible() and not w.diagnostics.isVisible()
                assert w.toolbar.isVisible() and w.stop_button.isVisible()
                assert w.grab().save(str(self.out/'fullscreen.png'))
                QTest.keyClick(w.plot,Qt.Key.Key_Escape)
                self.ui_deadline=time.monotonic()+5; self.stage=11
            elif self.stage==11:
                if w.isFullScreen() or not w.isMaximized():
                    assert time.monotonic()<self.ui_deadline, 'Esc did not restore maximized state'
                    return
                assert (w.sidebar.isVisible(),w.diagnostics.isVisible())==self.pre_fullscreen
                w.log_action.trigger(); w.settings_action.trigger(); w.maximize_action.trigger()
                self.report.update(maximize_restore=True,fullscreen_escape=True,panel_restore=True)
                (self.out/'gui-smoke.json').write_text(json.dumps(self.report,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps(self.report,ensure_ascii=False),flush=True)
                w.begin_shutdown(); self.stage=7
            elif self.stage==7 and not w.thread.isRunning():
                self.timer.stop()
        except Exception as e:
            self.timer.stop(); self.failed=True
            (self.out/'gui-smoke.json').write_text(json.dumps({'passed':False,'stage':self.stage,'error':repr(e),'traceback':traceback.format_exc(),'runs':self.runs,'max_live_gui_tick_gap_s':self.max_live_tick_gap,'state_phase':w.state.get('phase'),'capture_metadata':w.capture.get('metadata') if w.capture else None},indent=2))
            print('GUI SMOKE FAILED:',repr(e),flush=True)
            w.thread.finished.connect(lambda:QApplication.exit(1))
            w.begin_shutdown()
