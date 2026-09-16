"""検証用の合成 DICOM シリーズを作る。患者データなしでパイプラインを検証できる。

ひとつの「正解オブジェクト」を z 方向に 2 分割して撮影したことにし、
シリーズ B の ImagePositionPatient だけを既知の量だけずらして書く。
= 別々に撮った CT で患者座標が食い違っている状況の再現。

したがって B を (-offset) だけ動かせば正解に戻るはずで、
位置合わせ・リサンプル・融合を厳密に検証できる。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

AIR = -1000
BONE = 900
SOFT = 40


def phantom_hu(x, y, z):
    """世界座標 (mm) から HU を返す。回転を検出できるよう非対称にしてある。"""
    out = np.full(x.shape, AIR, dtype=np.int16)

    r = np.sqrt(x ** 2 + y ** 2)
    in_z = (z >= 0.0) & (z <= 200.0)

    # 体幹に相当する円柱 (外側は空気なので閉じた表面になる)
    body = (r <= 40.0) & in_z
    out[body] = SOFT

    # 骨に相当する外殻 (中空の円筒)
    shell = (r <= 40.0) & (r >= 32.0) & in_z
    out[shell] = BONE

    # 非対称な目印: +x 側だけに走る稜線 (回転ズレを検出できる)
    ridge = (np.abs(y) <= 7.0) & (x > 12.0) & (x <= 28.0) & (z >= 20.0) & (z <= 180.0)
    out[ridge] = BONE

    # 重複域のちょうど真ん中に球を置き、継ぎ目の検証に使う
    sphere = ((x - 6.0) ** 2 + (y + 14.0) ** 2 + (z - 100.0) ** 2) <= 10.0 ** 2
    out[sphere] = BONE
    return out


def sample_volume(origin, spacing, dims):
    """(nz, ny, nx) の HU ボリュームを軸平行グリッド上でサンプルする。"""
    nx, ny, nz = dims
    xs = origin[0] + spacing[0] * np.arange(nx)
    ys = origin[1] + spacing[1] * np.arange(ny)
    zs = origin[2] + spacing[2] * np.arange(nz)
    zz, yy, xx = np.meshgrid(zs, ys, xs, indexing="ij")
    return phantom_hu(xx, yy, zz)


def write_series(folder, volume, origin, spacing, description,
                 series_uid=None, study_uid=None, frame_uid=None):
    """(nz, ny, nx) のボリュームを CT DICOM シリーズとして書き出す。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    nz, ny, nx = volume.shape
    series_uid = series_uid or generate_uid()
    study_uid = study_uid or generate_uid()
    frame_uid = frame_uid or generate_uid()

    for k in range(nz):
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.ImplementationClassUID = generate_uid()

        ds = FileDataset(str(folder / f"slice_{k:04d}.dcm"), Dataset(),
                         file_meta=meta, preamble=b"\0" * 128)
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        ds.Modality = "CT"
        ds.PatientName = "PHANTOM^TEST"
        ds.PatientID = "PHANTOM001"
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.SeriesDescription = description
        ds.SeriesNumber = 1
        ds.InstanceNumber = k + 1

        ds.Rows, ds.Columns = ny, nx
        # PixelSpacing = [行間隔(y), 列間隔(x)] の順
        ds.PixelSpacing = [float(spacing[1]), float(spacing[0])]
        ds.SliceThickness = float(spacing[2])
        ds.SpacingBetweenSlices = float(spacing[2])
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.ImagePositionPatient = [float(origin[0]), float(origin[1]),
                                   float(origin[2] + k * spacing[2])]
        ds.SliceLocation = float(origin[2] + k * spacing[2])

        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0          # 符号なし + intercept (実機と同じ形)
        ds.RescaleIntercept = -1024.0
        ds.RescaleSlope = 1.0

        stored = np.clip(volume[k].astype(np.int32) + 1024, 0, 65535).astype(np.uint16)
        ds.PixelData = stored.tobytes()

        ds.save_as(str(folder / f"slice_{k:04d}.dcm"), enforce_file_format=True)

    return series_uid


def build(root, offset=(7.0, -4.0, 11.0), spacing=(1.0, 1.0, 2.0), clean=True):
    """2 シリーズを生成し、正解情報を返す。

    A: z = 0..120mm、B: z = 80..200mm (40mm 重複)。
    B の記録位置だけ offset ずらしてあるので、正解の補正は -offset。
    """
    root = Path(root)
    if clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    nx = ny = 96
    x0 = y0 = -(nx // 2) * spacing[0]

    # --- Series A: 真の z = 0..120 ---
    origin_a = (x0, y0, 0.0)
    nz_a = int(120 / spacing[2]) + 1
    vol_a = sample_volume(origin_a, spacing, (nx, ny, nz_a))
    write_series(root / "seriesA", vol_a, origin_a, spacing, "PHANTOM A (lower)")

    # --- Series B: 真の z = 80..200、ただし記録位置は offset ずれている ---
    true_origin_b = (x0, y0, 80.0)
    nz_b = int(120 / spacing[2]) + 1
    vol_b = sample_volume(true_origin_b, spacing, (nx, ny, nz_b))
    recorded_origin_b = tuple(float(true_origin_b[i] + offset[i]) for i in range(3))
    write_series(root / "seriesB", vol_b, recorded_origin_b, spacing,
                 "PHANTOM B (upper)", frame_uid=generate_uid())

    return {
        "root": root,
        "folder_a": root / "seriesA",
        "folder_b": root / "seriesB",
        "offset": tuple(float(v) for v in offset),
        "correction": tuple(float(-v) for v in offset),
        "spacing": spacing,
        "overlap_mm": 40.0,
        "true_bounds_z": (0.0, 200.0),
        "shape_a": vol_a.shape,
        "shape_b": vol_b.shape,
    }


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "tests/phantom_data"
    info = build(out)
    print("生成しました:", info["root"])
    print("  A:", info["shape_a"], "->", info["folder_a"])
    print("  B:", info["shape_b"], "->", info["folder_b"])
    print("  仕込んだズレ:", info["offset"], " 正解の補正:", info["correction"])
