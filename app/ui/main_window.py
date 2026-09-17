"""メインウィンドウ。全パネルの配線と処理フローの制御。

制約1 の実装箇所: 重い処理はすべて TaskRunner (QThread) に投げ、
メインスレッドでは描画と行列更新しかしない。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from app import config
from app.core import blend as blend_mod
from app.core import resample
from app.core.memory import human
from app.core.metrics import SurfaceMetric
from app.core.transform import initial_placement
from app.ui.align_panel import AlignPanel
from app.ui.merge_panel import MergePanel
from app.ui.progress_bar import ProgressBar
from app.ui.segment_panel import SegmentPanel
from app.ui.series_panel import (PREVIEW_KEY, ROLE_FIXED, ROLE_MOVING,
                                 SeriesEntry, SeriesPanel)
from app.ui.viewer2d import Viewer2D
from app.ui.viewer3d import VIEW_DIRECTIONS, Viewer3D
from app.workers.base import TaskRunner
from app.core import export_dicom, session
from app.workers.tasks import (DiceWorker, ExportDicomWorker, ExportWorker,
                               LoadSessionWorker, LoadWorker, MergeWorker,
                               MeshWorker, SaveSessionWorker, ScanWorker)

log = logging.getLogger(__name__)

DARK_QSS = """
QWidget { background:#15171c; color:#d6dbe5; font-size:12px; }
QGroupBox { border:1px solid #2a2d35; border-radius:6px; margin-top:10px; padding-top:8px; }
QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#9aa3b2; font-weight:600; }
QPushButton { background:#232730; border:1px solid #343945; border-radius:4px; padding:5px 8px; }
QPushButton:hover:enabled { background:#2b303b; }
QPushButton:disabled { color:#5b6272; background:#1b1e25; }
QListWidget, QComboBox, QSpinBox, QDoubleSpinBox { background:#1a1d24; border:1px solid #2a2d35; border-radius:4px; padding:3px; }
QListWidget::item:selected { background:#2f5d8a; }
QProgressBar { background:#1a1d24; border:1px solid #2a2d35; border-radius:7px; }
QProgressBar::chunk { background:#3d7dbd; border-radius:6px; }
QTabBar::tab { background:#1a1d24; padding:5px 12px; border:1px solid #2a2d35; }
QTabBar::tab:selected { background:#2b303b; }
QSlider::groove:horizontal { height:4px; background:#2a2d35; border-radius:2px; }
QSlider::handle:horizontal { background:#5b9bd5; width:13px; margin:-5px 0; border-radius:6px; }
"""


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(config.APP_NAME)
        self._size_to_screen()

        self.runner = TaskRunner(self)
        self.metric: SurfaceMetric | None = None
        self._pending_role: str | None = None
        # 表示キー -> {"poly","volume","preview_volume"}。
        # キーは ROLE_FIXED / ROLE_MOVING / PREVIEW_KEY。
        self._meshes = {}
        self._last_grid = None

        self._build_ui()
        self._build_menu()
        self._connect()
        self._install_shortcuts()
        self._restore_settings()
        self._update_actions()
        # 前回のセッションがあれば起動直後に復元を促す。
        # HEAD3DV1_NO_RESTORE=1 で抑止できる (自動テストや、まっさらに始めたいとき)。
        if os.environ.get("HEAD3DV1_NO_RESTORE") != "1":
            QtCore.QTimer.singleShot(400, self._offer_autosave_restore)

    # ==================================================================
    # メニュー
    # ==================================================================
    def _build_menu(self):
        bar = self.menuBar()
        m = bar.addMenu("ファイル")

        act_save = m.addAction("セッションを保存…")
        act_save.setShortcut(QtGui.QKeySequence.Save)
        act_save.triggered.connect(self.on_save_session)

        act_open = m.addAction("セッションを開く…")
        act_open.setShortcut(QtGui.QKeySequence.Open)
        act_open.triggered.connect(self.on_open_session)

        self.act_restore = m.addAction("前回のセッションを復元")
        self.act_restore.triggered.connect(
            lambda: self._start_restore(session.AUTOSAVE_DIR))

        m.addSeparator()
        act_clean = m.addAction("未使用の一時ファイルを削除")
        act_clean.triggered.connect(self.on_cleanup_scratch)

    def _size_to_screen(self):
        """画面に収まるウィンドウサイズにする。

        13 インチクラスの Retina では論理解像度が 1500x900 程度しかないため、
        固定サイズだと右側パネルが画面外にはみ出す。
        """
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is None:
            self.resize(1440, 860)
            return
        avail = screen.availableGeometry()
        self.resize(min(1680, int(avail.width() * 0.96)),
                    min(1040, int(avail.height() * 0.94)))
        self.move(avail.left() + 8, avail.top() + 8)

    # ==================================================================
    # UI 構築
    # ==================================================================
    def _build_ui(self):
        self.series_panel = SeriesPanel()
        self.segment_panel = SegmentPanel()
        self.align_panel = AlignPanel()
        self.merge_panel = MergePanel()
        self.viewer3d = Viewer3D()
        self.viewer2d = Viewer2D()
        self.progress = ProgressBar()

        # --- 中央: 3D の下に 2D 重ね合わせ -------------------------------
        center = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        top = QtWidgets.QWidget()
        tv = QtWidgets.QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(4)
        tv.addWidget(self._view_toolbar())
        tv.addWidget(self.viewer3d, 1)
        center.addWidget(top)
        center.addWidget(self.viewer2d)
        center.setStretchFactor(0, 3)
        center.setStretchFactor(1, 2)

        # --- 右: セグメント / 位置合わせ / 結合 ---------------------------
        # セグメントと位置合わせはスクロール可能に。結合/書出しはワークフローの
        # 終点なので、スクロールの外に固定して常に押せるようにしておく。
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        holder = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(holder)
        rv.setContentsMargins(4, 4, 4, 4)
        rv.addWidget(self.segment_panel)
        rv.addWidget(self.align_panel)
        rv.addStretch(1)
        scroll.setWidget(holder)

        right = QtWidgets.QWidget()
        rvv = QtWidgets.QVBoxLayout(right)
        rvv.setContentsMargins(0, 0, 0, 0)
        rvv.setSpacing(4)
        rvv.addWidget(scroll, 1)
        rvv.addWidget(self.merge_panel, 0)
        right.setMinimumWidth(370)
        right.setMaximumWidth(440)

        main = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        lv.addWidget(self.series_panel)
        left.setMinimumWidth(250)
        left.setMaximumWidth(330)
        main.addWidget(left)
        main.addWidget(center)
        main.addWidget(right)
        main.setStretchFactor(1, 1)

        root = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(main, 1)
        rl.addWidget(self._separator())
        rl.addWidget(self.progress)
        self.setCentralWidget(root)

        self.statusBar().showMessage(
            "DICOM フォルダを追加してください  —  " + config.DISCLAIMER.splitlines()[0])

    @staticmethod
    def _separator():
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color:#2a2d35;")
        return line

    def _view_toolbar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(bar)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)

        for name in VIEW_DIRECTIONS:
            b = QtWidgets.QPushButton(name)
            b.setFixedWidth(58)
            b.clicked.connect(lambda _=False, n=name: self.viewer3d.set_view(n))
            row.addWidget(b)

        reset = QtWidgets.QPushButton("全体表示")
        reset.clicked.connect(self.viewer3d.reset_camera)
        row.addWidget(reset)

        row.addSpacing(12)
        self.show_fixed = QtWidgets.QCheckBox("Fixed")
        self.show_moving = QtWidgets.QCheckBox("Moving")
        self.show_preview = QtWidgets.QCheckBox("プレビュー")
        for cb in (self.show_fixed, self.show_moving, self.show_preview):
            cb.setChecked(True)
        self.show_fixed.toggled.connect(
            lambda v: self.viewer3d.set_visible(ROLE_FIXED, v))
        self.show_moving.toggled.connect(
            lambda v: self.viewer3d.set_visible(ROLE_MOVING, v))
        self.show_preview.toggled.connect(
            lambda v: self.viewer3d.set_visible(PREVIEW_KEY, v))
        self.show_preview.setEnabled(False)
        row.addWidget(self.show_fixed)
        row.addSpacing(8)
        row.addWidget(self.show_moving)
        row.addSpacing(8)
        row.addWidget(self.show_preview)

        row.addSpacing(10)
        row.addWidget(QtWidgets.QLabel("透過"))
        self.moving_opacity = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.moving_opacity.setRange(10, 100)
        self.moving_opacity.setValue(int(config.OPACITY_MOVING * 100))
        self.moving_opacity.setFixedWidth(100)
        self.moving_opacity.valueChanged.connect(
            lambda v: self.viewer3d.set_opacity(ROLE_MOVING, v / 100.0))
        row.addWidget(self.moving_opacity)

        row.addSpacing(12)
        self.clip_check = QtWidgets.QCheckBox("断面")
        self.clip_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.clip_slider.setRange(0, 100)
        self.clip_slider.setValue(50)
        self.clip_slider.setFixedWidth(110)
        self.clip_slider.setEnabled(False)
        self.clip_axis = QtWidgets.QComboBox()
        self.clip_axis.addItems(["X", "Y", "Z"])
        self.clip_axis.setCurrentIndex(2)
        self.clip_axis.setFixedWidth(52)
        self.clip_axis.setEnabled(False)
        self.clip_check.toggled.connect(self._on_clip_toggle)
        self.clip_slider.valueChanged.connect(self._apply_clip)
        self.clip_axis.currentIndexChanged.connect(self._apply_clip)
        row.addWidget(self.clip_check)
        row.addWidget(self.clip_axis)
        row.addWidget(self.clip_slider)

        row.addStretch(1)
        return bar

    # ==================================================================
    # シグナル配線
    # ==================================================================
    def _connect(self):
        self.series_panel.add_folder_requested.connect(self.on_add_folder)
        self.series_panel.load_requested.connect(self.on_load_series)
        self.series_panel.roles_changed.connect(self.on_roles_changed)
        self.series_panel.remove_requested.connect(self.on_remove_entry)
        self.series_panel.preview_changed.connect(self.on_preview_changed)

        self.segment_panel.apply_requested.connect(self.rebuild_previews)
        self.segment_panel.threshold_preview.connect(self.on_threshold_preview)

        self.align_panel.transform_changed.connect(self.on_transform_changed)
        self.align_panel.dice_requested.connect(self.on_dice)
        self.align_panel.init_mode.currentIndexChanged.connect(self.on_init_mode)

        self.merge_panel.merge_requested.connect(self.on_merge)
        self.merge_panel.export_requested.connect(self.on_export)
        self.merge_panel.export_dicom_requested.connect(self.on_export_dicom)

        self.runner.progress.connect(self.progress.update_progress)
        self.runner.failed.connect(self.on_failed)
        self.runner.busy_changed.connect(self.on_busy_changed)
        self.progress.cancel_requested.connect(self.runner.cancel)

    def _install_shortcuts(self):
        specs = [
            (QtCore.Qt.Key_Left, "tx", -1.0), (QtCore.Qt.Key_Right, "tx", +1.0),
            (QtCore.Qt.Key_Down, "ty", -1.0), (QtCore.Qt.Key_Up, "ty", +1.0),
            (QtCore.Qt.Key_PageDown, "tz", -1.0), (QtCore.Qt.Key_PageUp, "tz", +1.0),
        ]
        for key, axis, sign in specs:
            for mod, big in ((QtCore.Qt.NoModifier, False),
                             (QtCore.Qt.ShiftModifier, True)):
                sc = QtWidgets.QShortcut(QtGui.QKeySequence(int(mod) | int(key)), self)
                sc.setContext(QtCore.Qt.ApplicationShortcut)
                sc.activated.connect(
                    lambda a=axis, s=sign, b=big: self._shortcut_nudge(a, s, b))

    def _shortcut_nudge(self, axis, sign, big):
        if self.runner.busy or ROLE_MOVING not in self._meshes:
            return
        self.align_panel.nudge_by_key(axis, sign, big)

    # ==================================================================
    # 設定の保存と復元
    # ==================================================================
    def _collect_settings(self) -> dict:
        a = self.align_panel
        return {
            "segment": {
                "preset": self.segment_panel.preset.currentText(),
                "threshold": self.segment_panel.threshold,
                "largest_component": self.segment_panel.use_largest_component,
                "island_enabled": self.segment_panel.island_enable.isChecked(),
                "island_size": int(self.segment_panel.island_size.value()),
            },
            "align": {
                **a.transform.values(),
                "center": list(a.transform.center),
                "base": np.asarray(a.transform.base, float).tolist(),
                "init_mode": a.init_mode.currentIndex(),
                "translate_step": a.translate_step.currentIndex(),
                "rotate_step": a.rotate_step.currentIndex(),
            },
            "merge": {
                "blend": self.merge_panel.blend.currentText(),
                "target_triangles": int(self.merge_panel.target_tris.value()),
                "smooth": int(self.merge_panel.smooth.value()),
                "fill_holes": self.merge_panel.fill_holes.isChecked(),
                "high_quality": self.merge_panel.high_quality.isChecked(),
                "dicom_description": self.merge_panel.dicom_description,
                "dicom_threshold": self.merge_panel.dicom_apply_threshold,
            },
            "last_folder": getattr(self, "_last_folder", ""),
        }

    def _apply_settings(self, d: dict, include_transform: bool = False):
        """設定を復元する。途中で再メッシュ化が走らないよう抑止しておく。

        ``include_transform`` は位置合わせの 6 自由度まで戻すかどうか。
        位置合わせは「特定の 2 シリーズに対する」値なので、セッション復元の
        ときだけ戻す。起動時の環境設定として無条件に適用すると、別のデータを
        読み込んだときに古い変換が黙って効いてしまう。
        """
        if not d:
            return
        self._suspend_rebuild = True
        try:
            seg = d.get("segment", {})
            if seg:
                if seg.get("preset"):
                    self.segment_panel.preset.setCurrentText(seg["preset"])
                if seg.get("threshold") is not None:
                    self.segment_panel.set_threshold(int(seg["threshold"]))
                self.segment_panel.largest_cc.setChecked(
                    bool(seg.get("largest_component")))
                self.segment_panel.island_enable.setChecked(
                    bool(seg.get("island_enabled")))
                if seg.get("island_size"):
                    self.segment_panel.island_size.setValue(int(seg["island_size"]))

            al = d.get("align", {})
            if al:
                a = self.align_panel
                # ステップ幅などの「好み」は常に戻す
                for widget, key in ((a.init_mode, "init_mode"),
                                    (a.translate_step, "translate_step"),
                                    (a.rotate_step, "rotate_step")):
                    idx = al.get(key)
                    if idx is not None and 0 <= int(idx) < widget.count():
                        widget.setCurrentIndex(int(idx))
                # 変換そのものはセッション復元時のみ
                if include_transform:
                    a.transform.set_values(**{k: al[k] for k in
                                              ("tx", "ty", "tz", "rx", "ry", "rz")
                                              if k in al})
                    if al.get("center"):
                        a.set_center(al["center"])
                    if al.get("base"):
                        a.set_base(np.array(al["base"], float))
                    a.clear_history()
                a._refresh_values()

            mg = d.get("merge", {})
            if mg:
                if mg.get("blend"):
                    self.merge_panel.blend.setCurrentText(mg["blend"])
                if mg.get("target_triangles"):
                    self.merge_panel.target_tris.setValue(int(mg["target_triangles"]))
                if mg.get("smooth") is not None:
                    self.merge_panel.smooth.setValue(int(mg["smooth"]))
                self.merge_panel.fill_holes.setChecked(bool(mg.get("fill_holes")))
                self.merge_panel.high_quality.setChecked(bool(mg.get("high_quality")))
                self.merge_panel.series_description.setText(
                    mg.get("dicom_description", "") or "")
                self.merge_panel.dicom_threshold.setChecked(
                    bool(mg.get("dicom_threshold")))

            self._last_folder = d.get("last_folder", "") or ""
        finally:
            self._suspend_rebuild = False

    def _qsettings(self):
        """設定の保存先。

        HEAD3DV1_SETTINGS_SCOPE で切り替えられる。自動テストがユーザーの
        実際の環境設定 (閾値やブレンド方式) を上書きしないようにするため。
        """
        scope = os.environ.get("HEAD3DV1_SETTINGS_SCOPE") or "head3Dv1"
        return QtCore.QSettings("head3Dv1", scope)

    def _restore_settings(self):
        """ウィンドウ位置と各パネルの設定を前回終了時の状態に戻す。"""
        self._suspend_rebuild = False
        self._last_folder = ""
        st = self._qsettings()
        geo = st.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        raw = st.value("panels/settings")
        if raw:
            try:
                self._apply_settings(json.loads(raw))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                log.warning("設定の復元に失敗しました: %s", exc)

    def _save_settings(self):
        st = self._qsettings()
        st.setValue("window/geometry", self.saveGeometry())
        try:
            st.setValue("panels/settings",
                        json.dumps(self._collect_settings(), ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            log.warning("設定の保存に失敗しました: %s", exc)

    # ==================================================================
    # セッション
    # ==================================================================
    def on_save_session(self):
        default = str(session.SESSIONS_DIR /
                      datetime.datetime.now().strftime("session_%Y%m%d_%H%M"))
        session.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        folder = QtWidgets.QFileDialog.getSaveFileName(
            self, "セッションの保存先", default)[0]
        if not folder:
            return
        entries = self.series_panel.entries
        if not entries:
            QtWidgets.QMessageBox.information(
                self, "保存するものがありません", "先にシリーズを読み込んでください。")
            return
        n_vol = sum(1 for e in entries if e.merged and e.volume is not None)
        self.statusBar().showMessage(
            f"セッションを保存中… (結合結果 {n_vol} 件の実体をコピーします)")
        self._start(SaveSessionWorker(folder, list(entries),
                                      self._collect_settings(), copy_volumes=True),
                    self._on_session_saved, "セッションを保存中…")

    def _on_session_saved(self, info):
        if not info:
            return
        QtWidgets.QMessageBox.information(
            self, "セッションを保存しました",
            f"{info['folder']}\n\n"
            f"シリーズ : {info['entries']} 件 (うち結合結果 {info['volumes']} 件)\n"
            f"サイズ   : {human(info['bytes'])}\n\n"
            f"元の DICOM シリーズはパス参照です。元フォルダを移動・削除すると\n"
            f"復元できなくなります。")
        self.statusBar().showMessage(f"セッションを保存しました: {info['folder']}")

    def on_open_session(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "セッションフォルダを選択", str(session.SESSIONS_DIR))
        if not folder:
            return
        if not session.is_session(folder):
            QtWidgets.QMessageBox.warning(
                self, "セッションではありません",
                f"{folder} に {session.MANIFEST} が見つかりません。")
            return
        self._start_restore(folder)

    def _offer_autosave_restore(self):
        info = session.peek(session.AUTOSAVE_DIR)
        self.act_restore.setEnabled(info is not None)
        if info is None or not info["entries"]:
            return
        titles = "\n".join(f"・{t}" for t in info["titles"][:6])
        more = f"\n… 他 {info['entries'] - 6} 件" if info["entries"] > 6 else ""
        if QtWidgets.QMessageBox.question(
                self, "前回のセッションを復元しますか？",
                f"前回終了時 ({info['saved_at']}) の作業内容が残っています。\n\n"
                f"{titles}{more}\n\n復元しますか？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        ) == QtWidgets.QMessageBox.Yes:
            self._start_restore(session.AUTOSAVE_DIR)

    def _start_restore(self, folder):
        if not session.is_session(folder):
            QtWidgets.QMessageBox.information(
                self, "セッションがありません",
                "復元できるセッションが見つかりませんでした。")
            return
        self._start(LoadSessionWorker(folder, config.SCRATCH_DIR),
                    self._on_session_loaded, "セッションを復元中…")

    def _on_session_loaded(self, data):
        if not data:
            return
        # セッション復元では位置合わせまで戻す (作業状態そのものなので)
        self._apply_settings(data.get("settings", {}), include_transform=True)

        self.series_panel.entries.clear()
        self.viewer3d.clear()
        self._meshes.clear()
        # 復元後のシリーズは previewed=False なので、表示状態も揃えておく
        self.show_preview.setEnabled(False)

        entries = []
        for rec in data["entries"]:
            entry = SeriesEntry(
                title=rec["title"], role=rec.get("role", ""),
                merged=bool(rec.get("merged")), meta=rec.get("meta", {}),
                volume=rec.get("volume"), info=rec.get("info"),
                dicom_ref=rec.get("dicom"))
            entries.append(entry)
        self.series_panel.add_entries(entries)

        if data.get("problems"):
            QtWidgets.QMessageBox.warning(
                self, "一部を復元できませんでした",
                "次の項目は復元できませんでした:\n\n" +
                "\n".join(f"・{p}" for p in data["problems"]))

        n = len(entries)
        self.statusBar().showMessage(
            f"セッションを復元しました: {n} 件 "
            f"({data.get('saved_at', '')})")
        self.on_roles_changed()

    def on_cleanup_scratch(self):
        used = len(session.scratch_files_in_use(self.series_panel.entries))
        if QtWidgets.QMessageBox.question(
                self, "一時ファイルの削除",
                f"現在使用中でない scratch の一時ファイルを削除します。\n"
                f"(使用中の {used} 件は残します)\n\n続けますか？",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        ) != QtWidgets.QMessageBox.Ok:
            return
        n = session.cleanup_unused_scratch(self.series_panel.entries)
        self.statusBar().showMessage(f"一時ファイルを {n} 件削除しました")

    # ==================================================================
    # シリーズ操作
    # ==================================================================
    def on_add_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "DICOM フォルダを選択", getattr(self, "_last_folder", "") or "")
        if not folder:
            return
        self._last_folder = folder
        self._start(ScanWorker(folder), self._on_scan_done, "DICOM を走査中…")

    def _on_scan_done(self, result):
        if not result:
            QtWidgets.QMessageBox.information(
                self, "シリーズなし",
                "このフォルダから CT シリーズを検出できませんでした。")
            return
        entries = [
            SeriesEntry(title=i.label(), info=i,
                        dicom_ref={"folder": str(i.folder), "series_uid": i.uid})
            for i in result]
        self.series_panel.add_entries(entries)

        warns = [i for i in result if not i.spacing_consistent and i.spacing_warning]
        if warns:
            QtWidgets.QMessageBox.warning(
                self, "スライス間隔の警告",
                "\n\n".join(f"・{w.description or w.uid[-8:]}\n  {w.spacing_warning}"
                            for w in warns))
        self.statusBar().showMessage(f"{len(result)} 件のシリーズを検出しました")

    def on_load_series(self, index: int):
        entry = self.series_panel.entry(index)
        if entry is None or entry.loaded or entry.info is None:
            return
        if entry.info.nbytes > config.MAX_VOLUME_BYTES:
            ok = QtWidgets.QMessageBox.question(
                self, "メモリ警告",
                f"このシリーズは約 {human(entry.info.nbytes)} あり、"
                f"上限 {human(config.MAX_VOLUME_BYTES)} を超えます。\n"
                "続行すると動作が重くなる可能性があります。読み込みますか？")
            if ok != QtWidgets.QMessageBox.Yes:
                return
        self._loading_index = index
        self._start(LoadWorker(entry.info, config.SCRATCH_DIR),
                    self._on_load_done, "シリーズを読込中…")

    def _on_load_done(self, volume):
        if volume is None:
            return
        entry = self.series_panel.entry(getattr(self, "_loading_index", -1))
        if entry is None:
            return
        entry.volume = volume
        entry.title = f"{volume.name}"
        self.series_panel.refresh()
        self.statusBar().showMessage(
            f"読込完了: {volume.name} — {human(volume.nbytes)}")
        if self.series_panel.by_role(ROLE_FIXED) is None:
            entry.role = ROLE_FIXED
            self.series_panel.refresh()
            self.on_roles_changed()

    def on_remove_entry(self, index: int):
        entry = self.series_panel.entry(index)
        if entry is None:
            return
        role, was_previewed = entry.role, entry.previewed
        self.series_panel.remove_entry(index)
        if was_previewed:
            self._meshes.pop(PREVIEW_KEY, None)
            self.viewer3d.remove_surface(PREVIEW_KEY)
            self.show_preview.setEnabled(False)
        if role:
            self._meshes.pop(role, None)
            self.viewer3d.remove_surface(role)
        if role or was_previewed:
            self.on_roles_changed()

    # ==================================================================
    # プレビュー生成
    # ==================================================================
    def on_roles_changed(self):
        for role in (ROLE_FIXED, ROLE_MOVING):
            if self.series_panel.by_role(role) is None:
                self._meshes.pop(role, None)
                self.viewer3d.remove_surface(role)
        self.rebuild_previews()

    def on_preview_changed(self):
        """単体プレビューの対象が変わったとき。

        Fixed / Moving のメッシュは作り直さず、プレビュー分だけ更新する。
        位置合わせ中に押しても作業が巻き戻らないようにするため。
        """
        entry = self.series_panel.previewed_entry()
        if entry is None:
            self._meshes.pop(PREVIEW_KEY, None)
            self.viewer3d.remove_surface(PREVIEW_KEY)
            self.show_preview.setEnabled(False)
            self.statusBar().showMessage("プレビューを閉じました")
            self._update_actions()
            return

        self.show_preview.setEnabled(True)
        if not self.show_preview.isChecked():
            self.show_preview.setChecked(True)
        self._focus_preview_when_ready = True
        self._mesh_queue = [PREVIEW_KEY]
        self._next_mesh()

    def on_threshold_preview(self, value: int):
        self.viewer2d.set_threshold(value)
        self.statusBar().showMessage(
            f"HU 閾値 {value} — [再セグメント] で 3D プレビューに反映されます")

    def _entry_for(self, key: str):
        """表示キーに対応するシリーズを返す。"""
        if key == PREVIEW_KEY:
            return self.series_panel.previewed_entry()
        return self.series_panel.by_role(key)

    @staticmethod
    def _label_for(key: str) -> str:
        return {ROLE_FIXED: "Fixed", ROLE_MOVING: "Moving",
                PREVIEW_KEY: "プレビュー"}.get(key, key)

    def rebuild_previews(self):
        if self.runner.busy or getattr(self, "_suspend_rebuild", False):
            return
        keys = [k for k in (ROLE_FIXED, ROLE_MOVING, PREVIEW_KEY)
                if self._entry_for(k) is not None]
        # 表示対象から外れたものは畳んでおく
        for k in (ROLE_FIXED, ROLE_MOVING, PREVIEW_KEY):
            if k not in keys:
                self._meshes.pop(k, None)
                self.viewer3d.remove_surface(k)
        if not keys:
            self.viewer3d.clear()
            self.viewer2d.clear()
            self._update_actions()
            return
        self._mesh_queue = list(keys)
        self._next_mesh()

    def _next_mesh(self):
        if not getattr(self, "_mesh_queue", None):
            self._after_previews()
            return
        key = self._mesh_queue.pop(0)
        entry = self._entry_for(key)
        if entry is None or not entry.loaded:
            self._next_mesh()
            return
        self._pending_role = key
        worker = MeshWorker(
            key, entry.volume, self.segment_panel.threshold,
            largest_component=self.segment_panel.use_largest_component,
            min_island_voxels=self.segment_panel.min_island_voxels,
            scratch_dir=config.SCRATCH_DIR)
        self._start(worker, self._on_mesh_done,
                    f"{self._label_for(key)} のプレビューを生成中…")

    def _on_mesh_done(self, result):
        if result is None:
            self._mesh_queue = []
            return
        key = result["role"]
        self._meshes[key] = result

        color, opacity = {
            ROLE_FIXED: (config.COLOR_FIXED, config.OPACITY_FIXED),
            ROLE_MOVING: (config.COLOR_MOVING,
                          self.moving_opacity.value() / 100.0),
            PREVIEW_KEY: (config.COLOR_PREVIEW, config.OPACITY_PREVIEW),
        }.get(key, (config.COLOR_MERGED, 1.0))
        self.viewer3d.set_surface(key, result["poly"], color, opacity)
        if key == PREVIEW_KEY:
            self.viewer3d.set_visible(PREVIEW_KEY, self.show_preview.isChecked())

        n = result["poly"].GetNumberOfPolys()
        self.statusBar().showMessage(
            f"{self._label_for(key)}: {n:,} 三角形 "
            f"(1/{result['factor']} に縮小)")
        self._next_mesh()

    def _after_previews(self):
        # 単体プレビューを点けた直後は、そこへカメラを寄せて確実に見えるようにする
        # (別々に撮った CT は患者座標上で遠く離れていることがある)
        if getattr(self, "_focus_preview_when_ready", False):
            self._focus_preview_when_ready = False
            # CT は体軸に長いので、正面 (A) から見ないと形が読めない
            if self.viewer3d.focus_on(PREVIEW_KEY, view="前 (A)"):
                entry = self.series_panel.previewed_entry()
                self.statusBar().showMessage(
                    f"プレビュー表示: {entry.title if entry else ''}"
                    "  — [全体表示] で元の視点に戻せます")
            self.on_transform_changed()
            self._update_actions()
            return

        fixed = self._meshes.get(ROLE_FIXED)
        moving = self._meshes.get(ROLE_MOVING)

        if moving is not None:
            self._set_rotation_center(moving["poly"])
            self.on_init_mode()

        if fixed is not None and moving is not None:
            self.metric = SurfaceMetric(fixed["poly"], moving["poly"])
        else:
            self.metric = None

        self.viewer2d.set_volumes(
            fixed["preview_volume"] if fixed else None,
            moving["preview_volume"] if moving else None,
            self.segment_panel.threshold)

        self.viewer3d.reset_camera()
        self.on_transform_changed()
        self._update_actions()
        self._refresh_grid_label()

    def _set_rotation_center(self, poly):
        from vtkmodules.util import numpy_support
        if poly.GetNumberOfPoints() == 0:
            return
        pts = numpy_support.vtk_to_numpy(poly.GetPoints().GetData())
        self.align_panel.set_center(pts.mean(axis=0))

    # ==================================================================
    # 位置合わせ
    # ==================================================================
    def on_init_mode(self):
        fixed = self.series_panel.by_role(ROLE_FIXED)
        moving = self.series_panel.by_role(ROLE_MOVING)
        if fixed is None or moving is None:
            return
        mode = ["dicom", "centroid", "stack_z"][self.align_panel.init_mode.currentIndex()]
        self.align_panel.set_base(initial_placement(fixed.volume, moving.volume, mode))
        self.on_transform_changed()

    def on_transform_changed(self):
        """★ ここが軽いことが肝心。メッシュは作り直さず行列だけ差し替える。"""
        matrix = self.align_panel.transform.matrix()
        self.viewer3d.set_transform(ROLE_MOVING,
                                    self.align_panel.transform.to_vtk_transform())
        self.viewer2d.set_transform(matrix)

        self.align_panel.set_metric_text(self.align_panel.transform.describe())
        if self.metric is not None and self.metric.ready:
            result = self.metric.evaluate(matrix)
            self.progress.set_metric(SurfaceMetric.format(result))
        else:
            self.progress.set_metric("")
        self._refresh_grid_label()

    def _refresh_grid_label(self):
        fixed = self.series_panel.by_role(ROLE_FIXED)
        moving = self.series_panel.by_role(ROLE_MOVING)
        if fixed is None or moving is None or not (fixed.loaded and moving.loaded):
            self.merge_panel.set_grid_text("出力グリッド: —")
            return
        try:
            grid = resample.compute_output_grid(
                fixed.volume, moving.volume, self.align_panel.transform.matrix())
        except Exception:      # noqa: BLE001
            return
        self._last_grid = grid
        text = "出力グリッド: " + resample.describe_grid(grid)
        if grid["adjusted"]:
            text += "\n⚠ " + grid["note"]
        self.merge_panel.set_grid_text(text)

    def on_dice(self):
        fixed = self.series_panel.by_role(ROLE_FIXED)
        moving = self.series_panel.by_role(ROLE_MOVING)
        if fixed is None or moving is None:
            return
        self._start(DiceWorker(fixed.volume, moving.volume,
                               self.segment_panel.threshold,
                               self.align_panel.transform.matrix()),
                    self._on_dice_done, "Dice 係数を計算中…")

    def _on_dice_done(self, result):
        if not result:
            return
        self.progress.set_metric(self.progress.metric.text() + "   " + result["note"])
        self.statusBar().showMessage(result["note"])

    # ==================================================================
    # 結合 / 書出し
    # ==================================================================
    def on_merge(self):
        fixed = self.series_panel.by_role(ROLE_FIXED)
        moving = self.series_panel.by_role(ROLE_MOVING)
        if fixed is None or moving is None:
            return

        grid = self._last_grid or resample.compute_output_grid(
            fixed.volume, moving.volume, self.align_panel.transform.matrix())
        msg = (f"次の設定で結合します。\n\n"
               f"Fixed : {fixed.volume.name}\n"
               f"Moving: {moving.volume.name}\n"
               f"{self.align_panel.transform.describe()}\n\n"
               f"出力: {resample.describe_grid(grid)}")
        if grid["adjusted"]:
            msg += f"\n\n⚠ {grid['note']}"
        if QtWidgets.QMessageBox.question(
                self, "結合の確認", msg,
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        ) != QtWidgets.QMessageBox.Ok:
            return

        mode = blend_mod.normalize_mode(self.merge_panel.blend_mode_label)
        self._start(MergeWorker(fixed.volume, moving.volume,
                                self.align_panel.transform.matrix(), mode,
                                scratch_dir=config.SCRATCH_DIR,
                                name=self._next_merge_name(fixed, moving)),
                    self._on_merge_done, "ボリュームを融合中…")

    def _next_merge_name(self, fixed, moving) -> str:
        """結合を重ねても名前が伸び続けないようにする。"""
        n = sum(1 for e in self.series_panel.entries if e.merged) + 1
        sources = int(fixed.meta.get("source_count", 1) if fixed.merged else 1) + 1
        return f"結合結果 #{n} ({sources} シリーズ)"

    def _on_merge_done(self, result):
        if result is None:
            return
        volume, info = result["volume"], result["info"]

        fixed_before = self.series_panel.by_role(ROLE_FIXED)
        source_count = 2
        if fixed_before is not None and fixed_before.merged:
            source_count = int(fixed_before.meta.get("source_count", 1)) + 1
        info["source_count"] = source_count
        for e in self.series_panel.entries:
            e.role = ""
        entry = SeriesEntry(title=volume.name, volume=volume, merged=True,
                            role=ROLE_FIXED, meta=info)
        self.series_panel.add_entries([entry])

        self._meshes.pop(ROLE_MOVING, None)
        self.viewer3d.remove_surface(ROLE_MOVING)
        self.align_panel.transform.reset()
        self.align_panel.clear_history()

        self.statusBar().showMessage(
            f"結合完了: {volume.name} — 継ぎ目 {info['seam_axis']} 軸 / "
            f"重複 {info['overlap_mm']:.1f} mm")
        self.rebuild_previews()

    def on_export(self):
        entry = self.series_panel.by_role(ROLE_FIXED)
        if entry is None or not entry.loaded:
            QtWidgets.QMessageBox.information(
                self, "書き出し対象がありません",
                "書き出すシリーズを Fixed に設定してください。")
            return

        default = str(Path.home() / f"{entry.volume.name.replace(' ', '_')}.stl")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "STL の保存先", default, "STL ファイル (*.stl)")
        if not path:
            return

        if QtWidgets.QMessageBox.question(
                self, "書き出しの確認",
                f"{entry.volume.name} をフル解像度でメッシュ化して書き出します。\n\n"
                f"HU 閾値      : {self.segment_panel.threshold}\n"
                f"目標三角形数 : {self.merge_panel.target_tris.value():,}\n"
                f"平滑化       : {self.merge_panel.smooth.value()} 回\n\n"
                f"{config.DISCLAIMER}",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        ) != QtWidgets.QMessageBox.Ok:
            return

        self._start(ExportWorker(entry.volume, path, self.segment_panel.threshold,
                                 self.merge_panel.target_tris.value(),
                                 self.merge_panel.smooth.value(),
                                 self.merge_panel.fill_holes.isChecked(),
                                 self.merge_panel.high_quality.isChecked()),
                    self._on_export_done, "STL を書き出し中…")

    def on_export_dicom(self):
        entry = self.series_panel.by_role(ROLE_FIXED)
        if entry is None or not entry.loaded:
            QtWidgets.QMessageBox.information(
                self, "書き出し対象がありません",
                "書き出すシリーズを Fixed に設定してください。")
            return

        parent = QtWidgets.QFileDialog.getExistingDirectory(
            self, "DICOM シリーズの保存先フォルダ", str(Path.home()))
        if not parent:
            return

        volume = entry.volume
        description = self.merge_panel.dicom_description or None
        threshold = (self.segment_panel.threshold
                     if self.merge_panel.dicom_apply_threshold else None)
        plan = export_dicom.plan_export(volume, parent, description)

        note = ("\n\n⚠ HU 閾値 %d 未満を空気に置き換えます。"
                % self.segment_panel.threshold) if threshold is not None else ""
        if QtWidgets.QMessageBox.question(
                self, "DICOM 書き出しの確認",
                f"{volume.name} を CT DICOM シリーズとして書き出します。\n\n"
                f"スライス数   : {plan['slices']:,} 枚 "
                f"({plan['columns']} x {plan['rows']})\n"
                f"推定サイズ   : {human(plan['total_bytes'])}\n"
                f"シリーズ説明 : {plan['series_description']}\n"
                f"保存先       : {parent}\n\n"
                f"元データではなく派生データ (DERIVED / SECONDARY) として、\n"
                f"新しい SeriesInstanceUID で書き出します。"
                f"{note}\n\n{config.DISCLAIMER}",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        ) != QtWidgets.QMessageBox.Ok:
            return

        # 既存ファイルに混ざらないよう専用のサブフォルダを作る
        folder = export_dicom.unique_dir(
            parent, export_dicom.dicom_folder_name(volume))

        self._start(ExportDicomWorker(volume, folder, description, threshold),
                    self._on_export_dicom_done, "DICOM を書き出し中…")

    def _on_export_dicom_done(self, info):
        if not info:
            return
        QtWidgets.QMessageBox.information(
            self, "DICOM 書き出し完了",
            f"{info['folder']}\n\n"
            f"ファイル数   : {info['files']:,} 枚\n"
            f"サイズ       : {human(info['total_bytes'])}\n"
            f"シリーズ説明 : {info['series_description']}\n"
            f"SeriesInstanceUID:\n{info['series_uid']}")
        self.statusBar().showMessage(
            f"DICOM 書き出し完了: {info['files']:,} 枚 → {info['folder']}")

    def _on_export_done(self, stats):
        if not stats:
            return
        b = stats["bounds_mm"]
        QtWidgets.QMessageBox.information(
            self, "書き出し完了",
            f"{stats['path']}\n\n"
            f"三角形数 : {stats['triangles']:,}\n"
            f"頂点数   : {stats['points']:,}\n"
            f"サイズ   : {b[0]:.1f} x {b[1]:.1f} x {b[2]:.1f} mm\n"
            f"ファイル : {human(stats['file_bytes'])}")
        self.statusBar().showMessage(f"書き出し完了: {stats['path']}")

    # ==================================================================
    # 共通
    # ==================================================================
    def _start(self, worker, on_done, message: str):
        if self.runner.busy:
            QtWidgets.QMessageBox.information(
                self, "処理中", "別の処理を実行中です。完了までお待ちください。")
            return
        try:
            self.runner.done.disconnect()
        except TypeError:
            pass
        self.runner.done.connect(on_done)
        self.progress.set_message(message)
        self.runner.start(worker)

    def on_busy_changed(self, busy: bool):
        self.progress.set_busy(busy)
        for w in (self.series_panel, self.segment_panel, self.merge_panel):
            w.setEnabled(not busy)
        self.align_panel.set_enabled_controls(not busy)
        if not busy:
            self.progress.set_message("準備完了")
            self._update_actions()

    def on_failed(self, message: str):
        log.error(message)
        QtWidgets.QMessageBox.critical(self, "エラー", message)
        self.progress.set_message("エラーが発生しました")

    def _update_actions(self):
        fixed = self.series_panel.by_role(ROLE_FIXED)
        moving = self.series_panel.by_role(ROLE_MOVING)
        busy = self.runner.busy

        has_both = fixed is not None and moving is not None
        self.merge_panel.set_merge_enabled(
            has_both and not busy,
            self._why_disabled(fixed, moving, busy, need_moving=True))

        can_export = fixed is not None and fixed.loaded and not busy
        self.merge_panel.set_export_enabled(
            can_export, self._why_disabled(fixed, moving, busy, need_moving=False))
        self.align_panel.set_enabled_controls(
            self.series_panel.by_role(ROLE_MOVING) is not None and not self.runner.busy)

    @staticmethod
    def _why_disabled(fixed, moving, busy, need_moving: bool) -> str:
        """ボタンが押せない理由を日本語で返す (無いと「ボタンが無い」と誤解される)。"""
        if busy:
            return "処理中です。完了までお待ちください。"
        if fixed is None:
            return "シリーズを読み込み、[Fixed に設定] を押すと使えます。"
        if not fixed.loaded:
            return "Fixed のシリーズがまだ読み込まれていません。"
        if need_moving and moving is None:
            return "もう 1 つのシリーズを [Moving に設定] してください。"
        return ""

    def _on_clip_toggle(self, enabled: bool):
        self.clip_slider.setEnabled(enabled)
        self.clip_axis.setEnabled(enabled)
        self._apply_clip()

    def _apply_clip(self):
        self.viewer3d.set_clipping(self.clip_check.isChecked(),
                                   axis=self.clip_axis.currentIndex(),
                                   position=self.clip_slider.value() / 100.0)

    # ------------------------------------------------------------------
    def closeEvent(self, event):        # noqa: N802
        self.runner.shutdown()
        self.viewer3d.close_viewer()
        self._save_settings()
        self._autosave_session()
        # ここで scratch を全消しすると次回の復元ができなくなるので、
        # セッションが参照していないファイルだけを掃除する。
        n = session.cleanup_unused_scratch(self.series_panel.entries)
        log.info("未使用の一時ファイルを %d 件削除しました", n)
        super().closeEvent(event)

    def _autosave_session(self):
        """終了時の自動保存。

        実体はコピーせず scratch を参照するだけなので一瞬で終わる。
        次回起動時にここから復元を促す。
        """
        entries = list(self.series_panel.entries)
        if not entries:
            return
        try:
            session.AUTOSAVE_DIR.mkdir(parents=True, exist_ok=True)
            session.save_session(session.AUTOSAVE_DIR, entries,
                                 self._collect_settings(), copy_volumes=False)
            log.info("セッションを自動保存しました: %s", session.AUTOSAVE_DIR)
        except Exception as exc:                        # noqa: BLE001
            log.warning("セッションの自動保存に失敗しました: %s", exc)
