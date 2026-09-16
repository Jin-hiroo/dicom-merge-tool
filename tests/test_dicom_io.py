"""DICOM 読込の検証: ジオメトリと HU が生成時の値と一致するか。"""
import numpy as np

from app.core import dicom_io


def test_scan_finds_one_series_per_folder(phantom):
    a = dicom_io.scan_folder(phantom["folder_a"])
    b = dicom_io.scan_folder(phantom["folder_b"])
    assert len(a) == 1 and len(b) == 1
    assert a[0].n_slices == 61 and b[0].n_slices == 61
    assert a[0].modality == "CT"
    assert a[0].spacing_consistent


def test_spacing_order_is_correct(volumes):
    """PixelSpacing=[行(y), 列(x)] を x,y に取り違えていないこと。"""
    va, _ = volumes
    assert np.allclose(va.spacing, (1.0, 1.0, 2.0))


def test_hu_rescale_applied(volumes):
    """RescaleSlope/Intercept が適用され HU になっていること。"""
    va, _ = volumes
    assert int(va.array.min()) == -1000       # 空気
    assert int(va.array.max()) == 900         # 骨
    assert np.any(va.array == 40)             # 軟部組織


def test_origin_and_bounds(volumes, phantom):
    va, vb = volumes
    assert np.allclose(va.origin[2], 0.0)
    # B は仕込んだ offset ぶんずれて記録されている
    assert np.allclose(vb.origin, np.array(va.origin) +
                       np.array([phantom["offset"][0], phantom["offset"][1],
                                 80.0 + phantom["offset"][2]]))
    assert np.allclose(va.world_bounds()[1][2], 120.0)


def test_slices_sorted_by_position_not_instance_number(volumes):
    """z が単調増加していること (ソートが効いている)。"""
    va, _ = volumes
    assert va.spacing[2] > 0
    mid = va.array[va.shape_zyx[0] // 2]
    assert mid.max() == 900        # 中央スライスに骨がある
