"""セグメントパネル: HU 閾値と連結成分クリーニング。"""
from __future__ import annotations

from PyQt5 import QtCore, QtWidgets

from app import config


class SegmentPanel(QtWidgets.QGroupBox):
    apply_requested = QtCore.pyqtSignal()
    threshold_preview = QtCore.pyqtSignal(int)   # スライダー移動中の速報

    def __init__(self, parent=None):
        super().__init__("セグメント", parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)

        preset_row = QtWidgets.QHBoxLayout()
        preset_row.addWidget(QtWidgets.QLabel("プリセット"))
        self.preset = QtWidgets.QComboBox()
        for name, value in config.SEGMENT_PRESETS.items():
            self.preset.addItem(name, value)
        self.preset.setCurrentText(config.DEFAULT_PRESET)
        self.preset.currentIndexChanged.connect(self._on_preset)
        preset_row.addWidget(self.preset, 1)
        layout.addLayout(preset_row)

        thr_row = QtWidgets.QHBoxLayout()
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(*config.THRESHOLD_RANGE)
        self.slider.setValue(config.SEGMENT_PRESETS[config.DEFAULT_PRESET])
        self.spin = QtWidgets.QSpinBox()
        self.spin.setRange(*config.THRESHOLD_RANGE)
        self.spin.setValue(self.slider.value())
        self.spin.setSuffix(" HU")
        self.spin.setFixedWidth(92)
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        thr_row.addWidget(QtWidgets.QLabel("HU 閾値"))
        thr_row.addWidget(self.slider, 1)
        thr_row.addWidget(self.spin)
        layout.addLayout(thr_row)

        self.largest_cc = QtWidgets.QCheckBox("最大連結成分のみ (寝台・ノイズ除去)")
        layout.addWidget(self.largest_cc)

        island_row = QtWidgets.QHBoxLayout()
        self.island_enable = QtWidgets.QCheckBox("小島除去")
        self.island_size = QtWidgets.QSpinBox()
        self.island_size.setRange(1, 1_000_000)
        self.island_size.setValue(500)
        self.island_size.setSuffix(" ボクセル未満")
        self.island_size.setEnabled(False)
        self.island_enable.toggled.connect(self.island_size.setEnabled)
        island_row.addWidget(self.island_enable)
        island_row.addWidget(self.island_size, 1)
        layout.addLayout(island_row)

        self.largest_cc.setToolTip(
            "連結成分の処理は重いため、チェックした場合のみ実行されます")
        self.island_enable.setToolTip(
            "連結成分の処理は重いため、チェックした場合のみ実行されます")

        self.apply_btn = QtWidgets.QPushButton("再セグメント / プレビュー更新")
        self.apply_btn.clicked.connect(self.apply_requested)
        layout.addWidget(self.apply_btn)

        self._syncing = False

    # ------------------------------------------------------------------
    def _on_preset(self):
        value = self.preset.currentData()
        if value is not None:
            self.set_threshold(int(value))

    def _on_slider(self, value):
        if self._syncing:
            return
        self._syncing = True
        self.spin.setValue(value)
        self._syncing = False
        self.threshold_preview.emit(int(value))

    def _on_spin(self, value):
        if self._syncing:
            return
        self._syncing = True
        self.slider.setValue(value)
        self._syncing = False
        self.threshold_preview.emit(int(value))

    # ------------------------------------------------------------------
    @property
    def threshold(self) -> int:
        return int(self.slider.value())

    def set_threshold(self, value: int):
        self._syncing = True
        self.slider.setValue(int(value))
        self.spin.setValue(int(value))
        self._syncing = False
        self.threshold_preview.emit(int(value))

    @property
    def min_island_voxels(self) -> int:
        return int(self.island_size.value()) if self.island_enable.isChecked() else 0

    @property
    def use_largest_component(self) -> bool:
        return bool(self.largest_cc.isChecked())
