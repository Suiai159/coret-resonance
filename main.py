#!/usr/bin/env python3
"""Native launcher. No web server, no automatic device connection."""
import argparse
import os
from pathlib import Path
import sys
from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox
from coret import __version__
from coret.theme import apply_theme
from coret.window import MainWindow


def main():
    parser=argparse.ArgumentParser(description='CoreT desktop acquisition assistant')
    parser.add_argument('--version',action='version',version=__version__)
    parser.add_argument('--open',type=Path,help='Open an existing .coret capture; no device I/O')
    parser.add_argument('--data-dir',type=Path,help='Settings and default capture directory')
    parser.add_argument('--smoke-test',type=Path,help='Explicit PC-only GUI smoke test; write evidence and exit')
    parser.add_argument('--qc-replay-check',type=Path,help='Explicit offline QC package check; requires --open; never connects')
    args=parser.parse_args()
    if args.qc_replay_check and (not args.open or args.smoke_test):
        parser.error('--qc-replay-check requires --open and cannot be combined with --smoke-test')
    app=QApplication([sys.argv[0]])
    app.setApplicationName('CoreT Desktop'); app.setApplicationVersion(__version__)
    app.setOrganizationName('CoreT')
    apply_theme(app)
    resources=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
    app.setWindowIcon(QIcon(str(resources/'assets/coret.svg')))
    base=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
    data=args.data_dir or (args.qc_replay_check/'user-data' if args.qc_replay_check else args.smoke_test/'user-data' if args.smoke_test else base/'user-data')
    try:
        window=MainWindow(data)
    except OSError as e:
        QMessageBox.critical(None,'无法启动','无法写入数据目录，请使用 --data-dir 指定可写路径。\n'+str(e))
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
    if args.smoke_test:
        from coret.smoke import SmokeTest
        smoke=SmokeTest(window,args.smoke_test)
        QTimer.singleShot(0,smoke.start)
    return app.exec()


if __name__=='__main__':
    sys.exit(main())
