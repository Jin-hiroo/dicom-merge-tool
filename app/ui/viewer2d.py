"""2D 直交スライス重ね合わせビュー。

自動レジストレーションを使わない方針なので、3D 表面の目視だけでは
mm 精度の追い込みができない。Fixed をグレースケール、Moving を赤で
重ねて表示することで、ズレを桁違いに正確に読めるようにする。
"""
from __future__ import annotations

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from app.core import resample

PLANES = [("Axial (横断)", 2), ("Coronal (冠状)", 1), ("Sagittal (矢状)", 0)]
RENDER_SIZE = 420


class SliceCanvas(QtWidgets.QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setMinimumHeight(220)
        self.setStyleSheet("background:#0e0f12; border:1px solid #2a2d35;")
        self.setText("シリーズを選択してください")
        self._pixmap: QtGui.QPixmap | None = None

    def set_image(self, image: QtGui.QImage | None):
        if image is None:
            self._pixmap = None
            self.setText("表示できるスライスがありません")
            return
        self._pixmap = QtGui.QPixmap.fromImage(image)
        self._rescale()

    def resizeEvent(self, event):       # noqa: N802
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._pixmap is None:
            return
        self.setPixmap(self._pixmap.scaled(self.size(), QtCore.Qt.KeepAspectRatio,
                                           QtCore.Qt.SmoothTransformation))


class Viewer2D(QtWidgets.QWidget):
    """Fixed(グレー) に Moving(赤) を重ねた直交断面。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fixed = None
        self._moving = None
        self._transform = None
        self._threshold = 250.0
        self._bounds = None
        self._overlay_alpha = 0.55
        self._window = (-200.0, 1200.0)     # 骨に合わせた表示ウィンドウ

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.tabs = QtWidgets.QTabWidget()
        self._canvases = {}
        self._sliders = {}
        for label, axis in PLANES:
            page = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(page)
            v.setContentsMargins(2, 2, 2, 2)
            canvas = SliceCanvas()
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(50)
            slider.valueChanged.connect(self.refresh)
            v.addWidget(canvas, 1)
            v.addWidget(slider)
            self._canvases[axis] = canvas
            self._sliders[axis] = slider
            self.tabs.addTab(page, label)
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        layout.addWidget(self.tabs, 1)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Moving の濃さ"))
        self.alpha_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.alpha_slider.setRange(0, 100)
        self.alpha_slider.setValue(55)
        self.alpha_slider.setFixedWidth(120)
        self.alpha_slider.valueChanged.connect(self._on_alpha)
        row.addWidget(self.alpha_slider)
        row.addStretch(1)
        self.hint = QtWidgets.QLabel("Fixed = グレー / Moving = 赤")
        self.hint.setStyleSheet("color:#8b93a3;")
        row.addWidget(self.hint)
        layout.addLayout(row)

    # ------------------------------------------------------------------
    def set_volumes(self, fixed, moving, threshold: float):
        self._fixed, self._moving = fixed, moving
        self._threshold = float(threshold)
        self._bounds = self._combined_bounds()
        self.refresh()

    def set_transform(self, matrix):
        self._transform = matrix
        self.refresh()

    def set_threshold(self, threshold: float):
        self._threshold = float(threshold)
        self.refresh()

    def clear(self):
        self._fixed = self._moving = self._bounds = None
        for canvas in self._canvases.values():
            canvas.set_image(None)

    def _on_alpha(self, value):
        self._overlay_alpha = value / 100.0
        self.refresh()

    def _combined_bounds(self):
        boxes = []
        if self._fixed is not None:
            boxes.append(self._fixed.world_bounds())
        if self._moving is not None:
            boxes.append(self._moving.world_bounds(self._transform))
        if not boxes:
            return None
        lo = np.min([b[0] for b in boxes], axis=0)
        hi = np.max([b[1] for b in boxes], axis=0)
        return np.stack([lo, hi])

    # ------------------------------------------------------------------
    def refresh(self):
        if self._fixed is None:
            return
        axis = PLANES[self.tabs.currentIndex()][1]
        bounds = self._combined_bounds()
        if bounds is None:
            return
        self._bounds = bounds

        frac = self._sliders[axis].value() / 100.0
        position = bounds[0][axis] + (bounds[1][axis] - bounds[0][axis]) * frac

        min_u, max_u, min_v, max_v = resample.plane_extent(bounds, axis)
        aspect = (max_v - min_v) / max(max_u - min_u, 1e-6)
        width = RENDER_SIZE
        height = max(16, min(int(RENDER_SIZE * aspect), 1024))

        try:
            f_vals, f_mask = resample.sample_plane(
                self._fixed, axis, position, bounds, width, height, None)
        except Exception:      # noqa: BLE001 - 描画は失敗しても致命的ではない
            return

        rgb = self._to_grayscale(f_vals, f_mask)

        if self._moving is not None:
            try:
                m_vals, m_mask = resample.sample_plane(
                    self._moving, axis, position, bounds, width, height,
                    self._transform)
                rgb = self._overlay_red(rgb, m_vals, m_mask)
            except Exception:  # noqa: BLE001
                pass

        self._canvases[axis].set_image(self._to_qimage(rgb))

    # ------------------------------------------------------------------
    def _to_grayscale(self, vals, mask) -> np.ndarray:
        lo, hi = self._window
        g = np.clip((vals.astype(np.float32) - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
        g = (g * 255).astype(np.uint8)
        g[~mask] = 0
        return np.dstack([g, g, g])

    def _overlay_red(self, rgb, vals, mask) -> np.ndarray:
        hit = mask & (vals >= self._threshold)
        if not np.any(hit):
            return rgb
        a = float(self._overlay_alpha)
        out = rgb.astype(np.float32)
        out[hit, 0] = out[hit, 0] * (1 - a) + 255.0 * a
        out[hit, 1] = out[hit, 1] * (1 - a)
        out[hit, 2] = out[hit, 2] * (1 - a)
        return np.clip(out, 0, 255).astype(np.uint8)

    @staticmethod
    def _to_qimage(rgb: np.ndarray) -> QtGui.QImage:
        # 上下反転して解剖学的な向き (上が頭側/前側) に合わせる
        rgb = np.ascontiguousarray(rgb[::-1])
        h, w, _ = rgb.shape
        img = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888)
        return img.copy()
