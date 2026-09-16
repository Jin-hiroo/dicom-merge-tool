"""CT DICOM シリーズとしての書き出し。

STL は表面しか持たないが、DICOM ならボリュームそのものを他の医用ソフト
(3D Slicer / PACS ビューア / 造形ソフト) にそのまま渡せる。

この書き出しは ``dicom_io.py`` の読み込みのちょうど逆変換になっており、
書き出し → 読み戻しでジオメトリ (spacing / origin / direction) と HU が
完全に一致する (tests/test_export_dicom.py のラウンドトリップ試験で担保)。

生成物は元データそのものではなく *派生データ* なので、ImageType を
DERIVED/SECONDARY とし、由来を DerivationDescription に残す。患者・検査
情報は元シリーズから引き継ぎ、SeriesInstanceUID と FrameOfReferenceUID は
新規に採番する (ジオメトリが変わっているため)。
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from app import config
from app.core.volume import Volume

# 16bit 符号なし + intercept という実機 CT と同じ格納形式にする。
# 符号付きより対応ビューアが多い。
STORED_INTERCEPT = -1024.0
STORED_SLOPE = 1.0
STORED_MAX = 65535

# 骨を見る既定の表示ウィンドウ
DEFAULT_WINDOW_CENTER = 500
DEFAULT_WINDOW_WIDTH = 2000

# 元検査のシリーズ番号と衝突しないよう高い番号から振る
DERIVED_SERIES_NUMBER_BASE = 9000


def sanitize_name(text: str, fallback: str = "MERGED") -> str:
    """フォルダ名に使える ASCII 文字列にする。

    日本語名 ("結合結果 #1 (2 シリーズ)") を素通しすると数字と記号しか
    残らず "1_2" のような無意味な名前になるため、英数字が 3 文字未満に
    なった場合は fallback を使う。
    """
    ascii_only = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_")
    if len(re.sub(r"[^A-Za-z0-9]", "", ascii_only)) < 3:
        return fallback
    return ascii_only


def dicom_folder_name(volume: Volume) -> str:
    """書き出し先フォルダ名。日付を入れて取り違えを防ぐ。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    sources = volume.meta.get("source_names") or volume.meta.get("merged_from")
    if sources:
        return f"MERGED_CT_{len(sources)}series_{stamp}"
    return f"{sanitize_name(volume.name, 'CT')}_{stamp}"


def unique_dir(parent, name: str) -> Path:
    """<parent>/<name> を作る。既にあれば連番を付ける。"""
    parent = Path(parent)
    candidate = parent / name
    n = 2
    while candidate.exists():
        candidate = parent / f"{name}_{n}"
        n += 1
    candidate.mkdir(parents=True)
    return candidate


def _derivation_text(volume: Volume) -> str:
    """由来を ASCII で 1 行にまとめる (DerivationDescription は短い方が安全)。"""
    sources = volume.meta.get("source_names") or volume.meta.get("merged_from")
    parts = [f"{config.APP_NAME.split()[0]} merged volume"]
    if sources:
        parts.append("from " + ", ".join(sanitize_name(s) for s in sources))
    if volume.meta.get("blend_mode"):
        parts.append(f"blend={volume.meta['blend_mode']}")
    if volume.meta.get("overlap_mm"):
        parts.append(f"overlap={float(volume.meta['overlap_mm']):.1f}mm")
    text = "; ".join(parts)
    return text[:1024]


def _orientation(volume: Volume) -> tuple:
    """ImageOrientationPatient = [行方向余弦, 列方向余弦]。

    direction の 0 列目が i(列番号が増える向き = 行方向)、
    1 列目が j(行番号が増える向き = 列方向)。
    """
    d = np.asarray(volume.direction, float)
    return tuple(float(v) for v in d[:, 0]) + tuple(float(v) for v in d[:, 1])


def _slice_normal(volume: Volume) -> np.ndarray:
    return np.asarray(volume.direction, float)[:, 2]


def plan_export(volume: Volume, out_dir, series_description: str | None = None) -> dict:
    """書き出し前に見積もりを返す (UI の確認ダイアログ用)。"""
    nz, ny, nx = volume.shape_zyx
    per_slice = ny * nx * 2
    return {
        "slices": nz,
        "rows": ny,
        "columns": nx,
        "bytes_per_slice": per_slice,
        "total_bytes": per_slice * nz + nz * 2048,   # ヘッダぶんを概算で加算
        "folder": str(out_dir),
        "series_description": series_description or default_description(volume),
    }


def default_description(volume: Volume) -> str:
    """既定のシリーズ説明。互換性優先で ASCII にしておく。"""
    n = len(volume.meta.get("source_names") or []) or 2
    if volume.meta.get("merged_from"):
        return f"MERGED CT ({n} series) - head3Dv1"
    return f"{sanitize_name(volume.name, 'CT')} - head3Dv1"


