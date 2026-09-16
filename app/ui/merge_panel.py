"""結合と STL 書出しのパネル。"""
from __future__ import annotations

from PyQt5 import QtCore, QtWidgets

from app import config


class MergePanel(QtWidgets.QGroupBox):
    merge_requested = QtCore.pyqtSignal()
    export_requested = QtCore.pyqtSignal()
    export_dicom_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("結合 / 書出し", parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)

        blend_row = QtWidgets.QHBoxLayout()
        blend_row.addWidget(QtWidgets.QLabel("ブレンド"))
        self.blend = QtWidgets.QComboBox()
        self.blend.addItems(config.BLEND_MODES)
        self.blend.setCurrentText(config.DEFAULT_BLEND)
        blend_row.addWidget(self.blend, 1)
        layout.addLayout(blend_row)

        self.blend_hint = QtWidgets.QLabel()
        self.blend_hint.setWordWrap(True)
        self.blend_hint.setStyleSheet("color:#8b93a3; font-size:11px;")
        self.blend.currentTextChanged.connect(self._update_hint)
        layout.addWidget(self.blend_hint)
        self._update_hint(self.blend.currentText())

        self.grid_label = QtWidgets.QLabel("出力グリッド: —")
        self.grid_label.setWordWrap(True)
        self.grid_label.setStyleSheet(
            "background:#1a1d24; border:1px solid #2a2d35; border-radius:4px;"
            "padding:6px; color:#c8cedb; font-size:11px;")
        layout.addWidget(self.grid_label)

        self.merge_btn = QtWidgets.QPushButton("結合を実行")
        self.merge_btn.setStyleSheet(
            "QPushButton { font-weight:600; padding:7px; }")
        self.merge_btn.clicked.connect(self.merge_requested)
        layout.addWidget(self.merge_btn)

        layout.addWidget(self._separator())

        self.opt_box = QtWidgets.QGroupBox("書出しの詳細設定")
        self.opt_box.setCheckable(True)
        self.opt_box.setChecked(False)
        box_layout = QtWidgets.QVBoxLayout(self.opt_box)
        box_layout.setContentsMargins(6, 2, 6, 2)
        self.opt_body = QtWidgets.QWidget()
        box_layout.addWidget(self.opt_body)
        opt = QtWidgets.QFormLayout(self.opt_body)
        opt.setLabelAlignment(QtCore.Qt.AlignRight)
        opt.setContentsMargins(0, 0, 0, 0)
        opt.setSpacing(4)
        self.target_tris = QtWidgets.QSpinBox()
        self.target_tris.setRange(10_000, 20_000_000)
        self.target_tris.setSingleStep(100_000)
        self.target_tris.setValue(config.EXPORT_TARGET_TRIANGLES)
        self.target_tris.setGroupSeparatorShown(True)

        self.smooth = QtWidgets.QSpinBox()
        self.smooth.setRange(0, 100)
        self.smooth.setValue(config.EXPORT_SMOOTH_ITERATIONS)
        self.smooth.setSuffix(" 回")

        self.fill_holes = QtWidgets.QCheckBox("穴埋めを行う")
        self.high_quality = QtWidgets.QCheckBox("高品質な間引き (かなり遅い)")
        self.high_quality.setToolTip(
            "vtkQuadricDecimation を使い形状保持に優れるが、600 スライス級では "
            "5 分以上かかる。既定の vtkDecimatePro なら約 20 秒。")

        self.series_description = QtWidgets.QLineEdit()
        self.series_description.setPlaceholderText("自動 (MERGED CT …)")
        self.series_description.setToolTip(
            "DICOM の SeriesDescription。空なら自動で付ける。")

        self.dicom_threshold = QtWidgets.QCheckBox("閾値未満を空気にする")
        self.dicom_threshold.setToolTip(
            "チェックすると、現在の HU 閾値より低いボクセルを空気 (-1024 HU) に\n"
            "置き換えて書き出す。骨だけのボリュームが欲しい場合に使う。\n"
            "通常はチェックせず、元の HU をそのまま残すほうがよい。")

        opt.addRow("目標三角形数", self.target_tris)
        opt.addRow("平滑化", self.smooth)
        opt.addRow("", self.fill_holes)
        opt.addRow("", self.high_quality)
        opt.addRow(self._sub_label("DICOM"), None)
        opt.addRow("シリーズ説明", self.series_description)
        opt.addRow("", self.dicom_threshold)
        self._opt_extra = (self.series_description, self.dicom_threshold)
        # 中身ごと隠す (フィールドだけ隠すとラベルのぶん高さが残ってしまう)
        self.opt_body.setVisible(False)
        self.opt_box.toggled.connect(self.opt_body.setVisible)
        layout.addWidget(self.opt_box)

        self.export_hint = QtWidgets.QLabel()
        self.export_hint.setWordWrap(True)
        self.export_hint.setStyleSheet("color:#c9a227; font-size:11px;")
        self.export_hint.setVisible(False)
        layout.addWidget(self.export_hint)

        export_row = QtWidgets.QHBoxLayout()
        export_row.setSpacing(6)
        self.export_btn = QtWidgets.QPushButton("STL を書き出す…")
        self.export_dicom_btn = QtWidgets.QPushButton("DICOM を書き出す…")
        self.export_dicom_btn.setToolTip(
            "結合結果を CT DICOM シリーズとして書き出す。\n"
            "STL と違いボリュームそのものを保持するので、3D Slicer や\n"
            "PACS ビューアでそのまま開ける。")
        for b in (self.export_btn, self.export_dicom_btn):
            b.setStyleSheet("QPushButton { font-weight:600; padding:7px; }")
            export_row.addWidget(b)
        self.export_btn.clicked.connect(self.export_requested)
        self.export_dicom_btn.clicked.connect(self.export_dicom_requested)
        layout.addLayout(export_row)

        layout.addStretch(1)

    @staticmethod
    def _sub_label(text: str) -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet("color:#9aa3b2; font-weight:600;")
        return lbl

    @staticmethod
    def _separator():
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color:#2a2d35;")
        return line

    def _update_hint(self, text: str):
        hints = {
            "feather (推奨)": "重なりの厚い軸に沿って滑らかに遷移。継ぎ目の段差が最も出にくい。",
            "max": "重複部で大きいほうの HU を採用。骨が保たれるが境界が硬くなる。",
            "mean": "重複部を平均。ノイズは減るが輪郭がややぼける。",
        }
        self.blend_hint.setText(hints.get(text, ""))

    @property
    def blend_mode_label(self) -> str:
        return self.blend.currentText()

    def set_grid_text(self, text: str):
        self.grid_label.setText(text)

    def set_merge_enabled(self, enabled: bool, reason: str = ""):
        self.merge_btn.setEnabled(bool(enabled))
        self.merge_btn.setToolTip(
            reason if not enabled else "位置合わせ済みの 2 つをボリューム空間で融合する")

    def set_export_enabled(self, enabled: bool, reason: str = ""):
        """無効なときは、なぜ押せないのかが分かるようにする。

        「ボタンが見当たらない」と誤解されやすいので、理由を明示する。
        """
        self.export_btn.setEnabled(bool(enabled))
        self.export_dicom_btn.setEnabled(bool(enabled))
        if enabled:
            self.export_btn.setToolTip("表面メッシュを STL として書き出す")
            self.export_dicom_btn.setToolTip(
                "ボリュームを CT DICOM シリーズとして書き出す。\n"
                "3D Slicer や PACS ビューアでそのまま開ける。")
            self.export_hint.setVisible(False)
        else:
            for b in (self.export_btn, self.export_dicom_btn):
                b.setToolTip(reason)
            self.export_hint.setText(reason)
            self.export_hint.setVisible(bool(reason))

    @property
    def dicom_description(self) -> str:
        return self.series_description.text().strip()

    @property
    def dicom_apply_threshold(self) -> bool:
        return bool(self.dicom_threshold.isChecked())
