"""シリーズ一覧パネル。読み込んだシリーズと結合済み結果をまとめて扱う。"""
from __future__ import annotations

from dataclasses import dataclass, field

from PyQt5 import QtCore, QtWidgets

from app.core.memory import human

ROLE_NONE, ROLE_FIXED, ROLE_MOVING = "", "fixed", "moving"
# 役割とは別枠。Fixed/Moving を割り当てずに単体で 3D 確認するための表示。
PREVIEW_KEY = "preview"


@dataclass
class SeriesEntry:
    """一覧の 1 行。DICOM 由来でも結合結果でも同じ形で扱う。"""
    title: str
    info: object = None          # SeriesInfo (DICOM 由来のみ)
    volume: object = None        # Volume (読込済みなら)
    merged: bool = False
    role: str = ROLE_NONE
    previewed: bool = False      # 単体プレビュー中か (同時に 1 件だけ)
    meta: dict = field(default_factory=dict)
    # セッション復元用: 元 DICOM の在り処 ({"folder":…, "series_uid":…})。
    # 元シリーズは数 GB になりうるのでセッションには実体を持たず、ここから読み直す。
    dicom_ref: dict | None = None

    @property
    def loaded(self) -> bool:
        return self.volume is not None

    def detail(self) -> str:
        if self.volume is not None:
            v = self.volume
            nx, ny, nz = v.shape_xyz
            sx, sy, sz = v.spacing
            return (f"{nx} x {ny} x {nz} ボクセル\n"
                    f"{sx:.3f} x {sy:.3f} x {sz:.3f} mm\n"
                    f"メモリ {human(v.nbytes)}")
        if self.info is not None:
            i = self.info
            return (f"{i.cols} x {i.rows} x {i.n_slices} スライス\n"
                    f"{i.pixel_spacing[1]:.3f} x {i.pixel_spacing[0]:.3f} x "
                    f"{i.slice_spacing:.3f} mm\n"
                    f"推定 {human(i.nbytes)}  (未読込)")
        return ""


