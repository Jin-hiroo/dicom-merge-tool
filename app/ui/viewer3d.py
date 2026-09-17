"""3D プレビュー。Qt ウィンドウに埋め込んだ VTK ビュー。

制約1 により、描画は必ずこのウィジェット内で行う (オフスクリーンにしない)。

位置合わせ中はメッシュを作り直さず、アクタの UserTransform だけを差し替える。
GPU 側の行列が変わるだけなので、移動ボタン連打にも即座に追従する。
"""
from __future__ import annotations

import vtk
from PyQt5 import QtWidgets
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor

from app import config

VIEW_DIRECTIONS = {
    "前 (A)": ((0, -1, 0), (0, 0, 1)),
    "後 (P)": ((0, 1, 0), (0, 0, 1)),
    "左 (L)": ((-1, 0, 0), (0, 0, 1)),
    "右 (R)": ((1, 0, 0), (0, 0, 1)),
    "上 (S)": ((0, 0, 1), (0, -1, 0)),
    "下 (I)": ((0, 0, -1), (0, 1, 0)),
}


class Viewer3D(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.interactor = QVTKRenderWindowInteractor(self)
        layout.addWidget(self.interactor)

        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(0.11, 0.12, 0.15)
        self.renderer.SetBackground2(0.17, 0.18, 0.22)
        self.renderer.GradientBackgroundOn()
        self.interactor.GetRenderWindow().AddRenderer(self.renderer)

        style = vtk.vtkInteractorStyleTrackballCamera()
        self.interactor.SetInteractorStyle(style)

        self._actors: dict[str, vtk.vtkActor] = {}
        self._clip_plane = vtk.vtkPlane()
        self._clip_enabled = False

        self._orientation = self._make_orientation_widget()

    # ------------------------------------------------------------------
    def _make_orientation_widget(self):
        axes = vtk.vtkAxesActor()
        axes.SetXAxisLabelText("X")
        axes.SetYAxisLabelText("Y")
        axes.SetZAxisLabelText("Z")
        widget = vtk.vtkOrientationMarkerWidget()
        widget.SetOrientationMarker(axes)
        widget.SetInteractor(self.interactor)
        widget.SetViewport(0.0, 0.0, 0.16, 0.22)
        return widget

    def initialize(self):
        """ウィンドウ表示後に呼ぶ (macOS では show() の後でないと失敗する)。"""
        self.interactor.Initialize()
        self.interactor.Start()
        try:
            self._orientation.EnabledOn()
            self._orientation.InteractiveOff()
        except Exception:      # noqa: BLE001 - 環境によっては使えない
            pass

    # ------------------------------------------------------------------
    def set_surface(self, role: str, polydata, color=None, opacity: float = 1.0):
        """role ("fixed"/"moving"/"merged") のサーフェスを差し替える。"""
        self.remove_surface(role)
        if polydata is None or polydata.GetNumberOfPoints() == 0:
            self.render()
            return

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)
        mapper.ScalarVisibilityOff()
        if self._clip_enabled:
            mapper.AddClippingPlane(self._clip_plane)

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*(color or config.COLOR_FIXED))
        prop.SetOpacity(float(opacity))
        prop.SetSpecular(0.25)
        prop.SetSpecularPower(25)
        prop.SetDiffuse(0.85)
        prop.SetAmbient(0.18)

        self.renderer.AddActor(actor)
        self._actors[role] = actor
        self.render()

    def remove_surface(self, role: str):
        actor = self._actors.pop(role, None)
        if actor is not None:
            self.renderer.RemoveActor(actor)

    def clear(self):
        for role in list(self._actors):
            self.remove_surface(role)
        self.render()

    def has(self, role: str) -> bool:
        return role in self._actors

    # ------------------------------------------------------------------
    def set_transform(self, role: str, vtk_transform):
        """★ 位置合わせの中核。メッシュ再構築なしで姿勢だけ更新する。"""
        actor = self._actors.get(role)
        if actor is None:
            return
        actor.SetUserTransform(vtk_transform)
        self.render()

    def set_visible(self, role: str, visible: bool):
        actor = self._actors.get(role)
        if actor is not None:
            actor.SetVisibility(bool(visible))
            self.render()

    def set_opacity(self, role: str, opacity: float):
        actor = self._actors.get(role)
        if actor is not None:
            actor.GetProperty().SetOpacity(float(opacity))
            self.render()

    def set_color(self, role: str, color):
        actor = self._actors.get(role)
        if actor is not None:
            actor.GetProperty().SetColor(*color)
            self.render()

    # ------------------------------------------------------------------
    def set_clipping(self, enabled: bool, axis: int = 2, position: float | None = None):
        self._clip_enabled = bool(enabled)
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        self._clip_plane.SetNormal(*normal)

        bounds = self.renderer.ComputeVisiblePropBounds()
        if position is None:
            position = 0.5
        lo, hi = bounds[axis * 2], bounds[axis * 2 + 1]
        origin = [0.0, 0.0, 0.0]
        origin[axis] = lo + (hi - lo) * float(position)
        self._clip_plane.SetOrigin(*origin)

        for actor in self._actors.values():
            mapper = actor.GetMapper()
            mapper.RemoveAllClippingPlanes()
            if self._clip_enabled:
                mapper.AddClippingPlane(self._clip_plane)
        self.render()

    # ------------------------------------------------------------------
    def reset_camera(self):
        self.renderer.ResetCamera()
        self.render()

    def focus_on(self, role: str, view: str | None = None) -> bool:
        """指定したサーフェスが画面に収まるようカメラを合わせる。

        別々に撮った CT は患者座標上で遠く離れていることがあり、単に表示
        しただけでは画面外で「何も出ない」ように見えるため。

        ``view`` を渡すとその方向から見る。CT は体軸 (Z) に長いので、
        既定の Z 方向のままだと筒を真上から覗く形になり何も分からない。
        """
        actor = self._actors.get(role)
        if actor is None:
            return False
        bounds = actor.GetBounds()
        if bounds[1] < bounds[0]:
            return False
        # 先に一度フィットして焦点と距離を確定させてから向きを変え、再フィットする
        self.renderer.ResetCamera(bounds)
        if view:
            self.set_view(view)
            self.renderer.ResetCamera(bounds)
        self.renderer.ResetCameraClippingRange()
        self.render()
        return True

    def set_view(self, name: str):
        spec = VIEW_DIRECTIONS.get(name)
        if spec is None:
            return
        direction, up = spec
        cam = self.renderer.GetActiveCamera()
        focal = cam.GetFocalPoint()
        dist = cam.GetDistance()
        cam.SetPosition(focal[0] + direction[0] * dist,
                        focal[1] + direction[1] * dist,
                        focal[2] + direction[2] * dist)
        cam.SetViewUp(*up)
        self.renderer.ResetCameraClippingRange()
        self.render()

    def save_camera(self) -> dict:
        """現在の視点を保存する。モードを往復しても視点を失わないため。"""
        cam = self.renderer.GetActiveCamera()
        return {
            "position": tuple(cam.GetPosition()),
            "focal_point": tuple(cam.GetFocalPoint()),
            "view_up": tuple(cam.GetViewUp()),
            "parallel_scale": float(cam.GetParallelScale()),
            "view_angle": float(cam.GetViewAngle()),
        }

    def restore_camera(self, state: dict | None) -> bool:
        if not state:
            return False
        cam = self.renderer.GetActiveCamera()
        cam.SetPosition(*state["position"])
        cam.SetFocalPoint(*state["focal_point"])
        cam.SetViewUp(*state["view_up"])
        cam.SetParallelScale(state.get("parallel_scale", 1.0))
        cam.SetViewAngle(state.get("view_angle", 30.0))
        self.renderer.ResetCameraClippingRange()
        self.render()
        return True

    def render(self):
        self.interactor.GetRenderWindow().Render()

    def close_viewer(self):
        """終了時に VTK の対話ループを畳む (残すとクラッシュすることがある)。"""
        try:
            self.interactor.GetRenderWindow().Finalize()
            self.interactor.TerminateApp()
        except Exception:      # noqa: BLE001
            pass
