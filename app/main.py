"""アプリのブートストラップ。"""
from __future__ import annotations

import logging
import sys

from PyQt5 import QtCore, QtWidgets

from app import config
from app.ui.main_window import DARK_QSS, MainWindow


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts, True)
    app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setStyleSheet(DARK_QSS)

    config.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    window = MainWindow()
    window.show()
    # QVTKRenderWindowInteractor は macOS では show() の後で初期化する必要がある
    window.viewer3d.initialize()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
