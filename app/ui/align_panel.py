"""位置合わせパネル。ユーザー要求の中核。

X / Y / Z の移動ボタンと Roll / Pitch / Yaw の回転ボタンで Moving を動かす。
ボタンを押しても *メッシュは作り直さない* — 変換行列だけを更新するので
即座に反映され、UI が固まらない。
"""
from __future__ import annotations

from PyQt5 import QtCore, QtGui, QtWidgets

from app import config
from app.core.transform import RigidTransform, UndoStack

STEP_STYLE = """
QPushButton { font-size: 15px; font-weight: 600; padding: 2px; }
"""


class NudgeRow(QtWidgets.QWidget):
    """[−] ラベル [＋] の 1 行。"""
    nudged = QtCore.pyqtSignal(str, float)      # key, 符号(+1/-1)

    def __init__(self, key: str, label: str, color: str, parent=None):
        super().__init__(parent)
        self.key = key
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        minus = QtWidgets.QPushButton("−")
        plus = QtWidgets.QPushButton("＋")
        for b in (minus, plus):
            b.setFixedWidth(38)
            b.setFixedHeight(24)
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(90)
            b.setStyleSheet(STEP_STYLE)

        name = QtWidgets.QLabel(label)
        name.setAlignment(QtCore.Qt.AlignCenter)
        name.setStyleSheet(f"color:{color}; font-weight:600;")
        name.setMinimumWidth(74)

        self.value = QtWidgets.QDoubleSpinBox()
        self.value.setDecimals(2)
        self.value.setRange(-9999.0, 9999.0)
        self.value.setSingleStep(0.1)
        self.value.setFixedWidth(88)
        self.value.setFixedHeight(24)
        self.value.setKeyboardTracking(False)

        row.addWidget(minus)
        row.addWidget(name, 1)
        row.addWidget(plus)
        row.addWidget(self.value)

        minus.clicked.connect(lambda: self.nudged.emit(self.key, -1.0))
        plus.clicked.connect(lambda: self.nudged.emit(self.key, +1.0))


