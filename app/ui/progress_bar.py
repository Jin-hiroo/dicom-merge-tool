"""進捗バー + キャンセルボタン。

制約1 の可視部分: 重い処理の最中でも UI が生きていることを示し、
いつでも中断できるようにする。
"""
from __future__ import annotations

from PyQt5 import QtCore, QtWidgets


class ProgressBar(QtWidgets.QWidget):
    cancel_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(14)
        self.bar.setFixedWidth(220)

        self.label = QtWidgets.QLabel("準備完了")
        self.label.setStyleSheet("color:#c8cedb;")

        # 重なり誤差はボタンを押しながら見るものなので、スクロールで隠れない
        # フッターに常時表示する。
        self.metric = QtWidgets.QLabel("")
        self.metric.setStyleSheet(
            "color:#7fd4a0; font-weight:600; padding:0 10px;")

        self.cancel_btn = QtWidgets.QPushButton("キャンセル")
        self.cancel_btn.setFixedWidth(100)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_requested)

        row.addWidget(self.bar)
        row.addWidget(self.label, 1)
        row.addWidget(self.metric, 0)
        row.addWidget(self.cancel_btn)
        self.set_busy(False)

    def set_busy(self, busy: bool):
        self.cancel_btn.setEnabled(bool(busy))
        self.bar.setVisible(bool(busy))
        if not busy:
            self.bar.setValue(0)

    def update_progress(self, pct: int, message: str = ""):
        self.bar.setValue(int(pct))
        if message:
            self.label.setText(message)

    def set_message(self, message: str):
        self.label.setText(message)

    def set_metric(self, text: str):
        self.metric.setText(text)
