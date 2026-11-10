"""Platform paths and diagnostics shared by source and frozen launchers."""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from PySide6.QtCore import QStandardPaths


def default_data_directory():
    """Windows installs may be read-only. Keep Linux's existing portable layout."""
    if sys.platform == 'win32':
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        if not location:
            raise OSError('Windows did not provide a writable local application data directory')
        return Path(location)
    base = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    return base / 'user-data'


def configure_logging(data_directory):
    directory = Path(data_directory)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('coret')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(directory / 'runtime.log', maxBytes=2 * 1024 * 1024,
                                  backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    return logger


def prepare_headless_fonts():
    """Qt offscreen on Windows needs an explicit font directory (not in normal UI)."""
    if sys.platform == 'win32' and os.environ.get('QT_QPA_PLATFORM') == 'offscreen':
        fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        if fonts.is_dir():
            os.environ.setdefault('QT_QPA_FONTDIR', str(fonts))


def register_headless_cjk_font():
    # The offscreen font directory scanner skips .ttc font collections. Register
    # Windows' CJK collection explicitly so headless screenshots contain glyphs.
    if sys.platform == 'win32' and os.environ.get('QT_QPA_PLATFORM') == 'offscreen':
        from PySide6.QtGui import QFontDatabase
        if 'Microsoft YaHei UI' not in QFontDatabase.families():
            fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
            for name in ('msyh.ttc', 'simsun.ttc'):
                if (fonts / name).is_file() and QFontDatabase.addApplicationFont(str(fonts / name)) >= 0:
                    break
