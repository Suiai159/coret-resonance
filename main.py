#!/usr/bin/env python3
"""Native launcher. No web server, no automatic device connection."""
import argparse
from pathlib import Path
import sys
from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox
from coret import __version__
from coret.theme import apply_theme
from coret.runtime import configure_logging, default_data_directory, prepare_headless_fonts
from coret.window import MainWindow


def main():
    parser=argparse.ArgumentParser(description='CoreT desktop acquisition assistant')
    parser.add_argument('--version',action='version',version=__version__)
    parser.add_argument('--open',type=Path,help='Open an existing .coret capture; no device I/O')
    parser.add_argument('--data-dir',type=Path,help='Settings and default capture directory')
    parser.add_argument('--smoke-test',type=Path,help='Explicit PC-only GUI smoke test; write evidence and exit')
    parser.add_argument('--qc-replay-check',type=Path,help='Explicit offline QC package check; requires --open; never connects')
    parser.add_argument('--learning-check',type=Path,help='Explicit isolated synthetic learning GUI check; never connects')
    args=parser.parse_args()
    if args.learning_check and (args.open or args.data_dir or args.smoke_test or args.qc_replay_check):
        parser.error('--learning-check cannot be combined with other check, open or data-dir options')
    if args.qc_replay_check and (not args.open or args.smoke_test):
        parser.error('--qc-replay-check requires --open and cannot be combined with --smoke-test')
    prepare_headless_fonts()
    app=QApplication([sys.argv[0]])
    app.setApplicationName('CoreT Desktop'); app.setApplicationVersion(__version__)
    app.setOrganizationName('CoreT')
    apply_theme(app)
    resources=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
    app.setWindowIcon(QIcon(str(resources/'assets/coret.svg')))
    try:
        data=args.data_dir or (args.learning_check/'user-data' if args.learning_check else args.qc_replay_check/'user-data' if args.qc_replay_check else args.smoke_test/'user-data' if args.smoke_test else default_data_directory())
        data=data.expanduser().resolve()
        logger=configure_logging(data)
    except OSError as e:
        QMessageBox.critical(None,'无法启动','无法写入数据目录，请使用 --data-dir 指定可写路径。\n'+str(e))
        return 1
    logger.info('Starting CoreT %s on %s; frozen=%s; data=%s', __version__, sys.platform, getattr(sys,'frozen',False), data)

    def report_exception(kind, value, tb):
        logger.error('Unhandled application exception', exc_info=(kind, value, tb))
        QMessageBox.critical(None,'运行异常',str(value)+'\n\n诊断日志：'+str(data/'runtime.log'))
    sys.excepthook=report_exception
    try:
        window=MainWindow(data)
    except Exception as e:
        logger.exception('Startup failed')
        QMessageBox.critical(None,'无法启动',str(e)+'\n\n诊断日志：'+str(data/'runtime.log'))
        return 1
    window.show()
    if args.open:
        def open_later():
            try: window.load_from(args.open)
            except Exception as e: window.show_error(e)
        QTimer.singleShot(0,open_later)
    if args.qc_replay_check:
        from coret.qc_smoke import ReplayCheck
        qc_check=ReplayCheck(window,args.qc_replay_check)
        QTimer.singleShot(0,qc_check.start)
    if args.learning_check:
        from coret.learning_smoke import LearningCheck
        learning_check=LearningCheck(window,args.learning_check)
        QTimer.singleShot(0,learning_check.start)
    if args.smoke_test:
        from coret.smoke import SmokeTest
        smoke=SmokeTest(window,args.smoke_test)
        QTimer.singleShot(0,smoke.start)
    return app.exec()


if __name__=='__main__':
    sys.exit(main())