def export_volume_to_dicom(volume: Volume, out_dir,
                           series_description: str | None = None,
                           threshold: float | None = None,
                           series_number: int | None = None,
                           progress_cb=None, cancel_cb=None) -> dict | None:
    """ボリュームを CT DICOM シリーズとして out_dir に書き出す。

    Parameters
    ----------
    threshold
        指定すると、この HU 未満のボクセルを空気 (-1024 HU) に置き換えて
        書き出す。骨だけのボリュームを渡したい場合に使う。None なら素のまま。

    Returns
    -------
    dict | None
        書き出し結果の要約。キャンセル時は None (書きかけのファイルは削除する)。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    nz, ny, nx = volume.shape_zyx
    if nz == 0 or ny == 0 or nx == 0:
        raise ValueError("空のボリュームは書き出せません")

    meta = volume.meta or {}
    now = datetime.datetime.now()
    date_str, time_str = now.strftime("%Y%m%d"), now.strftime("%H%M%S")

    series_uid = generate_uid()
    frame_uid = generate_uid()
    # 同じ検査にぶら下げたいので StudyInstanceUID は引き継ぐ
    study_uid = meta.get("study_uid") or generate_uid()
    description = series_description or default_description(volume)
    derivation = _derivation_text(volume)
    orientation = list(_orientation(volume))
    normal = _slice_normal(volume)
    origin = np.asarray(volume.origin, float)
    sx, sy, sz = (float(v) for v in volume.spacing)

    written: list[Path] = []
    try:
        for k in range(nz):
            if cancel_cb and cancel_cb():
                _cleanup(written)
                return None
            if progress_cb and (k % 5 == 0 or k == nz - 1):
                progress_cb(int((k + 1) / nz * 100),
                            f"DICOM を書き出し中… {k + 1}/{nz}")

            position = origin + normal * (k * sz)
            path = out_dir / f"IM{k + 1:05d}.dcm"
            ds = _build_slice(
                volume, k, position, orientation, (sx, sy, sz),
                study_uid=study_uid, series_uid=series_uid, frame_uid=frame_uid,
                description=description, derivation=derivation, meta=meta,
                date_str=date_str, time_str=time_str, n_slices=nz,
                series_number=series_number or DERIVED_SERIES_NUMBER_BASE + 1,
                threshold=threshold,
            )
            ds.save_as(str(path), enforce_file_format=True)
            written.append(path)
    except Exception:
        _cleanup(written)
        raise

    total = sum(p.stat().st_size for p in written)
    if progress_cb:
        progress_cb(100, "DICOM の書き出しが完了しました")

    return {
        "folder": str(out_dir),
        "files": len(written),
        "slices": nz,
        "series_uid": series_uid,
        "study_uid": study_uid,
        "frame_of_reference": frame_uid,
        "series_description": description,
        "total_bytes": total,
        "spacing": (sx, sy, sz),
        "shape_xyz": volume.shape_xyz,
        "thresholded": threshold is not None,
    }


def _cleanup(paths):
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


def _build_slice(volume: Volume, k: int, position, orientation, spacing,
                 *, study_uid, series_uid, frame_uid, description, derivation,
                 meta, date_str, time_str, n_slices, series_number,
                 threshold) -> FileDataset:
    sx, sy, sz = spacing
    sop_uid = generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    ds = FileDataset("", Dataset(), file_meta=file_meta, preamble=b"\0" * 128)
    ds.SpecificCharacterSet = "ISO_IR 192"      # UTF-8 (説明に日本語が来ても壊れない)

    # --- 患者 (元シリーズから引き継ぐ) ---------------------------------
    ds.PatientName = meta.get("patient_name") or "ANONYMOUS"
    ds.PatientID = meta.get("patient_id") or "UNKNOWN"
    ds.PatientBirthDate = meta.get("patient_birth_date") or ""
    ds.PatientSex = meta.get("patient_sex") or ""

    # --- 検査 (同じ検査にぶら下げる) -----------------------------------
    ds.StudyInstanceUID = study_uid
    ds.StudyDate = meta.get("study_date") or date_str
    ds.StudyTime = meta.get("study_time") or time_str
    ds.StudyID = meta.get("study_id") or "1"
    ds.StudyDescription = meta.get("study_description") or ""
    ds.AccessionNumber = meta.get("accession_number") or ""

    # --- シリーズ (新規採番) -------------------------------------------
    ds.Modality = "CT"
    ds.SeriesInstanceUID = series_uid
    ds.SeriesNumber = int(series_number)
    ds.SeriesDescription = description
    ds.FrameOfReferenceUID = frame_uid
    ds.PositionReferenceIndicator = ""

    # --- この画像 -------------------------------------------------------
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.InstanceNumber = k + 1
    # 元データそのものではないことを明示する
    ds.ImageType = ["DERIVED", "SECONDARY", "AXIAL"]
    ds.DerivationDescription = derivation
    ds.ContentDate = date_str
    ds.ContentTime = time_str
    ds.AcquisitionNumber = 1
    ds.ImagesInAcquisition = int(n_slices)

    ds.ImagePositionPatient = [float(v) for v in position]
    ds.ImageOrientationPatient = [float(v) for v in orientation]
    ds.SliceLocation = float(np.dot(position, _slice_normal(volume)))
    # PixelSpacing は [行間隔(y), 列間隔(x)] の順である点に注意
    ds.PixelSpacing = [float(sy), float(sx)]
    ds.SliceThickness = float(sz)
    ds.SpacingBetweenSlices = float(sz)

    ds.Rows = int(volume.shape_zyx[1])
    ds.Columns = int(volume.shape_zyx[2])
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0                  # 符号なし + intercept
    ds.RescaleIntercept = STORED_INTERCEPT
    ds.RescaleSlope = STORED_SLOPE
    ds.RescaleType = "HU"
    ds.WindowCenter = DEFAULT_WINDOW_CENTER
    ds.WindowWidth = DEFAULT_WINDOW_WIDTH

    hu = np.asarray(volume.array[k], dtype=np.int32)
    if threshold is not None:
        hu = np.where(hu >= int(threshold), hu, int(config.AIR_HU))
    stored = np.clip(hu - int(STORED_INTERCEPT), 0, STORED_MAX).astype(np.uint16)
    ds.PixelData = stored.tobytes()

    return ds


def read_back_volume(folder, scratch_dir=None):
    """書き出した DICOM を読み戻す (検証用の薄いヘルパ)。"""
    from app.core import dicom_io
    series = dicom_io.scan_folder(folder)
    if not series:
        raise ValueError(f"{folder} から CT シリーズを検出できませんでした")
    return dicom_io.load_series(series[0], scratch_dir)
