"""DICOM 書き出しの検証。

中核はラウンドトリップ試験: 書き出した DICOM を読み戻したとき、
ジオメトリ (spacing / origin / direction) と HU が完全に一致すること。
読み書きが互いの逆変換になっていることをこれで担保する。
"""
import re

import numpy as np
import pydicom
import pytest

from app.core import blend, export_dicom, resample
from app.core.transform import RigidTransform


@pytest.fixture(scope="module")
def merged(volumes, phantom, scratch):
    c = phantom["correction"]
    m = RigidTransform(tx=c[0], ty=c[1], tz=c[2]).matrix()
    vol, _ = resample.merge_volumes(volumes[0], volumes[1], m,
                                    mode=blend.MODE_FEATHER, scratch_dir=scratch)
    return vol


@pytest.fixture(scope="module")
def exported(merged, tmp_path_factory):
    out = tmp_path_factory.mktemp("dicom_out") / "series"
    info = export_dicom.export_volume_to_dicom(merged, out)
    return info, out


# ----------------------------------------------------------------------
def test_writes_one_file_per_slice(merged, exported):
    info, out = exported
    assert info["files"] == merged.shape_zyx[0]
    assert len(list(out.glob("*.dcm"))) == merged.shape_zyx[0]


def test_roundtrip_geometry_is_exact(merged, exported, scratch):
    """★ 書き出し → 読み戻しでジオメトリが一致すること。"""
    _, out = exported
    back = export_dicom.read_back_volume(out, scratch)

    assert back.shape_zyx == merged.shape_zyx
    assert np.allclose(back.spacing, merged.spacing, atol=1e-6)
    assert np.allclose(back.origin, merged.origin, atol=1e-6)
    assert np.allclose(back.direction, merged.direction, atol=1e-6)
    assert np.allclose(back.world_bounds(), merged.world_bounds(), atol=1e-6)


def test_roundtrip_hu_is_lossless(merged, exported, scratch):
    """★ HU 値が 1 ボクセルも変わらないこと。"""
    _, out = exported
    back = export_dicom.read_back_volume(out, scratch)
    assert np.array_equal(np.asarray(back.array), np.asarray(merged.array))


def test_marked_as_derived(exported):
    """元データそのものと取り違えられないよう DERIVED であること。"""
    _, out = exported
    ds = pydicom.dcmread(str(sorted(out.glob("*.dcm"))[0]))
    assert "DERIVED" in list(ds.ImageType)
    assert "SECONDARY" in list(ds.ImageType)
    assert ds.DerivationDescription
    assert ds.Modality == "CT"


def test_patient_and_study_are_inherited(merged, exported):
    """患者・検査の同一性が保たれること (別患者に見えては困る)。"""
    info, out = exported
    ds = pydicom.dcmread(str(sorted(out.glob("*.dcm"))[0]))
    assert str(ds.PatientID) == "PHANTOM001"
    assert "PHANTOM" in str(ds.PatientName)
    # 同じ検査にぶら下がり、シリーズは新規採番されている
    assert ds.StudyInstanceUID == merged.meta["study_uid"]
    assert ds.SeriesInstanceUID == info["series_uid"]
    assert ds.SeriesInstanceUID != merged.meta.get("series_uid", "")


def test_new_frame_of_reference(exported):
    """ジオメトリが変わっているので FrameOfReference は新規であること。"""
    info, out = exported
    ds = pydicom.dcmread(str(sorted(out.glob("*.dcm"))[0]))
    assert ds.FrameOfReferenceUID == info["frame_of_reference"]


def test_slice_positions_advance_along_normal(merged, exported):
    _, out = exported
    files = sorted(out.glob("*.dcm"))
    z = [float(pydicom.dcmread(str(f)).ImagePositionPatient[2]) for f in files]
    assert np.allclose(np.diff(z), merged.spacing[2], atol=1e-6)
    assert np.isclose(z[0], merged.origin[2], atol=1e-6)


def test_pixel_spacing_row_column_order(merged, exported):
    """PixelSpacing=[行(y), 列(x)] を取り違えていないこと。"""
    _, out = exported
    ds = pydicom.dcmread(str(sorted(out.glob("*.dcm"))[0]))
    assert np.isclose(float(ds.PixelSpacing[0]), merged.spacing[1])
    assert np.isclose(float(ds.PixelSpacing[1]), merged.spacing[0])


def test_stored_as_unsigned_with_intercept(exported):
    _, out = exported
    ds = pydicom.dcmread(str(sorted(out.glob("*.dcm"))[0]))
    assert ds.PixelRepresentation == 0
    assert float(ds.RescaleIntercept) == -1024.0
    assert float(ds.RescaleSlope) == 1.0
    assert ds.pixel_array.min() >= 0


