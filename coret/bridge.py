"""Qt adapter: blocking serial/controller work never runs on the GUI thread."""
from PySide6.QtCore import QObject, QTimer, Signal, Slot
from .controller import Controller, ports
from .storage import SCHEMA


class AcquisitionWorker(QObject):
    state = Signal(dict)
    completed = Signal(dict)
    result = Signal(str, str)
    port_list = Signal(list)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.controller = None
        self.timer = None
        self.last_series = None
        self.last_complete = None

    @Slot()
    def initialize(self):
        self.controller = Controller()
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.publish)
        self.timer.start()
        self.scan_ports()
        self.publish()

    @Slot()
    def scan_ports(self):
        try:
            self.port_list.emit(ports())
        except Exception as e:
            self.result.emit('ports', str(e))

    @Slot(str, dict)
    def command(self, name, args):
        if self.controller is None:
            self.result.emit(name, '通信线程尚未就绪')
            return
        try:
            self.controller.request(name, **args)
            self.result.emit(name, '')
        except Exception as e:
            self.result.emit(name, str(e))
        self.publish()

    @Slot()
    def publish(self):
        c = self.controller
        if c is None:
            return
        with c.lock:
            s = c.snapshot()
            s.pop('points', None)
            key = (c.run_id, len(c.samples.values), c.run_timestamp)
            if key != self.last_series:
                previous = self.last_series[1] if self.last_series and self.last_series[0] == c.run_id and self.last_series[2] == c.run_timestamp else 0
                s['series_delta'] = (previous, c.samples.indices[previous:], c.samples.values[previous:])
                self.last_series = key
            final = c.run_id > 0 and not c.run_open and c.pending is None and len(c.samples.values) > 0
            if final and key != self.last_complete:
                meta = {k: v for k, v in s.items() if k != 'series_delta'}
                capture = {'schema': SCHEMA, 'metadata': meta,
                           'indices': c.samples.indices[:], 'values': c.samples.values[:],
                           'rx_hex': c.rx.hex(), 'tx_hex': c.tx.hex()}
                self.completed.emit(capture)
                self.last_complete = key
        self.state.emit(s)

    @Slot()
    def shutdown(self):
        if self.timer:
            self.timer.stop()
        try:
            if self.controller:
                self.controller.close()
                self.publish()
        except Exception as e:
            self.result.emit('shutdown', '退出时通信异常：' + str(e))
        finally:
            self.finished.emit()
