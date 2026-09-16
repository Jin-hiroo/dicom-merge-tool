"""ボリューム → サーフェスメッシュ。

vtkMarchingCubes ではなく vtkFlyingEdges3D を使う (同じ結果でより高速・低メモリ)。
プレビューと書出しでパイプラインを分け、プレビュー側は必ず三角形数を抑える。
"""
from __future__ import annotations

import vtk

from app import config
from app.core.volume import Volume


def attach_progress(vtk_filter, progress_cb, cancel_cb, label: str,
                    lo: int = 0, hi: int = 100):
    """VTK フィルタの進捗を Qt 側へ橋渡しし、キャンセル時に中断させる。"""
    if progress_cb is None and cancel_cb is None:
        return

    def _observer(caller, _event):
        if cancel_cb and cancel_cb():
            caller.SetAbortExecute(1)
            return
        if progress_cb:
            p = lo + (hi - lo) * float(caller.GetProgress())
            progress_cb(int(p), label)

    vtk_filter.AddObserver(vtk.vtkCommand.ProgressEvent, _observer)


def extract_surface(image, threshold: float,
                    target_triangles: int | None = None,
                    smooth_iterations: int = 0,
                    fill_holes: bool = False,
                    fast_decimate: bool = False,
                    progress_cb=None, cancel_cb=None):
    """vtkImageData から等値面 vtkPolyData を作る。

    fast_decimate=True なら vtkDecimatePro を使う。vtkQuadricDecimation より
    5 倍ほど速く、品質差はプレビューでは見えない。書出し時は品質を優先して
    QuadricDecimation を使う (実測: 699k -> 300k で 9.3s 対 1.7s)。
    """
    surface = vtk.vtkFlyingEdges3D()
    surface.SetInputData(image)
    surface.SetValue(0, float(threshold))
    surface.ComputeNormalsOff()      # 後段で計算するほうが安い
    surface.ComputeGradientsOff()
    surface.ComputeScalarsOff()
    attach_progress(surface, progress_cb, cancel_cb, "等値面を抽出中…", 0, 30)
    surface.Update()

    out = surface.GetOutput()
    if cancel_cb and cancel_cb():
        return out
    if out.GetNumberOfPoints() == 0:
        return out

    # 間引きを平滑化より *先* にやるのが重要。逆順だと数百万三角形を平滑化する
    # ことになり桁違いに遅い (実測: 先に平滑化 350s 対 先に間引き 20s)。
    if target_triangles and out.GetNumberOfPolys() > target_triangles:
        ratio = 1.0 - float(target_triangles) / float(out.GetNumberOfPolys())
        if fast_decimate:
            deci = vtk.vtkDecimatePro()
            deci.PreserveTopologyOff()
            deci.SplittingOff()
            deci.BoundaryVertexDeletionOn()
        else:
            deci = vtk.vtkQuadricDecimation()
        deci.SetInputData(out)
        deci.SetTargetReduction(min(max(ratio, 0.0), 0.98))
        attach_progress(deci, progress_cb, cancel_cb, "ポリゴンを削減中…", 30, 75)
        deci.Update()
        out = deci.GetOutput()
        if cancel_cb and cancel_cb():
            return out

    if smooth_iterations > 0:
        smoother = vtk.vtkWindowedSincPolyDataFilter()
        smoother.SetInputData(out)
        smoother.SetNumberOfIterations(int(smooth_iterations))
        smoother.BoundarySmoothingOff()
        smoother.FeatureEdgeSmoothingOff()
        smoother.SetPassBand(0.1)
        smoother.NonManifoldSmoothingOn()
        smoother.NormalizeCoordinatesOn()   # 収縮を防ぐ (Taubin)
        attach_progress(smoother, progress_cb, cancel_cb, "表面を平滑化中…", 75, 88)
        smoother.Update()
        out = smoother.GetOutput()
        if cancel_cb and cancel_cb():
            return out

    if fill_holes:
        filler = vtk.vtkFillHolesFilter()
        filler.SetInputData(out)
        filler.SetHoleSize(10.0)
        attach_progress(filler, progress_cb, cancel_cb, "穴を埋めています…", 88, 93)
        filler.Update()
        out = filler.GetOutput()

    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(out)
    normals.SetFeatureAngle(60.0)
    normals.ConsistencyOn()
    normals.SplittingOff()
    attach_progress(normals, progress_cb, cancel_cb, "法線を計算中…", 92, 100)
    normals.Update()
    return normals.GetOutput()


def preview_surface(volume: Volume, threshold: float,
                    progress_cb=None, cancel_cb=None):
    """プレビュー用: ダウンサンプル + 三角形数制限。

    Returns (polydata, binning_factor, downsampled_volume)
    ダウンサンプル済みボリュームは 2D 重ね合わせビューでも再利用する。
    """
    factor = volume.preview_binning()
    small = volume.downsample(factor)
    if progress_cb:
        progress_cb(5, f"プレビュー生成中 (1/{factor} に縮小)…")
    poly = extract_surface(
        small.to_vtk(), threshold,
        target_triangles=config.PREVIEW_TARGET_TRIANGLES,
        smooth_iterations=0,
        fast_decimate=True,
        progress_cb=progress_cb, cancel_cb=cancel_cb,
    )
    return poly, factor, small


def export_surface(volume: Volume, threshold: float,
                   target_triangles: int = config.EXPORT_TARGET_TRIANGLES,
                   smooth_iterations: int = config.EXPORT_SMOOTH_ITERATIONS,
                   fill_holes: bool = False,
                   high_quality: bool = False,
                   progress_cb=None, cancel_cb=None):
    """書出し用: フル解像度で 1 回だけメッシュ化する。

    high_quality=True は vtkQuadricDecimation を使い形状保持に優れるが、
    600 スライス級では 5 分以上かかる。既定は vtkDecimatePro (約 20 秒)。
    """
    return extract_surface(
        volume.to_vtk(), threshold,
        target_triangles=target_triangles,
        smooth_iterations=smooth_iterations,
        fill_holes=fill_holes,
        fast_decimate=not high_quality,
        progress_cb=progress_cb, cancel_cb=cancel_cb,
    )


def polydata_stats(poly) -> dict:
    b = poly.GetBounds()
    n_tri = int(poly.GetNumberOfPolys())
    return {
        "points": int(poly.GetNumberOfPoints()),
        "triangles": n_tri,
        "bounds_mm": (b[1] - b[0], b[3] - b[2], b[5] - b[4]),
        "stl_bytes": 84 + n_tri * 50,   # バイナリ STL の理論サイズ
    }
