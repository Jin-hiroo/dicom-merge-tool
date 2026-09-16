"""メモリ予算ガードの検証 (制約2)。"""
import numpy as np

from app.core import memory


def test_volume_bytes():
    assert memory.volume_bytes((10, 20, 30), 2) == 12000


def test_binning_for_budget():
    # 512x512x600 の int16 = 約 300MB を 64MB に収める
    assert memory.binning_for_budget((600, 512, 512), 2, 64 * 1024**2) >= 2
    # 既に予算内なら 1 のまま
    assert memory.binning_for_budget((10, 10, 10), 2, 64 * 1024**2) == 1


def test_fit_spacing_leaves_small_grid_alone():
    bounds = np.array([[0.0, 0.0, 0.0], [100.0, 100.0, 100.0]])
    spacing, dims, adjusted, scale = memory.fit_spacing_to_budget(
        bounds, (1.0, 1.0, 1.0), budget_bytes=2 * 1024**3)
    assert not adjusted and scale == 1.0
    assert dims == (101, 101, 101)


def test_fit_spacing_coarsens_when_over_budget():
    """予算超過ならスワップさせず自動的に粗くすること。"""
    bounds = np.array([[0.0, 0.0, 0.0], [500.0, 500.0, 2000.0]])
    fine = (0.2, 0.2, 0.2)
    spacing, dims, adjusted, scale = memory.fit_spacing_to_budget(
        bounds, fine, budget_bytes=256 * 1024**2)
    assert adjusted and scale > 1.0
    assert all(s > f for s, f in zip(spacing, fine))
    assert memory.volume_bytes(dims[::-1], 2) * 1.6 <= 256 * 1024**2


def test_check_total():
    class V:
        nbytes = 1024**3
    ok, total = memory.check_total([V(), V()], budget=3 * 1024**3)
    assert ok and total == 2 * 1024**3
    ok, _ = memory.check_total([V(), V(), V(), V()], budget=3 * 1024**3)
    assert not ok


def test_human():
    assert memory.human(1024) == "1.0 KB"
    assert memory.human(1024**3) == "1.0 GB"


def test_preview_binning_keeps_volume_small(volumes):
    va, _ = volumes
    factor = va.preview_binning(target_bytes=64 * 1024)
    small = va.downsample(factor)
    assert small.nbytes <= va.nbytes
    # ダウンサンプル後もワールド範囲がほぼ保たれること
    assert np.allclose(small.world_bounds(), va.world_bounds(), atol=max(small.spacing))
