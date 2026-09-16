"""リサンプル・ブレンド・融合の検証。

正解の変換を与えたとき、融合結果が解析的な正解オブジェクトと一致するかを見る。
"""
import numpy as np
import pytest

from app.core import blend, resample
from app.core.transform import RigidTransform
from tests.make_phantom import sample_volume


def correction(phantom):
    c = phantom["correction"]
    return RigidTransform(tx=c[0], ty=c[1], tz=c[2]).matrix()


def test_output_grid_covers_union(volumes, phantom):
    va, vb = volumes
    grid = resample.compute_output_grid(va, vb, correction(phantom))
    b = grid["bounds"]
    assert np.isclose(b[0][2], 0.0)       # 正解オブジェクトの z 範囲全体
    assert np.isclose(b[1][2], 200.0)
    assert not grid["adjusted"]           # ファントムは予算内に収まる


def test_output_grid_uses_finer_spacing(volumes, phantom):
    va, vb = volumes
    grid = resample.compute_output_grid(va, vb, correction(phantom))
    assert np.allclose(grid["spacing"],
                       np.minimum(va.spacing, vb.spacing))


def test_seam_axis_and_overlap_detected(volumes, phantom):
    va, vb = volumes
    grid = resample.compute_output_grid(va, vb, correction(phantom))
    axis, lo, hi, a_first = blend.seam_axis(grid["fixed_bounds"],
                                            grid["moving_bounds"])
    assert axis == 2                              # 撮影方向 (z) が継ぎ目
    assert np.isclose(hi - lo, phantom["overlap_mm"])
    assert a_first                                # A が低い z 側


@pytest.mark.parametrize("mode", [blend.MODE_FEATHER, blend.MODE_MAX,
                                  blend.MODE_MEAN])
def test_merge_matches_ground_truth(volumes, phantom, scratch, mode):
    """正解変換を与えれば、融合結果は解析的な正解と一致するはず。"""
    va, vb = volumes
    merged, info = resample.merge_volumes(va, vb, correction(phantom),
                                          mode=mode, scratch_dir=scratch)
    assert merged is not None
    truth = sample_volume(merged.origin, merged.spacing, merged.shape_xyz)

    diff = np.abs(merged.array.astype(np.float32) - truth.astype(np.float32))
    assert diff.mean() < 1.0

    bone_m, bone_t = merged.array >= 250, truth >= 250
    dice = (2 * np.count_nonzero(bone_m & bone_t) /
            (np.count_nonzero(bone_m) + np.count_nonzero(bone_t)))
    assert dice > 0.99


def test_misalignment_degrades_result(volumes, phantom, scratch):
    """指標が意味を持つこと: ズラせば一致度が下がる。"""
    va, vb = volumes
    c = phantom["correction"]
    bad = RigidTransform(tx=c[0], ty=c[1], tz=c[2] + 6.0).matrix()
    merged, _ = resample.merge_volumes(va, vb, bad, mode=blend.MODE_FEATHER,
                                       scratch_dir=scratch)
    truth = sample_volume(merged.origin, merged.spacing, merged.shape_xyz)
    bone_m, bone_t = merged.array >= 250, truth >= 250
    dice = (2 * np.count_nonzero(bone_m & bone_t) /
            (np.count_nonzero(bone_m) + np.count_nonzero(bone_t)))
    assert dice < 0.99


def test_non_overlap_regions_preserved(volumes, phantom, scratch):
    """片方にしか無い領域はその値がそのまま残ること。"""
    va, vb = volumes
    merged, _ = resample.merge_volumes(va, vb, correction(phantom),
                                       mode=blend.MODE_FEATHER,
                                       scratch_dir=scratch)
    # A だけが持つ z<80 の領域に骨があること
    z_idx = int((40.0 - merged.origin[2]) / merged.spacing[2])
    assert merged.array[z_idx].max() >= 250
    # B だけが持つ z>120 の領域にも骨があること
    z_idx = int((160.0 - merged.origin[2]) / merged.spacing[2])
    assert merged.array[z_idx].max() >= 250


def test_cancel_returns_none(volumes, phantom, scratch):
    va, vb = volumes
    merged, _ = resample.merge_volumes(va, vb, correction(phantom),
                                       scratch_dir=scratch,
                                       cancel_cb=lambda: True)
    assert merged is None


def test_blend_slab_modes():
    a = np.array([[[100, 200]]], dtype=np.int16)
    b = np.array([[[300, 0]]], dtype=np.int16)
    both = np.ones((1, 1, 2), dtype=bool)
    assert blend.blend_slab(a, both, b, both, blend.MODE_MAX).tolist() == [[[300, 200]]]
    assert blend.blend_slab(a, both, b, both, blend.MODE_MEAN).tolist() == [[[200, 100]]]

    # 片方だけ有効な領域は、その値がそのまま出る
    only_a = np.array([[[True, False]]])
    only_b = np.array([[[False, True]]])
    out = blend.blend_slab(a, only_a, b, only_b, blend.MODE_MAX)
    assert out.tolist() == [[[100, 0]]]


def test_feather_weights_ramp():
    w = blend.feather_weights(np.array([0.0, 5.0, 10.0]), 0.0, 10.0, a_first=True)
    assert np.allclose(w, [0.0, 0.5, 1.0])
    w = blend.feather_weights(np.array([0.0, 5.0, 10.0]), 0.0, 10.0, a_first=False)
    assert np.allclose(w, [1.0, 0.5, 0.0])
