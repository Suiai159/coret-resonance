"""Cross-platform checks for Windows support. No real serial device is opened."""
from array import array
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coret.runtime import prepare_headless_fonts
prepare_headless_fonts()
from PySide6.QtWidgets import QApplication
from coret import runtime
from coret.controller import Controller, ports
from coret.qc_algorithm.coret_qc_v2 import load_profile, save_profile
from coret.qc_engine import load_configuration
from coret.storage import export_csv, load_capture, save_capture
from coret.theme import plot_font
from coret.window import MainWindow

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class PathTests(unittest.TestCase):
    def test_windows_uses_local_app_data_even_when_frozen(self):
        target = str(Path(tempfile.gettempdir()) / '中文 用户' / 'CoreT')
        with patch.object(runtime, 'sys', SimpleNamespace(platform='win32', frozen=True)), patch.object(
                runtime.QStandardPaths, 'writableLocation', return_value=target):
            self.assertEqual(runtime.default_data_directory(), Path(target))

    def test_empty_windows_path_is_not_silently_current_directory(self):
        with patch.object(runtime, 'sys', SimpleNamespace(platform='win32')), patch.object(
                runtime.QStandardPaths, 'writableLocation', return_value=''):
            with self.assertRaises(OSError):
                runtime.default_data_directory()

    def test_linux_portable_directory_is_preserved(self):
        with patch.object(runtime, 'sys', SimpleNamespace(platform='linux')):
            self.assertEqual(runtime.default_data_directory(), Path(runtime.__file__).resolve().parents[1] / 'user-data')
        executable = Path(tempfile.gettempdir()) / 'CoreT' / 'CoreT-Desktop'
        with patch.object(runtime, 'sys', SimpleNamespace(platform='linux', frozen=True, executable=str(executable))):
            self.assertEqual(runtime.default_data_directory(), executable.parent / 'user-data')

    def test_utf8_diagnostic_log_in_unicode_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / '中文 空格目录'
            logger = runtime.configure_logging(directory)
            try:
                logger.error('Windows 中文错误：串口被占用')
                for handler in logger.handlers:
                    handler.flush()
                self.assertIn('中文错误', (directory / 'runtime.log').read_text(encoding='utf-8'))
            finally:
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    handler.close()


class SerialTests(unittest.TestCase):
    def setUp(self):
        self.c = Controller()
        self.fake = Mock()
        self.fake.read.return_value = b''
        self.fake.write.side_effect = len

    def tearDown(self):
        # Test doubles never need a real-device STOP/drain handshake.
        self.c.quit.set()
        self.c.thread.join(2)
        if self.c.port is not None:
            self.c.port.close()

    def connect(self, platform_name):
        with patch('coret.controller.os', SimpleNamespace(name=platform_name)), patch(
                'coret.controller.ports', return_value=[{'device': 'COM12'}]), patch(
                'serial.Serial', return_value=self.fake) as serial_factory:
            self.c.request('connect', mode='serial', port='COM12')
            return serial_factory.call_args.kwargs

    def test_windows_high_number_com_and_reset_lines(self):
        options = self.connect('nt')
        self.assertNotIn('exclusive', options)
        self.assertEqual(options['baudrate'], 115200)
        self.assertEqual(options['timeout'], 0)
        self.assertEqual(options['write_timeout'], .2)
        self.assertEqual(self.fake.port, 'COM12')
        self.assertIs(self.fake.dtr, False)
        self.assertIs(self.fake.rts, False)
        self.fake.open.assert_called_once_with()
        self.assertEqual(self.c.phase, 'syncing')
        self.assertFalse(self.c.run_open)

    def test_posix_retains_exclusive_open(self):
        self.assertIs(self.connect('posix')['exclusive'], True)

    def test_failed_open_releases_handle_and_can_retry(self):
        self.fake.open.side_effect = OSError('Access denied / port in use')
        with self.assertRaisesRegex(ValueError, 'Access denied'):
            self.connect('nt')
        self.fake.close.assert_called_once_with()
        self.assertIsNone(self.c.port)
        self.assertEqual(self.c.phase, 'error')
        self.fake.open.side_effect = None
        self.connect('nt')
        self.assertIs(self.c.port, self.fake)

    def test_unenumerated_port_is_not_opened(self):
        with patch('coret.controller.ports', return_value=[]), patch('serial.Serial') as factory:
            with self.assertRaises(ValueError):
                self.c.request('connect', mode='serial', port='COM99')
            factory.assert_not_called()

    def test_native_usb_identification_preserved_on_com_ports(self):
        devices = [SimpleNamespace(device='COM12', description='USB Serial Device',
                                  serial_number='测试', vid=0x303a, pid=0x1001),
                   SimpleNamespace(device='COM3', description='UART log', serial_number='1', vid=0x10c4, pid=0xea60)]
        with patch('serial.tools.list_ports.comports', return_value=devices):
            result = ports()
        self.assertTrue(result[0]['native_usb'])
        self.assertFalse(result[1]['native_usb'])
        self.assertEqual(result[0]['device'], 'COM12')


