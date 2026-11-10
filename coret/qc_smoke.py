"""Explicit packaged-app offline replay check; never connects to a device."""
import json
from pathlib import Path
import time
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication


class ReplayCheck(QObject):
    def __init__(self,window,directory):
        super().__init__(window)
        self.window=window;self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.deadline=time.monotonic()+15.;self.done=False;self.exit_code=1
        self.timer=QTimer(self);self.timer.setInterval(50);self.timer.timeout.connect(self.poll)
        QApplication.instance().setQuitOnLastWindowClosed(False)
        window.thread.finished.connect(lambda:QApplication.instance().exit(self.exit_code))

    def start(self):self.timer.start()

    def poll(self):
        if self.done:return
        w=self.window
        ready=w.qc_result is not None and w.qc_result.get('final') and not w.qc_final_pending
        if not ready and time.monotonic()<self.deadline:return
        self.done=True;self.timer.stop()
        disconnected=w.worker.controller is not None and w.worker.controller.port is None
        result={'completed':bool(ready),'no_device_connection':disconnected,'replaying':w.replaying,
                'window_title':w.windowTitle(),'status_text':w.qc_status.text(),'hint':w.qc_hint.text(),
                'qc':w.qc_result}
        result['screenshot_saved']=w.grab().save(str(self.directory/'window.png'))
        (self.directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        self.exit_code=0 if ready and disconnected and w.replaying and result['screenshot_saved'] else 1
        w.dirty=False;w.begin_shutdown()