class AlignPanel(QtWidgets.QGroupBox):
    """Moving の剛体変換を編集する。"""

    transform_changed = QtCore.pyqtSignal()
    dice_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("位置合わせ", parent)
        self.transform = RigidTransform()
        self.undo_stack = UndoStack()
        self._updating = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(3)

        # --- 初期配置 --------------------------------------------------
        init_row = QtWidgets.QHBoxLayout()
        init_row.addWidget(QtWidgets.QLabel("初期配置"))
        self.init_mode = QtWidgets.QComboBox()
        self.init_mode.addItems(["DICOM 患者座標のまま", "重心を一致させる",
                                 "Z 方向に積む"])
        init_row.addWidget(self.init_mode, 1)
        layout.addLayout(init_row)

        # --- 平行移動 --------------------------------------------------
        self.translate_step = QtWidgets.QComboBox()
        for s in config.TRANSLATE_STEPS_MM:
            self.translate_step.addItem(f"{s:g} mm", s)
        self.translate_step.setCurrentIndex(
            config.TRANSLATE_STEPS_MM.index(config.DEFAULT_TRANSLATE_STEP))
        layout.addLayout(self._header_row("平行移動 (mm)", self.translate_step))

        self.rows: dict[str, NudgeRow] = {}
        for key, label, color in (("tx", "X 方向", "#e06c75"),
                                  ("ty", "Y 方向", "#98c379"),
                                  ("tz", "Z 方向", "#61afef")):
            r = NudgeRow(key, label, color)
            r.nudged.connect(self._on_nudge)
            r.value.valueChanged.connect(self._on_value_edited)
            self.rows[key] = r
            layout.addWidget(r)

        # --- 回転 ------------------------------------------------------
        self.rotate_step = QtWidgets.QComboBox()
        for s in config.ROTATE_STEPS_DEG:
            self.rotate_step.addItem(f"{s:g}°", s)
        self.rotate_step.setCurrentIndex(
            config.ROTATE_STEPS_DEG.index(config.DEFAULT_ROTATE_STEP))
        layout.addLayout(self._header_row("回転 (度) — 重心まわり", self.rotate_step))

        for key, label, color in (("rx", "Roll (X)", "#e06c75"),
                                  ("ry", "Pitch (Y)", "#98c379"),
                                  ("rz", "Yaw (Z)", "#61afef")):
            r = NudgeRow(key, label, color)
            r.value.setRange(-180.0, 180.0)
            r.nudged.connect(self._on_nudge)
            r.value.valueChanged.connect(self._on_value_edited)
            self.rows[key] = r
            layout.addWidget(r)

        # --- 操作 ------------------------------------------------------
        btn_row = QtWidgets.QHBoxLayout()
        self.undo_btn = QtWidgets.QPushButton("元に戻す")
        self.redo_btn = QtWidgets.QPushButton("やり直す")
        self.reset_btn = QtWidgets.QPushButton("リセット")
        self.undo_btn.setShortcut(QtGui.QKeySequence.Undo)
        self.redo_btn.setShortcut(QtGui.QKeySequence.Redo)
        for b in (self.undo_btn, self.redo_btn, self.reset_btn):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)
        self.undo_btn.clicked.connect(self.undo)
        self.redo_btn.clicked.connect(self.redo)
        self.reset_btn.clicked.connect(self.reset)

        # --- 指標 ------------------------------------------------------
        self.metric_label = QtWidgets.QLabel("—")
        self.metric_label.setWordWrap(True)
        self.metric_label.setStyleSheet(
            "background:#1a1d24; border:1px solid #2a2d35; border-radius:4px;"
            "padding:5px; color:#c8cedb; font-size:11px;")
        layout.addWidget(self.metric_label)

        self.dice_btn = QtWidgets.QPushButton("Dice 係数を計算 (厳密・数秒)")
        self.dice_btn.clicked.connect(self.dice_requested)
        layout.addWidget(self.dice_btn)

        self.setToolTip("矢印キー = X/Y、PageUp/Down = Z、Shift 併用で 10 倍ステップ")

        layout.addStretch(1)
        self._sync_buttons()

    # ------------------------------------------------------------------
    def _header_row(self, text: str, step_widget) -> QtWidgets.QHBoxLayout:
        """見出しとステップ選択を同じ行にまとめて縦を節約する。"""
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
        row.addWidget(self._section(text))
        row.addStretch(1)
        step_widget.setFixedWidth(96)
        step_widget.setFixedHeight(24)
        row.addWidget(step_widget)
        return row

    @staticmethod
    def _section(text: str) -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet("color:#9aa3b2; font-weight:600; margin-top:4px;")
        return lbl

    # ------------------------------------------------------------------
    def current_step(self, key: str) -> float:
        if key.startswith("t"):
            return float(self.translate_step.currentData())
        return float(self.rotate_step.currentData())

    def _on_nudge(self, key: str, sign: float):
        self.undo_stack.push(self.transform.values())
        delta = sign * self.current_step(key)
        if key.startswith("t"):
            self.transform.translate(key[1], delta)
        else:
            self.transform.rotate(key[1], delta)
        self._refresh_values()
        self._emit()

    def _on_value_edited(self, _value):
        if self._updating:
            return
        self.undo_stack.push(self.transform.values())
        self.transform.set_values(**{k: r.value.value() for k, r in self.rows.items()})
        self._emit()

    def nudge_by_key(self, key: str, sign: float, big: bool = False):
        """キーボードショートカット用。"""
        self.undo_stack.push(self.transform.values())
        step = self.current_step(key) * (10.0 if big else 1.0)
        if key.startswith("t"):
            self.transform.translate(key[1], sign * step)
        else:
            self.transform.rotate(key[1], sign * step)
        self._refresh_values()
        self._emit()

    # ------------------------------------------------------------------
    def undo(self):
        state = self.undo_stack.undo(self.transform.values())
        if state is None:
            return
        self.transform.set_values(**state)
        self._refresh_values()
        self._emit()

    def redo(self):
        state = self.undo_stack.redo(self.transform.values())
        if state is None:
            return
        self.transform.set_values(**state)
        self._refresh_values()
        self._emit()

    def reset(self):
        self.undo_stack.push(self.transform.values())
        self.transform.reset()
        self._refresh_values()
        self._emit()

    def set_center(self, center):
        self.transform.center = tuple(float(v) for v in center)

    def set_base(self, base):
        self.transform.base = base

    def clear_history(self):
        self.undo_stack.clear()
        self._sync_buttons()

    # ------------------------------------------------------------------
    def _refresh_values(self):
        self._updating = True
        vals = self.transform.values()
        for k, r in self.rows.items():
            r.value.setValue(vals[k])
        self._updating = False

    def _emit(self):
        self._sync_buttons()
        self.transform_changed.emit()

    def _sync_buttons(self):
        self.undo_btn.setEnabled(self.undo_stack.can_undo)
        self.redo_btn.setEnabled(self.undo_stack.can_redo)

    def set_metric_text(self, text: str):
        self.metric_label.setText(text)

    def set_enabled_controls(self, enabled: bool):
        for r in self.rows.values():
            r.setEnabled(enabled)
        self.reset_btn.setEnabled(enabled)
        self.dice_btn.setEnabled(enabled)
        if enabled:
            self._sync_buttons()
        else:
            self.undo_btn.setEnabled(False)
            self.redo_btn.setEnabled(False)