def test_threshold_option_zeroes_soft_tissue(merged, tmp_path, scratch):
    """閾値を指定すると、それ未満が空気に置き換わること。"""
    out = tmp_path / "thresholded"
    export_dicom.export_volume_to_dicom(merged, out, threshold=250)
    back = export_dicom.read_back_volume(out, scratch)
    arr = np.asarray(back.array)
    # 骨は残り、軟部組織 (40 HU) は消えている
    assert arr.max() >= 250
    assert not np.any((arr > -1000) & (arr < 250))


def test_cancel_removes_partial_files(merged, tmp_path):
    """中断したら書きかけのシリーズを残さないこと。"""
    out = tmp_path / "cancelled"
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3

    result = export_dicom.export_volume_to_dicom(merged, out, cancel_cb=cancel)
    assert result is None
    assert list(out.glob("*.dcm")) == []


def test_unique_dir_avoids_overwrite(tmp_path):
    a = export_dicom.unique_dir(tmp_path, "series")
    b = export_dicom.unique_dir(tmp_path, "series")
    assert a != b and a.exists() and b.exists()


def test_sanitize_name_falls_back_on_japanese():
    """日本語名から数字だけ残った "1_3" のような名前を作らないこと。"""
    assert export_dicom.sanitize_name("結合結果 #1 (3 シリーズ)") == "MERGED"
    assert export_dicom.sanitize_name("結合結果") == "MERGED"
    assert export_dicom.sanitize_name("CT_head-01") == "CT_head-01"


def test_dicom_folder_name_is_descriptive(merged, volumes):
    """フォルダ名だけで中身が分かること。"""
    name = export_dicom.dicom_folder_name(merged)
    assert name.startswith("MERGED_CT_2series_")
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name)
    # 結合していない素のシリーズでも意味のある名前になる
    plain = export_dicom.dicom_folder_name(volumes[0])
    assert re.fullmatch(r"[A-Za-z0-9._-]+", plain) and len(plain) > 8


# ----------------------------------------------------------------------
# CT Image IOD の必須属性が揃っているか (他ソフトで開けるかの最低条件)
# Type 1 = 必須かつ値が必要 / Type 2 = 必須だが空でもよい
CT_TYPE1 = ["SOPClassUID", "SOPInstanceUID", "StudyInstanceUID",
            "SeriesInstanceUID", "Modality", "FrameOfReferenceUID",
            "ImageType", "Rows", "Columns", "BitsAllocated", "BitsStored",
            "HighBit", "PixelRepresentation", "SamplesPerPixel",
            "PhotometricInterpretation", "RescaleIntercept", "RescaleSlope",
            "PixelData"]
CT_TYPE2 = ["PatientName", "PatientID", "PatientBirthDate", "PatientSex",
            "StudyDate", "StudyTime", "StudyID", "AccessionNumber",
            "SeriesNumber", "InstanceNumber", "ImagePositionPatient",
            "ImageOrientationPatient", "PixelSpacing", "SliceThickness"]


def test_required_ct_attributes_present(exported):
    _, out = exported
    ds = pydicom.dcmread(str(sorted(out.glob("*.dcm"))[0]))
    missing1 = [t for t in CT_TYPE1
                if not hasattr(ds, t) or getattr(ds, t) in (None, "")]
    missing2 = [t for t in CT_TYPE2 if not hasattr(ds, t)]
    assert not missing1, f"Type 1 属性が不足: {missing1}"
    assert not missing2, f"Type 2 属性が不足: {missing2}"


def test_file_meta_is_valid(exported):
    """プリアンブルとファイルメタが正しく、他ソフトが開けること。"""
    from pydicom.dataset import validate_file_meta
    _, out = exported
    for f in sorted(out.glob("*.dcm"))[:3]:
        ds = pydicom.dcmread(str(f))
        validate_file_meta(ds.file_meta, enforce_standard=True)
        assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID
        assert ds.file_meta.TransferSyntaxUID == pydicom.uid.ExplicitVRLittleEndian


def test_sop_instance_uids_are_unique(exported):
    _, out = exported
    uids = {str(pydicom.dcmread(str(f)).SOPInstanceUID)
            for f in out.glob("*.dcm")}
    assert len(uids) == len(list(out.glob("*.dcm")))


def test_all_slices_share_series_and_frame(exported):
    _, out = exported
    series = {str(pydicom.dcmread(str(f)).SeriesInstanceUID)
              for f in out.glob("*.dcm")}
    frames = {str(pydicom.dcmread(str(f)).FrameOfReferenceUID)
              for f in out.glob("*.dcm")}
    assert len(series) == 1 and len(frames) == 1
