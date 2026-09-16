"""Phase 0 スモークテスト: Qt ウィンドウ内で VTK が実際に描画できるか。

制約1「レンダリングは必ず GUI ウィンドウ内で行う」の前提確認。
--capture PATH を渡すと描画結果を PNG に保存して自動終了するので、
CI や自動確認から実行できる。引数なしなら普通に表示してマウス操作できる。
"""
import argparse
import sys

import vtk
from PyQt5 import QtCore, QtWidgets
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor


def build_window():
    win = QtWidgets.QMainWindow()
    win.setWindowTitle("head3Dv1 smoke test — Qt + VTK")
    win.resize(640, 480)

    frame = QtWidgets.QFrame()
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    vtk_widget = QVTKRenderWindowInteractor(frame)
    layout.addWidget(vtk_widget)
    win.setCentralWidget(frame)

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(0.12, 0.13, 0.16)
    vtk_widget.GetRenderWindow().AddRenderer(renderer)

    # 目視でも自動でも判定しやすいよう、色の付いた球と立方体を置く
    for src, pos, color in (
        (vtk.vtkSphereSource(), (-0.8, 0, 0), (0.30, 0.78, 0.90)),
        (vtk.vtkCubeSource(), (0.8, 0, 0), (0.92, 0.88, 0.78)),
    ):
        if isinstance(src, vtk.vtkSphereSource):
            src.SetThetaResolution(32)
            src.SetPhiResolution(32)
        src.Update()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(src.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetPosition(*pos)
        actor.GetProperty().SetColor(*color)
        renderer.AddActor(actor)

    renderer.ResetCamera()
    return win, vtk_widget, renderer


def capture(vtk_widget, path):
    rw = vtk_widget.GetRenderWindow()
    rw.Render()
    grabber = vtk.vtkWindowToImageFilter()
    grabber.SetInput(rw)
    grabber.ReadFrontBufferOff()
    grabber.Update()
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(path))
    writer.SetInputConnection(grabber.GetOutputPort())
    writer.Write()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", help="描画結果を PNG に保存して終了")
    args = ap.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    win, vtk_widget, renderer = build_window()
    win.show()
    vtk_widget.Initialize()
    vtk_widget.Start()

    rw = vtk_widget.GetRenderWindow()
    print("Qt platform :", app.platformName())
    print("VTK version :", vtk.VTK_VERSION)

    if args.capture:
        def done():
            print("OpenGL       :", rw.ReportCapabilities().splitlines()[0]
                  if rw.ReportCapabilities() else "(unknown)")
            capture(vtk_widget, args.capture)
            print("captured ->", args.capture)
            app.quit()
        QtCore.QTimer.singleShot(1200, done)

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
