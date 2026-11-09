"""Regression: light controls must remain legible on a dark system palette."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QStyle, QStyleOptionComboBox, QToolButton)
from coret.theme import apply_theme
from coret.window import MainWindow

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
GROUPS = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
          QPalette.ColorGroup.Disabled)
R = QPalette.ColorRole


def luminance(color):
    channels = [color.redF(), color.greenF(), color.blueF()]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    return sum(v * w for v, w in zip(linear, (.2126, .7152, .0722)))


def contrast(a, b):
    values = sorted((luminance(a), luminance(b)))
    return (values[1] + .05) / (values[0] + .05)


def dark_palette():
    palette = QPalette()
    for group in GROUPS:
        for role in (R.Window, R.Base, R.Button, R.AlternateBase):
            palette.setColor(group, role, QColor('#252525'))
        for role in (R.WindowText, R.Text, R.ButtonText):
            palette.setColor(group, role, QColor('#f4f4f4'))
    return palette


def wait_until(predicate):
    end = time.monotonic() + 4
    while time.monotonic() < end:
        APP.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('GUI condition timed out')


def snapshot(widget, name):
    directory = os.environ.get('CORET_THEME_EVIDENCE')
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(path / (name + '.png')))


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.original_palette = APP.palette()
        APP.setPalette(dark_palette())
        self.assertEqual(APP.palette().color(R.Text).name(), '#f4f4f4')
        apply_theme(APP)
        self.tmp = tempfile.TemporaryDirectory()
        self.w = MainWindow(Path(self.tmp.name))
        self.w.show()
        wait_until(lambda: self.w.worker.controller is not None)

    def tearDown(self):
        self.w.dirty = False
        for combo in (self.w.mode, self.w.port, self.w.range, self.w.span):
            combo.hidePopup()
        self.w.begin_shutdown()
        wait_until(lambda: not self.w.thread.isRunning())
        self.w.deleteLater()
        APP.processEvents()
        self.tmp.cleanup()
        APP.setPalette(self.original_palette)

    def assert_readable(self, widget, foreground=R.Text, background=R.Base):
        widget.ensurePolished()
        for group in GROUPS:
            ratio = contrast(widget.palette().color(group, foreground),
                             widget.palette().color(group, background))
            self.assertGreaterEqual(ratio, 3 if group == QPalette.ColorGroup.Disabled else 4.5,
                                    f'{type(widget).__name__} {group}: contrast={ratio:.2f}')

    def test_application_palette_all_states_and_tooltips(self):
        for foreground, background in ((R.Text, R.Base), (R.WindowText, R.Window),
                (R.ButtonText, R.Button), (R.HighlightedText, R.Highlight),
                (R.ToolTipText, R.ToolTipBase)):
            for group in GROUPS:
                self.assertGreaterEqual(contrast(APP.palette().color(group, foreground),
                    APP.palette().color(group, background)), 3 if group == GROUPS[2] else 4.5)

    def test_selected_text_is_actually_rendered(self):
        for combo in (self.w.mode, self.w.range, self.w.span):
            self.assertTrue(combo.currentText())
            self.assert_readable(combo, R.ButtonText, R.Button)
            option = QStyleOptionComboBox()
            combo.initStyleOption(option)
            rect = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox,
                option, QStyle.SubControl.SC_ComboBoxEditField, combo).adjusted(2, 2, -2, -2)
            image = combo.grab().toImage()
            dark_pixels = sum(luminance(image.pixelColor(x, y)) < .3
                for y in range(rect.top(), rect.bottom() + 1)
                for x in range(rect.left(), rect.right() + 1))
            self.assertGreater(dark_pixels, 20, combo.currentText())
        snapshot(self.w, 'dark-inherited-fixed-window')

    def test_all_dropdown_popups_have_readable_items(self):
        self.w.mode.setCurrentIndex(self.w.mode.findData('serial'))
        self.w.on_ports([{'native_usb': True, 'device': '/dev/TEST-NOT-OPENED'}])
        for name in ('mode', 'port', 'range', 'span'):
            combo = getattr(self.w, name)
            combo.showPopup()
            APP.processEvents()
            view = combo.view()
            self.assertTrue(view.isVisible())
            self.assert_readable(view)
            self.assert_readable(view, R.HighlightedText, R.Highlight)
            for row in range(combo.count()):
                self.assertTrue(combo.itemText(row))
            snapshot(view.window(), 'dropdown-' + name)
            combo.hidePopup()
        self.assertFalse(self.w.state['connected'])

    def test_keyboard_selection_and_spinbox_edit(self):
        self.w.range.setCurrentIndex(0)
        self.w.range.showPopup()
        APP.processEvents()
        QTest.keyClick(self.w.range.view(), Qt.Key.Key_Down)
        QTest.keyClick(self.w.range.view(), Qt.Key.Key_Return)
        self.assertEqual(self.w.range.currentIndex(), 1)
        self.assertEqual(self.w.range.currentData(), 1)
        self.w.duration.setCurrentIndex(4)
        self.assertEqual(self.w.duration.currentData(), 30000)
        self.assertEqual(self.w.duration.currentText(), '30 秒')
        self.assert_readable(self.w.duration)

    def test_buttons_checkbox_and_disabled_controls(self):
        widgets = self.w.findChildren(QPushButton) + self.w.findChildren(QToolButton)
        for widget in widgets:
            self.assert_readable(widget, R.ButtonText, R.Button)
        self.assert_readable(self.w.y_check, R.WindowText, R.Window)
        for widget in (self.w.range, self.w.duration):
            widget.setEnabled(False)
            APP.processEvents()
            self.assert_readable(widget)
        snapshot(self.w, 'disabled-controls')

    def test_real_save_confirmation_cancel_preserves_capture(self):
        self.w.capture = {'sentinel': 'unsaved'}
        self.w.dirty = True
        errors = []
        def inspect_dialog():
            dialog = APP.activeModalWidget()
            try:
                self.assertIsInstance(dialog, QMessageBox)
                for button in dialog.buttons():
                    self.assertTrue(button.text())
                    self.assert_readable(button, R.ButtonText, R.Button)
                for label in dialog.findChildren(QLabel):
                    self.assert_readable(label, R.WindowText, R.Window)
                snapshot(dialog, 'save-confirmation')
                snapshot(self.w, 'inactive-window-behind-dialog')
            except Exception as error:
                errors.append(error)
            finally:
                if isinstance(dialog, QMessageBox):
                    dialog.button(QMessageBox.StandardButton.Cancel).click()
        QTimer.singleShot(50, inspect_dialog)
        self.assertFalse(self.w.confirm_replace())
        if errors:
            raise errors[0]
        self.assertTrue(self.w.dirty)
        self.assertEqual(self.w.capture, {'sentinel': 'unsaved'})

    def test_qt_file_dialog_fields(self):
        dialog = QFileDialog(self.w, '保存会话', self.tmp.name)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilter('CoreT 会话 (*.coret)')
        dialog.selectFile('test.coret')
        dialog.show()
        APP.processEvents()
        try:
            for editor in dialog.findChildren(QLineEdit):
                self.assert_readable(editor)
            for button in dialog.findChildren(QPushButton):
                self.assert_readable(button, R.ButtonText, R.Button)
            snapshot(dialog, 'qt-save-file-dialog')
        finally:
            dialog.reject()
            dialog.deleteLater()

    def test_light_start_and_reapply_are_stable(self):
        expected = APP.palette()
        APP.setPalette(APP.style().standardPalette())
        apply_theme(APP)
        apply_theme(APP)
        for group in GROUPS:
            for role in (R.Text, R.Base, R.ButtonText, R.Button, R.Window, R.WindowText):
                self.assertEqual(APP.palette().color(group, role), expected.color(group, role))


if __name__ == '__main__':
    unittest.main(verbosity=2)
