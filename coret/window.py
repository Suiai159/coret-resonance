"""CoreT desktop window. The GUI owns presentation, never serial I/O."""
from datetime import datetime
import json
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent
from PySide6.QtGui import QAction, QKeySequence, QFont
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QComboBox, QSpinBox, QPushButton, QGroupBox, QFrame, QSplitter, QPlainTextEdit,
    QProgressBar, QTabWidget, QFileDialog, QMessageBox, QCheckBox, QToolBar, QStyle, QScrollArea, QLayout, QSizePolicy)
from . import __version__
from .protocol import VALVE_FREQUENCIES, VALVE_DEFAULT_HZ
from .bridge import AcquisitionWorker
from .qc_worker import QCWorker
from .plot import Waveform
from .storage import save_capture, load_capture, export_csv, atomic_write

PHASE = {'disconnected':'未连接', 'syncing':'连接同步中', 'idle':'就绪 / 已停止', 'starting':'准备采集',
         'running':'采集中', 'stopping':'正在停止', 'draining':'接收剩余数据', 'error':'通信异常'}
ACTIVE = ('starting', 'running', 'stopping', 'draining')
RANGES = [(0,'±12.288 V'),(1,'±10.240 V'),(2,'±6.144 V'),(3,'±5.120 V'),(4,'±2.560 V'),
          (8,'0–12.288 V'),(9,'0–10.240 V'),(10,'0–6.144 V'),(11,'0–5.120 V')]
STYLE = '''
QMainWindow,QWidget#root{background:#f2f5f8;color:#243746;}
QWidget{font-family:"Microsoft YaHei UI","Microsoft YaHei","Noto Sans CJK SC","Noto Sans SC";font-size:12px;color:#243746;}
QWidget:disabled{color:#697c8a;}
QDialog{background:#f2f5f8;}
QToolTip{background:#fffbea;color:#243746;border:1px solid #cbd6df;}
QMenuBar,QMenu,QToolBar{background:#fff;color:#243746;}
QToolBar{border-bottom:1px solid #dce3ea;spacing:5px;padding:7px;}
QToolBar QToolButton{padding:7px 6px;}
QToolButton{padding:7px 12px;border-radius:5px;}
QToolButton:hover{background:#eaf0f6;}
QGroupBox{background:#fff;border:1px solid #dce3ea;border-radius:7px;margin-top:12px;padding:16px 12px 12px;}
QGroupBox::title{subcontrol-origin:margin;left:12px;padding:0 5px;color:#334d60;font-weight:bold;}
QPushButton{background:#fff;color:#243746;border:1px solid #cbd6df;border-radius:5px;padding:8px 12px;min-height:17px;}
QPushButton:hover{background:#edf4f7;border-color:#7ca8bb;}
QPushButton:disabled{color:#697c8a;background:#f5f7f9;border-color:#e0e6ed;}
QPushButton#start{background:#167c72;color:#fff;border-color:#167c72;font-weight:bold;padding:10px;}
QPushButton#start:hover{background:#11675f;} QPushButton#start:disabled{background:#c4d8d3;color:#365950;border-color:#c4d8d3;}
QPushButton#stop{color:#b74949;} QPushButton#stop:disabled{color:#697c8a;}
QComboBox,QSpinBox{background:#fff;color:#243746;border:1px solid #cbd6df;border-radius:4px;padding:6px;min-height:20px;}
QComboBox:disabled,QSpinBox:disabled{background:#f2f4f7;color:#697c8a;}
QComboBox QAbstractItemView{background:#fff;color:#243746;selection-background-color:#d8eee9;selection-color:#243746;}
QLabel#hint{color:#70818e;font-size:11px;} QLabel#brand{font-size:21px;font-weight:bold;color:#203a4f;}
QLabel#badge{background:#e4eee9;color:#236b61;border-radius:4px;padding:6px 12px;font-weight:bold;}
QLabel#notice{background:#e9f1f6;color:#3b586c;border:1px solid #d5e3ed;border-radius:5px;padding:6px 9px;}
QLabel#notice[warning="true"]{background:#fff0ed;color:#a53326;border-color:#e4b0a7;}
QFrame#metric{background:#fff;border:1px solid #dce3ea;border-radius:6px;}
QLabel#metricValue{font-family:"Consolas","DejaVu Sans Mono",monospace;font-size:23px;font-weight:bold;color:#213e52;}
QPlainTextEdit{background:#fff;color:#425b6a;border:1px solid #dce3ea;border-radius:4px;font-family:"Consolas","DejaVu Sans Mono",monospace;font-size:11px;}
QTabWidget::pane{border:1px solid #dce3ea;background:#fff;border-radius:4px;}
QTabBar::tab{padding:7px 16px;background:#e7edf2;color:#5c6e7e;}
QTabBar::tab:selected{background:#fff;color:#167c72;}
QSplitter::handle{background:#e0e7ed;}
QStatusBar{background:#fff;border-top:1px solid #dce3ea;color:#5f7382;}
'''


def label(text, hint=False):
    w = QLabel(text)
    w.setTextFormat(Qt.TextFormat.PlainText)
    if hint:
        w.setObjectName('hint'); w.setWordWrap(True)
    return w