class SeriesPanel(QtWidgets.QGroupBox):
    add_folder_requested = QtCore.pyqtSignal()
    load_requested = QtCore.pyqtSignal(int)         # entry index
    roles_changed = QtCore.pyqtSignal()
    remove_requested = QtCore.pyqtSignal(int)
    preview_changed = QtCore.pyqtSignal()           # 単体プレビューの対象が変わった

    def __init__(self, parent=None):
        super().__init__("シリーズ", parent)
        self.entries: list[SeriesEntry] = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(6)

        self.add_btn = QtWidgets.QPushButton("DICOM フォルダを追加…")
        self.add_btn.clicked.connect(self.add_folder_requested)
        layout.addWidget(self.add_btn)

        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        # 結合を重ねると名前が伸びるので、横スクロールバーを出さず省略表示にする
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(QtCore.Qt.ElideMiddle)
        self.list.setWordWrap(False)
        self.list.currentRowChanged.connect(self._on_selection)
        layout.addWidget(self.list, 1)

        self.detail = QtWidgets.QLabel("—")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(
            "background:#1a1d24; border:1px solid #2a2d35; border-radius:4px;"
            "padding:6px; color:#c8cedb; font-size:11px;")
        self.detail.setMinimumHeight(74)
        layout.addWidget(self.detail)

        self.load_btn = QtWidgets.QPushButton("読み込む")
        self.load_btn.clicked.connect(
            lambda: self.load_requested.emit(self.list.currentRow()))
        layout.addWidget(self.load_btn)

        self.preview_btn = QtWidgets.QPushButton("プレビュー表示")
        self.preview_btn.setToolTip(
            "選択中のシリーズを、役割を割り当てずに単体で 3D 表示する。\n"
            "位置合わせ中の Fixed / Moving はそのまま維持される。")
        self.preview_btn.clicked.connect(self._toggle_preview)
        layout.addWidget(self.preview_btn)

        role_row = QtWidgets.QHBoxLayout()
        self.fixed_btn = QtWidgets.QPushButton("Fixed に設定")
        self.moving_btn = QtWidgets.QPushButton("Moving に設定")
        self.fixed_btn.clicked.connect(lambda: self._assign(ROLE_FIXED))
        self.moving_btn.clicked.connect(lambda: self._assign(ROLE_MOVING))
        role_row.addWidget(self.fixed_btn)
        role_row.addWidget(self.moving_btn)
        layout.addLayout(role_row)

        self.clear_role_btn = QtWidgets.QPushButton("役割を解除")
        self.clear_role_btn.clicked.connect(self._clear_role)
        layout.addWidget(self.clear_role_btn)

        self.remove_btn = QtWidgets.QPushButton("一覧から削除")
        self.remove_btn.clicked.connect(
            lambda: self.remove_requested.emit(self.list.currentRow()))
        layout.addWidget(self.remove_btn)

        self._refresh_buttons()

    # ------------------------------------------------------------------
    def add_entries(self, entries):
        for e in entries:
            self.entries.append(e)
        self.refresh()
        if self.entries:
            self.list.setCurrentRow(len(self.entries) - 1)

    def remove_entry(self, index: int):
        if 0 <= index < len(self.entries):
            self.entries.pop(index)
            self.refresh()

    def entry(self, index: int) -> SeriesEntry | None:
        if 0 <= index < len(self.entries):
            return self.entries[index]
        return None

    def current_entry(self) -> SeriesEntry | None:
        return self.entry(self.list.currentRow())

    def by_role(self, role: str) -> SeriesEntry | None:
        for e in self.entries:
            if e.role == role:
                return e
        return None

    def refresh(self):
        row = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for e in self.entries:
            tag = {ROLE_FIXED: "  [Fixed]", ROLE_MOVING: "  [Moving]"}.get(e.role, "")
            if e.previewed:
                tag += "  [表示中]"
            mark = "●" if e.loaded else "○"
            prefix = "⧉ " if e.merged else ""
            item = QtWidgets.QListWidgetItem(f"{mark} {prefix}{e.title}{tag}")
            item.setToolTip(f"{e.title}\n{e.detail()}")
            self.list.addItem(item)
        self.list.blockSignals(False)
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        elif self.list.count():
            self.list.setCurrentRow(0)
        self._on_selection(self.list.currentRow())

    # ------------------------------------------------------------------
    def _assign(self, role: str):
        entry = self.current_entry()
        if entry is None or not entry.loaded:
            return
        if entry.role == role:
            return          # 冪等: 既にその役割ならなにもしない (解除は専用ボタン)
        for e in self.entries:
            if e is not entry and e.role == role:
                e.role = ROLE_NONE
        entry.role = role
        self.refresh()
        self.roles_changed.emit()

    def _toggle_preview(self):
        """選択中のシリーズの単体プレビューを入/切する。

        同時に見えるのは 1 件だけ。3D ビューが何色もの重なりで
        読めなくなるのを避けるため。
        """
        entry = self.current_entry()
        if entry is None or not entry.loaded:
            return
        turning_on = not entry.previewed
        for e in self.entries:
            e.previewed = False
        entry.previewed = turning_on
        self.refresh()
        self.preview_changed.emit()

    def previewed_entry(self):
        for e in self.entries:
            if e.previewed:
                return e
        return None

    def clear_preview(self):
        changed = False
        for e in self.entries:
            if e.previewed:
                e.previewed = False
                changed = True
        if changed:
            self.refresh()
        return changed

    def _clear_role(self):
        entry = self.current_entry()
        if entry is None or not entry.role:
            return
        entry.role = ROLE_NONE
        self.refresh()
        self.roles_changed.emit()

    def _on_selection(self, row: int):
        entry = self.entry(row)
        self.detail.setText(entry.detail() if entry else "—")
        self._refresh_buttons()

    def _refresh_buttons(self):
        entry = self.current_entry()
        has = entry is not None
        loaded = bool(entry and entry.loaded)
        self.load_btn.setEnabled(has and not loaded and entry.info is not None)
        self.fixed_btn.setEnabled(loaded)
        self.moving_btn.setEnabled(loaded)
        self.remove_btn.setEnabled(has)
        self.clear_role_btn.setEnabled(bool(entry and entry.role))
        self.preview_btn.setEnabled(loaded)
        self.preview_btn.setText(
            "プレビューを閉じる" if (entry and entry.previewed) else "プレビュー表示")
        if loaded:
            self.load_btn.setText("読み込み済み")
        else:
            self.load_btn.setText("読み込む")
