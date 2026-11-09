"""Optional offline learning panel. Explicit import, fit, then separate activation."""
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QLineEdit, QCheckBox, QPlainTextEdit, QFileDialog, QMessageBox)
from .qc_learning import CORES, LEVELS, SLOTS, ProfileStore, learn_reference, validate_bundle
from .storage import atomic_write
from .qc_learning import canonical


class LearningJob(QThread):
    progress = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, references, backgrounds, negatives, name, parent=None):
        super().__init__(parent)
        self.inputs = references, backgrounds, negatives, name

    def run(self):
        try:
            self.succeeded.emit(learn_reference(*self.inputs, progress=self.progress.emit))
        except Exception as e:
            self.failed.emit('学习未完成，当前启用配置不变：' + str(e))


class LearningDialog(QDialog):
    def __init__(self, store, active, error='', parent=None):
        super().__init__(parent)
        self.store = store; self.candidate = None; self.changed = False; self.job = None
        self.references = {}; self.backgrounds = []; self.negatives = []
        self.setWindowTitle('参考学习（可选）'); self.resize(760, 620)
        layout = QVBoxLayout(self)
        text = ('可选功能：不学习就使用默认配置。仅导入已保存的记录，不连接或驱动设备。\n'
                '三个不同合格核心件 × 最低合格 / 中等 / 最高三档，每格 20 秒、4 Hz、100 kS/s。\n'
                '本次所有记录须为同一量程（±2.560 V 或 ±5.120 V）；三档合并学习允许范围。')
        description = QLabel(text); description.setWordWrap(True); layout.addWidget(description)
        self.active_label = QLabel('当前：' + ('配置损坏，质检已禁止；请恢复默认或启用有效配置' if error else
                                            active['name'] if active else '默认参考（已验证版本）'))
        self.active_label.setWordWrap(True); layout.addWidget(self.active_label)
        self.name = QLineEdit('三个合格件参考'); self.name.setMaxLength(80)
        row = QHBoxLayout(); row.addWidget(QLabel('配置名称')); row.addWidget(self.name); layout.addLayout(row)
        grid = QGridLayout(); self.slot_buttons = {}
        for col, title in enumerate(('最低合格', '中等', '最高'), 1): grid.addWidget(QLabel(title), 0, col)
        for row, core in enumerate(CORES, 1):
            grid.addWidget(QLabel(core), row, 0)
            for col, level in enumerate(LEVELS, 1):
                button = QPushButton('选择 .coret'); button.setMinimumWidth(150)
                button.clicked.connect(lambda checked=False, slot=(core, level): self.select_reference(slot))
                self.slot_buttons[core, level] = button; grid.addWidget(button, row, col)
        layout.addLayout(grid)
        self.background_button = QPushButton('选择背景：驱动但不接触（必选）')
        self.negative_button = QPushButton('选择非合格验证记录（可选，不参与拟合）')
        self.background_button.clicked.connect(lambda: self.select_controls(False))
        self.negative_button.clicked.connect(lambda: self.select_controls(True))
        layout.addWidget(self.background_button); layout.addWidget(self.negative_button)
        self.confirmed = QCheckBox('我确认编号对应三个不同的已知合格核心件，最低档仍属合格，且背景标签正确')
        layout.addWidget(self.confirmed)
        self.train_button = QPushButton('生成候选配置并验证（不自动启用）'); self.train_button.clicked.connect(self.start_learning)
        layout.addWidget(self.train_button)
        self.report = QPlainTextEdit(); self.report.setReadOnly(True); self.report.setMinimumHeight(100)
        layout.addWidget(self.report, 1)
        row = QHBoxLayout()
        self.import_button = QPushButton('导入候选配置'); self.import_button.clicked.connect(self.import_candidate)
        self.export_button = QPushButton('导出候选配置'); self.export_button.clicked.connect(self.export_candidate)
        self.activate_button = QPushButton('启用候选配置'); self.activate_button.clicked.connect(self.activate)
        self.default_button = QPushButton('恢复默认'); self.default_button.clicked.connect(self.restore_default)
        self.close_button = QPushButton('关闭'); self.close_button.clicked.connect(self.reject)
        for button in (self.import_button, self.export_button, self.activate_button, self.default_button, self.close_button): row.addWidget(button)
        layout.addLayout(row)
        if error: self.report.appendPlainText(error)
        if store.candidate_path.exists():
            try: self.candidate = store.load_candidate(); self.describe_candidate()
            except Exception as e: self.report.appendPlainText('旧候选不可用：' + str(e))
        self.set_busy(False)

    def running(self):
        return self.job is not None and self.job.isRunning()

    def set_busy(self, busy):
        for widget in list(self.slot_buttons.values()) + [self.name, self.background_button, self.negative_button,
                self.confirmed, self.train_button, self.import_button, self.default_button, self.close_button]:
            widget.setEnabled(not busy)
        self.activate_button.setEnabled(not busy and self.candidate is not None)
        self.export_button.setEnabled(not busy and self.candidate is not None)

    def select_reference(self, slot):
        path, _ = QFileDialog.getOpenFileName(self, '选择 ' + '/'.join(slot), str(self.store.directory), 'CoreT (*.coret)')
        if path:
            self.references[slot] = path
            self.slot_buttons[slot].setText(Path(path).name[:26]); self.slot_buttons[slot].setToolTip(path)

    def select_controls(self, negative):
        paths, _ = QFileDialog.getOpenFileNames(self, '选择验证记录' if negative else '选择背景记录', str(self.store.directory), 'CoreT (*.coret)')
        if paths:
            if negative:
                self.negatives = paths; self.negative_button.setText(f'非合格验证记录：{len(paths)} 条（仅验证）')
            else:
                self.backgrounds = paths; self.background_button.setText(f'背景记录：{len(paths)} 条')

    def start_learning(self):
        if self.running(): return
        if set(self.references) != set(SLOTS) or not self.backgrounds or not self.confirmed.isChecked() or not self.name.text().strip():
            self.report.appendPlainText('请补齐 9 条参考记录、背景和名称，并确认样本身份。'); return
        refs = [dict(core=core, level=level, path=self.references[core, level]) for core, level in SLOTS]
        self.report.clear(); self.set_busy(True)
        # Keep the previous candidate/active profile intact until the new candidate saves successfully.
        self.job = LearningJob(refs, list(self.backgrounds), list(self.negatives), self.name.text().strip(), self)
        self.job.progress.connect(self.report.appendPlainText)
        self.job.failed.connect(self.failed)
        self.job.succeeded.connect(self.completed)
        self.job.finished.connect(lambda: self.set_busy(False))
        self.job.start()

    def failed(self, message):
        self.report.appendPlainText(message)
        if self.candidate is not None:
            self.report.appendPlainText('没有生成新候选；仍保留此前候选：' + self.candidate['name'])

    def completed(self, bundle):
        try:
            self.store.save_candidate(bundle)
            self.candidate = bundle; self.describe_candidate()
        except Exception as e:
            self.report.appendPlainText('候选保存失败，原配置不变：' + str(e))

    def describe_candidate(self):
        b = self.candidate; p = b['detector']; low, high = p['allowed_window_median_mv']
        volts = '±2.560 V' if b['contract']['range'] == 4 else '±5.120 V'
        self.report.appendPlainText(f"候选：{b['name']} · {volts}\n参考 9/9 通过，跨核心件验证 9/9 通过。\n"
            f"窗口响应参考：{low:.1f}～{high:.1f} mV；不是物理力值。\n"
            f"非合格验证记录：{len(b['negative_checks'])} 条；未覆盖的缺陷不保证检出。\n"
            f"配置标识：{b['sha256'][:12]}。候选只有单独点击启用后才生效。")

    def import_candidate(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入参考候选', str(self.store.directory), 'JSON (*.json)')
        if not path: return
        try:
            candidate = validate_bundle(ProfileStore.read(path)); self.store.save_candidate(candidate)
            self.candidate = candidate; self.describe_candidate(); self.set_busy(False)
        except Exception as e: self.report.appendPlainText('导入失败，原配置不变：' + str(e))

    def export_candidate(self):
        if self.candidate is None: return
        path, _ = QFileDialog.getSaveFileName(self, '导出参考候选', str(self.store.directory / 'reference.json'), 'JSON (*.json)')
        if path:
            try: atomic_write(path, canonical(validate_bundle(self.candidate)))
            except Exception as e: self.report.appendPlainText('导出失败：' + str(e))

    def activate(self):
        if self.running() or self.candidate is None: return
        if QMessageBox.question(self, '启用参考配置', '启用后，后续 F7 及回放使用此配置。\n已保存的历史判定不会被自动改写。是否启用？',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        try:
            self.store.activate(self.candidate); self.changed = True; self.accept()
        except Exception as e: self.report.appendPlainText('启用失败，原配置不变：' + str(e))

    def restore_default(self):
        if self.running(): return
        if QMessageBox.question(self, '恢复默认参考', '恢复当前已验证版本的默认参数和 ±2.560 V 质检量程？候选配置仍保留。',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        try:
            self.store.restore_default(); self.changed = True; self.accept()
        except Exception as e: self.report.appendPlainText('恢复失败：' + str(e))

    def reject(self):
        if self.running():
            self.report.appendPlainText('正在离线计算，请等待完成后关闭。'); return
        super().reject()

    def closeEvent(self, event):
        if self.running(): event.ignore()
        else: super().closeEvent(event)