def group(title):
    box = QGroupBox(title)
    layout = QVBoxLayout(box)
    layout.setSpacing(10)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    return box, layout


class MainWindow(QMainWindow):
    send = Signal(str, dict)
    scan = Signal()
    shutdown_requested = Signal()
    qc_reset = Signal(int)
    qc_samples = Signal(int, dict, object)
    qc_finish = Signal(int, dict, bool)

    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.capture = None
        self.capture_path = None
        self.dirty = False
        self.replaying = False
        self.busy = False
        self.closing = False
        self.allow_close = False
        self.state = {'phase':'disconnected','connected':False,'pending':None}
        self.last_log = ''
        self.operation_error = ''
        self.ui_errors = []
        self.qc_generation = 0
        self.qc_requested = False
        self._next_qc_requested = False
        self.qc_final_pending = False
        self.qc_result = None
        self.qc_stream_error = False
        self.setWindowTitle('CoreT 采集助手  ' + __version__)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setMinimumSize(900, 560)
        available = self.screen().availableGeometry()
        self.resize(min(1280, available.width() - 40), min(800, available.height() - 70))
        self._fullscreen_restore = None
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._restore_settings()
        self.qc_thread = QThread(self)
        self.qc_worker = QCWorker()
        self.qc_worker.moveToThread(self.qc_thread)
        self.qc_reset.connect(self.qc_worker.reset)
        self.qc_samples.connect(self.qc_worker.samples)
        self.qc_finish.connect(self.qc_worker.finish)
        self.qc_worker.updated.connect(self.on_qc_update)
        self.qc_thread.finished.connect(self.qc_worker.deleteLater)
        self.qc_thread.start()
        self.thread = QThread(self)
        self.worker = AcquisitionWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.initialize)
        self.send.connect(self.worker.command)
        self.scan.connect(self.worker.scan_ports)
        self.shutdown_requested.connect(self.worker.shutdown)
        self.worker.state.connect(self.on_state)
        self.worker.completed.connect(self.on_completed)
        self.worker.result.connect(self.on_result)
        self.worker.port_list.connect(self.on_ports)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._closed)
        self.thread.start()
        self.update_actions()

    def _action(self, title, shortcut, callback, icon=None):
        a = QAction(title, self)
        if shortcut: a.setShortcut(shortcut)
        if icon: a.setIcon(self.style().standardIcon(icon))
        a.triggered.connect(callback)
        return a

    def _build_ui(self):
        self.open_action = self._action('打开会话', 'Ctrl+O', self.open_file, QStyle.StandardPixmap.SP_DialogOpenButton)
        self.save_action = self._action('保存会话', 'Ctrl+S', self.save_file, QStyle.StandardPixmap.SP_DialogSaveButton)
        self.csv_action = self._action('导出 CSV', 'Ctrl+Shift+S', self.save_csv)
        self.image_action = self._action('导出波形图片', None, self.save_image)
        self.start_action = self._action('开始测量', 'F5', self.start_capture)
        self.qc_action = self._action('开始质检', 'F7', self.start_qc)
        self.stop_action = self._action('停止', 'F6', lambda:self.command('stop'))
        self.connect_action = self._action('连接', 'Ctrl+D', self.connect_source)
        self.disconnect_action = self._action('断开', None, lambda:self.command('disconnect'))
        self.fit_action = self._action('显示完整波形', 'F', lambda:self.plot.fit())
        self.settings_action = self._action('设置', 'Ctrl+B', self.toggle_settings)
        self.settings_action.setCheckable(True); self.settings_action.setChecked(True)
        self.log_action = self._action('日志', 'Ctrl+L', self.toggle_log)
        self.log_action.setCheckable(True)
        self.maximize_action = self._action('最大化', 'Ctrl+M', self.toggle_maximized)
        self.fullscreen_action = self._action('全屏', 'F11', self.toggle_fullscreen)
        self.fullscreen_action.setCheckable(True)
        self.escape_action = self._action('退出全屏', 'Esc', self.exit_fullscreen)
        # Window-level actions keep shortcuts alive even when the menu is hidden.
        self.addActions([self.escape_action, self.fullscreen_action, self.start_action,
                         self.stop_action, self.save_action, self.fit_action])
        file_menu = self.menuBar().addMenu('文件(&F)')
        file_menu.addActions([self.open_action,self.save_action,self.csv_action,self.image_action])
        file_menu.addSeparator(); file_menu.addAction(self._action('退出', 'Ctrl+Q', self.close))
        device_menu = self.menuBar().addMenu('采集(&A)')
        device_menu.addActions([self.connect_action,self.disconnect_action,self.qc_action,self.start_action,self.stop_action])
        self.addAction(self.qc_action)
        view_menu = self.menuBar().addMenu('视图(&V)')
        view_menu.addActions([self.fit_action, self.settings_action, self.log_action])
        view_menu.addSeparator(); view_menu.addActions([self.maximize_action,self.fullscreen_action])
        help_menu = self.menuBar().addMenu('帮助(&H)')
        help_menu.addAction(self._action('快速使用', 'F1', self.show_help))
        help_menu.addAction(self._action('关于 CoreT', None, self.show_about))

        self.toolbar = QToolBar('常用操作', self); self.toolbar.setMovable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        # File operations remain in the menu/shortcuts; keep F5/F6/F11 visible at 900 px.
        self.toolbar.addAction(self.save_action)
        self.toolbar.addWidget(label('时长  '))
        self.duration = QComboBox()
        for seconds in (5, 10, 15, 20, 30): self.duration.addItem(f'{seconds} 秒', seconds * 1000)
        self.duration.setCurrentIndex(1); self.duration.setMinimumWidth(88)
        self.toolbar.addWidget(self.duration)
        self.toolbar.addWidget(label('  电磁阀  '))
        self.valve_frequency = QComboBox()
        for hz in VALVE_FREQUENCIES: self.valve_frequency.addItem(f'{hz} Hz', hz)
        self.valve_frequency.setCurrentIndex(self.valve_frequency.findData(VALVE_DEFAULT_HZ))
        self.valve_frequency.setMinimumWidth(76)
        self.valve_frequency.setToolTip('与测量联动；高电平固定 40 ms，不是固定占空比。停止状态才可修改。')
        self.toolbar.addWidget(self.valve_frequency)
        self.start_button = QPushButton('开始测量  F5'); self.start_button.setObjectName('start')
        self.start_button.clicked.connect(self.start_capture); self.toolbar.addWidget(self.start_button)
        self.stop_button = QPushButton('停止  F6'); self.stop_button.setObjectName('stop')
        self.stop_button.clicked.connect(lambda:self.command('stop')); self.toolbar.addWidget(self.stop_button)
        spacer = QWidget(); spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(spacer)
        self.toolbar.addActions([self.settings_action,self.log_action,self.maximize_action,self.fullscreen_action])
        self.addToolBar(self.toolbar)

        root = QWidget(); root.setObjectName('root'); self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(10,8,10,6); outer.setSpacing(6)
        status = QHBoxLayout(); status.setSpacing(8)
        self.badge = label('未连接'); self.badge.setObjectName('badge'); status.addWidget(self.badge)
        self.notice = label('需配套 0.3 固件；开始测量会同步驱动电磁阀（40 ms 高电平）。')
        self.notice.setWordWrap(True); self.notice.setObjectName('notice')
        self.notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status.addWidget(self.notice,1)
        self.elapsed_label = label(''); status.addWidget(self.elapsed_label)
        outer.addLayout(status)
        self.progress = QProgressBar(); self.progress.setRange(0, 1000); self.progress.setValue(0)
        self.progress.setTextVisible(False); self.progress.setFixedHeight(4)
        outer.addWidget(self.progress)
        qc_panel = QFrame(); qc_panel.setObjectName('qcPanel')
        qc_panel.setStyleSheet('QFrame#qcPanel{background:#fff;border:1px solid #dce3ea;border-radius:6px;}')
        qc_layout = QVBoxLayout(qc_panel); qc_layout.setContentsMargins(10,7,10,7); qc_layout.setSpacing(4)
        qc_row = QHBoxLayout()
        self.qc_button = QPushButton('开始质检  F7'); self.qc_button.setObjectName('start')
        self.qc_button.setToolTip('使用当前选择的输入量程；固定 20 秒采集、4 Hz，确认出击后连续测试 10 秒。采集期间不可切换量程。')
        self.qc_button.clicked.connect(self.start_qc)
        self.qc_status = label('待开始质检'); self.qc_status.setStyleSheet('font-size:21px;font-weight:bold;color:#425b6a;')
        qc_row.addWidget(self.qc_status,1)
        self.qc_time = label('连续目标 10.0 s'); qc_row.addWidget(self.qc_time)
        qc_layout.addLayout(qc_row)
        self.qc_hint = label('试运行 · F7 开始质检；F5 仅采集。正式测试中不要抬起，中断后需重新开始。',True)
        qc_layout.addWidget(self.qc_hint)
        self.qc_progress = QProgressBar(); self.qc_progress.setRange(0,1000); self.qc_progress.setValue(0)
        self.qc_progress.setTextVisible(False); self.qc_progress.setFixedHeight(5); qc_layout.addWidget(self.qc_progress)
        self.qc_panel=qc_panel; qc_panel.hide()
        outer.addWidget(qc_panel)
        body = QHBoxLayout(); body.setSpacing(10); outer.addLayout(body,1)

        sidebar = QWidget(); left = QVBoxLayout(sidebar)
        left.setContentsMargins(0,0,0,0); left.setSpacing(8)
        box, g = group('核心件连续质检'); left.addWidget(box)
        g.addWidget(self.qc_button)
        g.addWidget(label('连续 10 秒 · 4 Hz · 试运行\n中断后必须重新开始，不自动续测。',True))
        box, g = group('设备连接'); left.addWidget(box)
        self.mode = QComboBox(); self.mode.addItem('USB · ESP32-S3', 'serial'); self.mode.addItem('演示 · PC 正弦模拟', 'simulator')
        g.addWidget(self.mode)
        self.port = QComboBox(); self.port.setMinimumContentsLength(12)
        self.port.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        g.addWidget(self.port)
        self.refresh = QPushButton('刷新串口'); self.refresh.clicked.connect(self.scan.emit); g.addWidget(self.refresh)
        row = QHBoxLayout(); self.connect_button = QPushButton('连接'); self.disconnect_button = QPushButton('断开')
        self.connect_button.clicked.connect(self.connect_source); self.disconnect_button.clicked.connect(lambda:self.command('disconnect'))
        row.addWidget(self.connect_button); row.addWidget(self.disconnect_button); g.addLayout(row)
        self.source_hint = label('',True); g.addWidget(self.source_hint)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        box, g = group('采样设置'); left.addWidget(box)
        self.range = QComboBox()
        for code, text in RANGES: self.range.addItem(text, code)
        g.addWidget(label('输入量程',True)); g.addWidget(self.range)
        g.addWidget(label('采样率 100 kS/s（标称）',True))
        box,g = group('会话文件'); left.addWidget(box)
        self.save_button = QPushButton('保存会话…'); self.save_button.clicked.connect(self.save_file); g.addWidget(self.save_button)
        self.file_hint = label('保存后可离线查看波形。',True); g.addWidget(self.file_hint)
        left.addStretch()
        self.sidebar = QScrollArea(); self.sidebar.setWidgetResizable(True)
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar.setFixedWidth(236); self.sidebar.setWidget(sidebar); body.addWidget(self.sidebar)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        scope = QWidget(); layout = QVBoxLayout(scope); layout.setContentsMargins(0,0,0,0); layout.setSpacing(4)
        controls = QHBoxLayout()
        self.chart_title = label('CH1 · 原始码'); controls.addWidget(self.chart_title); controls.addStretch()
        self.span = QComboBox()
        for text,count in [('最近 10 ms',1000),('最近 100 ms',10000),('最近 1 秒',100000),('最近 5 秒',500000),('完整会话',None)]: self.span.addItem(text,count)
        self.span.setCurrentIndex(4); controls.addWidget(self.span)
        self.y_check = QCheckBox('自动纵轴'); controls.addWidget(self.y_check)
        fit = QPushButton('全览'); fit.setToolTip('显示整段波形（F / 双击），不改变窗口大小')
        fit.clicked.connect(lambda:self.plot.fit()); controls.addWidget(fit)
        layout.addLayout(controls); self.plot = Waveform(); layout.addWidget(self.plot,1)
        self.span.currentIndexChanged.connect(lambda:self.plot.set_span(self.span.currentData()))
        self.y_check.toggled.connect(self.auto_y)
        self.cursor_label = label('滚轮缩放 · 拖动平移 · 双击全览 · F11 全屏',True)
        layout.addWidget(self.cursor_label); self.plot.cursor_readout.connect(self.cursor_label.setText)
        self.splitter.addWidget(scope)

        self.diagnostics = QTabWidget()
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(300)
        self.diagnostics.addTab(self.log,'通信日志')
        summary = QWidget(); form = QFormLayout(summary); form.setContentsMargins(12,8,12,8)
        self.metric_values = {}
        for key,title in [('samples','样本数'),('time','数据时长'),('integrity','CRC / 序号异常'),('result','会话结果')]:
            w = label('—'); form.addRow(title,w); self.metric_values[key] = w
        self.facts = {}
        for key,title in [('acks','START / STOP ACK'),('end','END 确认'),('signal','数据统计'),('source','数据来源'),('errors','错误信息')]:
            w = label('—'); w.setWordWrap(True); form.addRow(title,w); self.facts[key]=w
        detail_scroll = QScrollArea(); detail_scroll.setWidgetResizable(True); detail_scroll.setWidget(summary)
        self.diagnostics.addTab(detail_scroll,'诊断详情')
        self.splitter.addWidget(self.diagnostics); self.diagnostics.hide()
        self.splitter.setStretchFactor(0,1); self.splitter.setStretchFactor(1,0)
        body.addWidget(self.splitter,1)
        self.statusBar().showMessage('就绪 · 不会自动连接或开始测量')
        self.version_label = label('CoreT Desktop  ' + __version__); self.statusBar().addPermanentWidget(self.version_label)
        self.mode_changed()

    def set_notice(self, text, warning=False):
        self.notice.setText(text)
        if self.notice.property('warning') != warning:
            self.notice.setProperty('warning', warning)
            self.notice.style().unpolish(self.notice); self.notice.style().polish(self.notice)
        self.notice.setToolTip('详情可在“日志”中查看。' if warning else text)

    def toggle_settings(self, checked):
        self.sidebar.setVisible(checked)

    def toggle_log(self, checked):
        self.diagnostics.setVisible(checked)
        if checked:
            height = self.splitter.height()
            self.splitter.setSizes([max(300, height-180),180])

    def toggle_maximized(self):
        if self.isFullScreen(): return
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def toggle_fullscreen(self, checked):
        if checked:
            if self._fullscreen_restore is not None: return
            self._fullscreen_restore = (self.saveGeometry(), self.isMaximized())
            self.sidebar.hide(); self.diagnostics.hide()
            self.menuBar().hide(); self.statusBar().hide()
            self.settings_action.setEnabled(False); self.log_action.setEnabled(False)
            self.maximize_action.setEnabled(False)
            self.fullscreen_action.setText('退出全屏')
            self.showFullScreen()
        else:
            self.exit_fullscreen()

    def exit_fullscreen(self):
        if self._fullscreen_restore is None: return
        geometry, maximized = self._fullscreen_restore
        self._fullscreen_restore = None
        self.fullscreen_action.setChecked(False); self.fullscreen_action.setText('全屏')
        self.menuBar().show(); self.statusBar().show()
        self.settings_action.setEnabled(True); self.log_action.setEnabled(True)
        self.maximize_action.setEnabled(True)
        self.sidebar.setVisible(self.settings_action.isChecked())
        self.diagnostics.setVisible(self.log_action.isChecked())
        self.showNormal(); self.restoreGeometry(geometry)
        if maximized: self.showMaximized()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self,'maximize_action'):
            self.maximize_action.setText('还原窗口' if self.isMaximized() else '最大化')
            # Also restore panels if the window manager itself leaves fullscreen.
            if self._fullscreen_restore is not None and not self.isFullScreen():
                self.exit_fullscreen()

    def mode_changed(self):
        serial = self.mode.currentData() == 'serial'
        self.port.setVisible(serial); self.refresh.setVisible(serial)
        self.source_hint.setText('选择 USB 数据口，不选 UART 日志口。' if serial else '演示数据，不是真实设备测量。')
        self.update_actions()

    def on_ports(self, items):
        previous = self.port.currentData() or getattr(self,'preferred_port','')
        self.port.clear()
        for p in items:
            if p['native_usb']: self.port.addItem(p['device']+'  ·  USB 数据口',p['device'])
        found = self.port.findData(previous)
        if found >= 0: self.port.setCurrentIndex(found)
        if self.port.count()==0: self.port.addItem('未发现 ESP32-S3 USB 数据口','')
        self.update_actions()

    def update_actions(self):
        if not hasattr(self,'start_action'): return
        connected = self.state.get('connected',False)
        phase = self.state.get('phase','disconnected')
        ready = not self.busy and not self.closing
        can_connect = ready and not connected and (self.mode.currentData()!='serial' or bool(self.port.currentData()))
        can_start = ready and phase=='idle' and self.state.get('pending') is None
        can_save = ready and bool(self.capture) and phase not in ACTIVE and not self.qc_final_pending
        qc_ready = can_start and not self.qc_final_pending and self.state.get('mode')!='simulator'
        for w in (self.qc_button,self.qc_action): w.setEnabled(qc_ready)
        for w in (self.connect_button,self.connect_action): w.setEnabled(can_connect)
        for w in (self.disconnect_button,self.disconnect_action): w.setEnabled(ready and connected)
        for w in (self.start_button,self.start_action): w.setEnabled(can_start and not self.qc_final_pending)
        for w in (self.stop_button,self.stop_action): w.setEnabled(ready and phase in ('running','starting'))
        for w in (self.mode,self.port,self.refresh): w.setEnabled(ready and not connected)
        for w in (self.duration,self.range,self.valve_frequency): w.setEnabled(ready and phase not in ACTIVE and phase!='syncing')
        for w in (self.save_button,self.save_action,self.csv_action,self.image_action): w.setEnabled(can_save)
        self.open_action.setEnabled(ready and not connected)

    def command(self, name, **args):
        if self.busy or self.closing: return
        self.busy=True; self.update_actions(); self.send.emit(name,args)

    def connect_source(self):
        if not self.confirm_replace(): return
        self.command('connect',mode=self.mode.currentData(),port=self.port.currentData() or '')

    def start_capture(self):
        if self.busy or self.closing or self.qc_final_pending: return
        if not self.confirm_replace(): return
        self._next_qc_requested=False
        self.command('start',range=self.range.currentData(),duration_ms=self.duration.currentData(),
                     valve_hz=self.valve_frequency.currentData())

    def start_qc(self):
        if self.busy or self.closing or self.qc_final_pending or self.state.get('phase')!='idle': return
        if self.state.get('mode')=='simulator':
            self.set_notice('演示数据不用于质检，请连接真实设备。',warning=True); return
        if not self.confirm_replace(): return
        self._next_qc_requested=True
        self.duration.setCurrentIndex(self.duration.findData(20000))
        self.valve_frequency.setCurrentIndex(self.valve_frequency.findData(4))
        self.command('start',range=self.range.currentData(),duration_ms=20000,valve_hz=4)

    def reset_qc(self, enabled, replay=False):
        self.qc_generation+=1; self.qc_requested=enabled; self.qc_final_pending=False
        self.qc_result=None; self.qc_stream_error=False
        self.qc_reset.emit(self.qc_generation)
        self.qc_panel.setVisible(enabled or replay)
        self.qc_progress.setValue(0); self.qc_time.setText('连续目标 10.0 s')
        self.qc_status.setText('回放分析中' if replay else '准备质检' if enabled else '仅采集 · 未质检')
        self.qc_status.setStyleSheet('font-size:21px;font-weight:bold;color:#425b6a;')
        self.qc_hint.setText('正在后台分析完整波形…' if replay else '请保持接触，确认出击后开始连续计时。' if enabled else 'F5 仅采集；需要质检请在本次采集结束后按 F7 开始新一轮。')

    def on_qc_update(self, generation, result):
        if generation!=self.qc_generation or self.closing: return
        if not self.qc_requested and not self.replaying: return
        result=dict(result)
        if self.qc_stream_error:
            result.update(result='INVALID_DATA',reason='ACQUISITION_ERROR',message='采集通信异常，本轮不能判合格')
        self.qc_result=result
        state=result.get('result','INVALID_DATA'); final=result.get('final',False)
        titles={'PREPARING':'准备中 · 请保持接触','TESTING':'连续测试中 · 不要抬起',
                'AWAITING_VALIDATION':'连续测试完成 · 待校验','REFERENCE_PASS':'合格（试运行）',
                'NOT_PASSED':'不合格（本轮）','RETEST':'需重测','INVALID_DATA':'数据无效',
                'NOT_SUPPORTED':'未评估'}
        # Never display a green final verdict from a partial preview.
        if state=='REFERENCE_PASS' and not final:state='AWAITING_VALIDATION'
        color='#167c72' if state=='REFERENCE_PASS' else '#b74949' if state in ('NOT_PASSED','INVALID_DATA') else '#a66a13' if state=='RETEST' else '#245777'
        prefix='回放 · ' if self.replaying else ''
        self.qc_status.setText(prefix+titles.get(state,'待评估'))
        self.qc_status.setStyleSheet(f'font-size:21px;font-weight:bold;color:{color};')
        elapsed=float(result.get('continuous_evidence_s',0.)); required=float(result.get('required_continuous_s',10.))
        self.qc_time.setText(f'连续 {min(elapsed,required):.1f} / {required:g} s')
        self.qc_progress.setValue(min(1000,int(elapsed/required*1000)))
        self.qc_hint.setText(result.get('message','请检查测试状态'))
        if final:
            self.qc_final_pending=False
            if self.capture is not None:
                self.capture['metadata']['product_qc']=result
                self.file_hint.setText(('回放分析完成：' if self.replaying else '质检已完成，保存会话将包含判定：')+titles.get(state,state))
        self.update_actions()

    def on_result(self, name, error):
        self.busy=False
        if error:
            self.operation_error = error
            self.ui_errors.append(f'{datetime.now():%H:%M:%S}  界面操作 {name}：{error}')
            self.ui_errors = self.ui_errors[-20:]
            self.set_notice('操作未完成，请查看日志。', warning=True)
            self.statusBar().showMessage('操作未完成 · 详情见日志')
            self.render_metadata(self.capture['metadata'] if self.replaying and self.capture else self.state)
        else:
            self.operation_error = ''
            if name in ('start','connect'):
                self.replaying=False; self.capture=None; self.capture_path=None; self.dirty=False
                self.plot.set_data([],[]); self.plot.set_span(self.span.currentData())
                self.file_hint.setText('等待当前会话结束后保存。')
                self.reset_qc(name=='start' and self._next_qc_requested)
            self.statusBar().showMessage({'connect':'正在连接同步…','start':'正在准备测量…','stop':'正在停止并接收剩余数据…','disconnect':'正在断开连接…'}.get(name,'就绪'))
        self.update_actions()

    def on_state(self, s):
        self.state=s
        if not self.replaying:
            if 'series_delta' in s: self.plot.append_data(*s['series_delta'], s['sample_rate'])
            self.render_metadata(s)
            if self.qc_requested:
                if s.get('phase')=='error' or s.get('errors') or s.get('passed') is False:
                    self.qc_stream_error=True
                    self.on_qc_update(self.qc_generation,{'result':'INVALID_DATA','message':'通信或采集异常，本轮不能放行'})
                if 'series_delta' in s:
                    self.qc_samples.emit(self.qc_generation,{k:v for k,v in s.items() if k!='series_delta'},s['series_delta'])
        self.badge.setText('离线回放' if self.replaying else PHASE.get(s['phase'],s['phase']))
        self.update_actions()

    def render_metadata(self, s):
        count = s.get('samples',0)
        self.metric_values['samples'].setText(f'{count:,}')
        self.metric_values['time'].setText(f'{count/s.get("sample_rate",100000):.3f} s' if count else '—')
        self.metric_values['integrity'].setText(f'{s.get("crc_errors",0)} / {s.get("index_gaps",0)}')
        target = s.get('target_samples', 0)
        elapsed = count / s.get('sample_rate', 100000)
        remaining = max(0, target - count) / s.get('sample_rate', 100000)
        self.progress.setValue(min(1000, int(count * 1000 / target)) if target else 0)
        self.progress.setFormat(f'已采 {elapsed:.2f} s  /  剩余 {remaining:.2f} s' if target else '等待开始')
        self.elapsed_label.setText(f'{elapsed:.2f} / {target / s.get("sample_rate",100000):g} s' if target else (f'{elapsed:.2f} s' if count else ''))
        result = '提前停止' if s.get('passed') is True and s.get('early_stop') else '完整通过' if s.get('passed') is True else '未通过' if s.get('passed') is False else '接收中' if count else '等待采集'
        self.metric_values['result'].setText(result)
        self.metric_values['result'].setStyleSheet('color:'+('#167c72' if s.get('passed') is True else '#b74949' if s.get('passed') is False else '#213e52')+';')
        self.facts['acks'].setText(f'{s.get("start_ack") if s.get("start_ack") is not None else "—"} / {s.get("stop_ack") if s.get("stop_ack") is not None else "—"}')
        end = s.get('end')
        self.facts['end'].setText(f'{end.get("stored_samples")} 点  ·  reason {end.get("reason")}' if isinstance(end,dict) else '—')
        lo, hi = self.plot.extrema
        self.facts['signal'].setText(f'MIN {lo:,}   /   MAX {hi:,}   /   P-P {hi-lo:,}' if lo is not None else '—')
        source = 'PC 正弦模拟' if s.get('mode')=='simulator' else 'USB 设备数据'
        self.chart_title.setText('CH1 · 原始码' + (' · 演示' if s.get('mode')=='simulator' else ''))
        self.facts['source'].setText(('文件回放 · ' if self.replaying else '')+source+'  |  '+str(s.get('timestamp') or '尚未采集'))
        errors = list(s.get('errors',[]))
        if self.operation_error: errors.append(self.operation_error)
        if s.get('passed') is False and not errors: errors=['完整性检查未通过，请核对 CRC、序号、ACK 和 END。']
        self.facts['errors'].setText('; '.join(errors) or '无')
        text='\n'.join([f'{e["time"]}  {e["text"]}' for e in s.get('events',[])] + self.ui_errors)
        if text != self.last_log:
            self.log.setPlainText(text); self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum()); self.last_log=text
        if errors or s.get('phase') == 'error':
            self.set_notice(('回放文件存在异常' if self.replaying else '测量异常，数据需检查') + ' · 详情见日志', warning=True)
        elif self.replaying:
            self.set_notice('离线回放 · ' + ('演示数据' if s.get('mode')=='simulator' else '设备数据') + ' · 非实时采集')
        elif s.get('passed') is True:
            self.set_notice('提前停止 · 已收数据完整，可保存' if s.get('early_stop') else '测量完成 · 数据完整，可保存')
        elif s.get('phase') in ACTIVE:
            self.set_notice('测量中 · 可点击停止提前结束' if s.get('phase')=='running' else PHASE.get(s.get('phase'),'正在处理…'))
        elif s.get('phase')=='idle': self.set_notice('设备就绪 · 开始测量将联动电磁阀，40 ms 高电平' if s.get('mode')!='simulator' else '演示就绪 · 非真实设备数据')
        elif s.get('phase')=='syncing': self.set_notice('正在连接设备…')
        else: self.set_notice('需配套 0.3 固件；开始测量会同步驱动电磁阀（40 ms 高电平）。')

    def on_completed(self, capture):
        self.capture=capture; self.capture_path=None; self.dirty=True
        capture['metadata']['qc_requested']=self.qc_requested
        if self.qc_requested:
            self.qc_final_pending=True
            self.qc_finish.emit(self.qc_generation,capture,False)
        self.file_hint.setText(f'{len(capture["values"]):,} 点 · 尚未保存\n保存 .coret 会话可完整回放。')
        self.update_actions()

    def confirm_replace(self):
        if not self.dirty: return True
        answer=QMessageBox.question(self,'保存当前会话','当前会话尚未保存，是否先保存？',QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Save)
        if answer==QMessageBox.StandardButton.Cancel: return False
        if answer==QMessageBox.StandardButton.Save: return self.save_file()
        return True

    def default_path(self, suffix):
        return str(self.data_dir / ('CoreT-'+datetime.now().strftime('%Y%m%d-%H%M%S')+suffix))

    def save_to(self, path):
        if not self.capture: raise ValueError('当前没有可保存的会话')
        if self.qc_final_pending: raise ValueError('质检结果正在校验，请稍候保存')
        save_capture(path,self.capture)
        self.dirty=False; self.capture_path=Path(path)
        self.file_hint.setText('已保存：'+Path(path).name)
        self.statusBar().showMessage('会话已保存：'+str(path))

    def save_file(self):
        if not self.capture: return False
        path,_=QFileDialog.getSaveFileName(self,'保存完整会话',str(self.capture_path or self.default_path('.coret')),'CoreT 会话 (*.coret)')
        if not path: return False
        if not path.endswith('.coret'): path+='.coret'
        try: self.save_to(path); return True
        except Exception as e: self.show_error(e); return False

    def save_csv(self):
        if not self.capture: return
        path,_=QFileDialog.getSaveFileName(self,'导出全部原始样本',self.default_path('.csv'),'CSV (*.csv)')
        if path:
            try:
                if not path.endswith('.csv'): path+='.csv'
                export_csv(path,self.capture); self.statusBar().showMessage('CSV 已导出：'+path)
            except Exception as e: self.show_error(e)

    def load_from(self,path):
        if self.state.get('connected'): raise ValueError('请先断开连接再回放文件')
        capture=load_capture(path)
        self.operation_error = ''
        self.capture=capture; self.capture_path=Path(path); self.replaying=True; self.dirty=False
        self.plot.set_data(capture['indices'],capture['values'],capture['metadata']['sample_rate'])
        self.plot.set_span(self.span.currentData()); self.render_metadata(capture['metadata'])
        self.badge.setText('离线回放'); self.file_hint.setText('已打开：'+Path(path).name)
        self.statusBar().showMessage('离线回放：'+str(path))
        self.reset_qc(True,replay=True); self.qc_final_pending=True
        self.qc_finish.emit(self.qc_generation,capture,True); self.update_actions()

    def open_file(self):
        if not self.confirm_replace(): return
        path,_=QFileDialog.getOpenFileName(self,'打开 CoreT 会话',str(self.data_dir),'CoreT 会话 (*.coret)')
        if path:
            try: self.load_from(path)
            except Exception as e: self.show_error(e)

    def save_image(self):
        path,_=QFileDialog.getSaveFileName(self,'导出当前波形视图',self.default_path('.png'),'PNG (*.png)')
        if path:
            if not path.endswith('.png'): path+='.png'
            if not self.plot.grab().save(path,'PNG'): self.show_error('图片保存失败')
            else: self.statusBar().showMessage('波形视图已保存：'+path)

    def auto_y(self, checked):
        self.plot.auto_y=checked; self.plot.update()

    def show_error(self, error):
        QMessageBox.warning(self,'操作未完成',str(error))

    def show_help(self):
        QMessageBox.information(self,'快速使用','质检：连接后按 F7，使用当前选择的输入量程，固定 20 秒采集 / 4 Hz。开始前可选择量程，采集期间不可切换。确认出击后连续测试 10 秒，不要抬起；中断后本轮锁定不合格，须重新开始。连续测试完成后等待数据校验，再显示合格。F5 仍为普通采集。\n\n1. 先配套烧录 0.3 联动固件，再连接 USB 数据口；无需设备时可选演示（不驱动硬件）。\n2. 选择测量时间与电磁阀 1～6 Hz（默认 4 Hz），高电平固定 40 ms。开始测量（F5）同步开阀，自动完成、停止（F6）或采集故障会关阀。\n3. 自动结束或提前停止（F6）后，可保存会话（Ctrl+S）。\n4. 滚轮缩放、拖动平移、双击全览。\n5. F11 全屏，Esc 退出；Ctrl+M 最大化/还原。\n6. Ctrl+B 收起设置，Ctrl+L 查看日志与诊断详情。\n7. 断开后可打开 .coret 离线回放。\n\n纵轴仍为原始码，横轴按标称采样率计算；不是电压标定值。')

    def show_about(self):
        QMessageBox.about(self,'关于 CoreT',f'CoreT 采集助手 {__version__}\n本地单通道采集工具 · Linux x86_64\n\nQt / PySide6 桌面界面，协议层复用已验证的 CoreT 接收器。\n不修改或烧录板端固件，不启动 HTTP 服务。\nQt / PySide6 使用 LGPLv3，第三方声明随发行包提供。')

    def _restore_settings(self):
        self.preferred_port=''
        try:
            p=self.data_dir/'settings.json'
            if not p.exists() or p.stat().st_size>8192: return
            s=json.loads(p.read_text(encoding='utf-8'))
            if not isinstance(s,dict): return
            idx = self.duration.findData(s.get('duration'))
            if idx >= 0: self.duration.setCurrentIndex(idx)
            idx=self.range.findData(s.get('range'))
            if idx>=0: self.range.setCurrentIndex(idx)
            self.preferred_port=s.get('port','') if isinstance(s.get('port',''),str) else ''
        except (ValueError,OSError): pass

    def _save_settings(self):
        s={'duration':self.duration.currentData(),'range':self.range.currentData(),'port':self.port.currentData() or ''}
        atomic_write(self.data_dir/'settings.json',json.dumps(s).encode())

    def closeEvent(self,event):
        if self.allow_close:
            event.accept(); return
        event.ignore()
        if self.closing: return
        if self.state.get('phase') in ACTIVE:
            answer=QMessageBox.question(self,'停止并退出','采集正在进行。是否发送 STOP、等待排空后退出？本次数据将尝试保存到退出恢复会话。',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
            if answer!=QMessageBox.StandardButton.Yes: return
            self.recover_on_exit=True
        else:
            if not self.confirm_replace(): return
            self.recover_on_exit=False
        self.begin_shutdown()

    def begin_shutdown(self):
        self.closing=True; self.qc_generation+=1; self.update_actions(); self.notice.setText('正在停止通信并释放串口，请稍候…')
        self.shutdown_requested.emit()

    def _closed(self):
        self.qc_thread.quit()
        if not self.qc_thread.wait(5000):
            QTimer.singleShot(100,self._closed); return
        self.qc_final_pending=False
        try:
            if getattr(self,'recover_on_exit',False) and self.capture:
                self.save_to(self.default_path('-exit-recovery.coret'))
            self._save_settings()
        except Exception as e:
            QMessageBox.warning(self,'保存未完成','通信已停止，但保存失败：'+str(e))
        self.allow_close=True; self.close()