class EncodingTests(unittest.TestCase):
    def test_profile_json_is_explicit_utf8_under_legacy_code_page(self):
        original = Path.read_text
        def legacy_read(path, *args, **kwargs):
            kwargs.setdefault('encoding', 'cp936')
            return original(path, *args, **kwargs)
        profile, config, digest = load_configuration()
        profile['windows_test'] = '中文路径 / 测试配置'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '中文 配置.json'
            save_profile(profile, path)
            with patch.object(Path, 'read_text', legacy_read):
                self.assertEqual(load_profile(path)['windows_test'], profile['windows_test'])
                self.assertEqual(load_configuration()[2], digest)
            self.assertIn('中文路径', path.read_text(encoding='utf-8'))

    def test_unicode_capture_csv_and_linux_json_interchange(self):
        metadata = dict(sample_rate=100000, samples=3, crc_errors=0, index_gaps=0,
                        bad_headers=0, garbage_bytes=0, passed=True, errors=[],
                        events=[{'time': '00:00:00', 'text': '采集完成'}])
        capture = dict(schema='coret.capture/1', metadata=metadata, indices=[0, 1, 2],
                       values=[32768, 33259, 33749], rx_hex='', tx_hex='')
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / '中文 空格'
            directory.mkdir()
            path = directory / '数据.coret'
            save_capture(path, capture)
            self.assertEqual(load_capture(path)['metadata'], metadata)
            export_csv(directory / '数据.csv', capture)
            self.assertEqual((directory / '数据.csv').read_text(encoding='utf-8').splitlines()[2], '1,0.00001000,33259')
            legacy = directory / 'Linux 旧格式.coret'
            legacy.write_text(json.dumps(capture, ensure_ascii=False), encoding='utf-8')
            self.assertEqual(load_capture(legacy), capture)

    def test_port_preference_roundtrips(self):
        with tempfile.TemporaryDirectory() as temp:
            window = SimpleNamespace(data_dir=Path(temp), duration=Mock(), range=Mock(), port=Mock())
            window.duration.currentData.return_value = 15000
            window.range.currentData.return_value = 4
            window.port.currentData.return_value = 'COM12'
            window.duration.findData.return_value = 2
            window.range.findData.return_value = 4
            MainWindow._save_settings(window)
            MainWindow._restore_settings(window)
            self.assertEqual(window.preferred_port, 'COM12')
            window.duration.setCurrentIndex.assert_called_once_with(2)
            window.range.setCurrentIndex.assert_called_once_with(4)

    def test_plot_fonts_are_installed(self):
        from PySide6.QtGui import QFontInfo
        for fixed in (False, True):
            font = plot_font(12, fixed=fixed)
            self.assertEqual(font.pointSize(), 12)
            self.assertTrue(QFontInfo(font).family())


if __name__ == '__main__':
    unittest.main(verbosity=2)
