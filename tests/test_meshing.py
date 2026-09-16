"""メッシュ化・指標・STL 書出しの検証。"""
import numpy as np

from app.core import meshing, metrics
from app.core.export_stl import write_stl
from app.core.transform import RigidTransform


def test_preview_surface_respects_triangle_budget(volumes):
    va, _ = volumes
    poly, factor, small = meshing.preview_surface(va, 250)
    assert poly.GetNumberOfPolys() > 0
    assert factor >= 1
    assert small.nbytes <= va.nbytes


def test_surface_bounds_match_volume(volumes):
    va, _ = volumes
    poly, _, _ = meshing.preview_surface(va, 250)
    b = poly.GetBounds()
    vb = va.world_bounds()
    # 骨は円柱 (半径 40mm) なので、ボリューム全体より内側に収まる
    assert b[0] >= vb[0][0] - 2 and b[1] <= vb[1][0] + 2
    assert b[4] >= vb[0][2] - 2 and b[5] <= vb[1][2] + 2


def test_export_surface_smooths_and_decimates(volumes):
    va, _ = volumes
    poly = meshing.export_surface(va, 250, target_triangles=5000,
                                  smooth_iterations=10)
    assert 0 < poly.GetNumberOfPolys() <= 7000


def test_write_stl_roundtrip(volumes, tmp_path):
    import vtk
    va, _ = volumes
    poly, _, _ = meshing.preview_surface(va, 250)
    path = tmp_path / "out.stl"
    stats = write_stl(poly, path)
    assert path.exists() and stats["file_bytes"] > 84

    reader = vtk.vtkSTLReader()
    reader.SetFileName(str(path))
    reader.Update()
    assert reader.GetOutput().GetNumberOfPolys() == stats["triangles"]


def test_surface_metric_improves_toward_truth(volumes, phantom):
    """★ 手動位置合わせの指標が実際に機能するか。

    正解の変換で誤差が最小になり、ズラすと悪化すること。
    """
    va, vb = volumes
    fixed_poly, _, _ = meshing.preview_surface(va, 250)
    moving_poly, _, _ = meshing.preview_surface(vb, 250)
    metric = metrics.SurfaceMetric(fixed_poly, moving_poly)
    assert metric.ready

    c = phantom["correction"]
    good = metric.evaluate(RigidTransform(tx=c[0], ty=c[1], tz=c[2]).matrix())
    off = metric.evaluate(RigidTransform(tx=c[0], ty=c[1], tz=c[2] + 8.0).matrix())
    nothing = metric.evaluate(None)

    assert good["n_overlap"] > 0
    assert good["mean_mm"] < off["mean_mm"]
    assert good["mean_mm"] < nothing["mean_mm"]
    assert good["mean_mm"] < 2.0


def test_dice_score_peaks_at_truth(volumes, phantom):
    va, vb = volumes
    c = phantom["correction"]
    good = metrics.dice_score(va, vb, 250,
                              RigidTransform(tx=c[0], ty=c[1], tz=c[2]).matrix())
    off = metrics.dice_score(va, vb, 250,
                             RigidTransform(tx=c[0], ty=c[1], tz=c[2] + 8).matrix())
    assert good["dice"] > 0.95
    assert good["dice"] > off["dice"]


def test_preview_is_fast_path(volumes):
    """プレビューは高速デシメータを使い、予算内なら間引かないこと。"""
    import time
    va, _ = volumes
    t0 = time.time()
    poly, _, _ = meshing.preview_surface(va, 250)
    assert time.time() - t0 < 5.0, "プレビューが遅すぎる"
    assert poly.GetNumberOfPolys() > 0


def test_export_decimates_before_smoothing(volumes):
    """間引きを平滑化より先に行うこと (逆順だと桁違いに遅い)。

    目標三角形数を厳しくしても結果が目標付近に収まることで確認する。
    """
    va, _ = volumes
    poly = meshing.export_surface(va, 250, target_triangles=3000,
                                  smooth_iterations=10)
    n = poly.GetNumberOfPolys()
    assert 0 < n <= 6000, f"間引きが効いていない: {n}"


def test_export_high_quality_option(volumes):
    va, _ = volumes
    poly = meshing.export_surface(va, 250, target_triangles=3000,
                                  smooth_iterations=0, high_quality=True)
    assert 0 < poly.GetNumberOfPolys() <= 6000
