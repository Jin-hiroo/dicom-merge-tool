"""DICOM シリーズの検出と読み込み。

読み込みは 1 スライスずつ memmap に書き出すストリーミング方式。
全スライスを一度に RAM へ載せないため 500 スライス超でも安全。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError

from app import config
from app.core.volume import Volume, new_memmap

log = logging.getLogger(__name__)


@dataclass
class SeriesInfo:
    """ピクセルを読まずにヘッダだけで作るシリーズの要約。"""
    uid: str
    folder: Path
    files: list = field(default_factory=list)       # (sort_key, path) をソート済みで保持
    description: str = ""
    modality: str = ""
    patient_id: str = ""
    patient_name: str = ""
    patient_birth_date: str = ""
    patient_sex: str = ""
    study_uid: str = ""
    study_date: str = ""
    study_time: str = ""
    study_id: str = ""
    study_description: str = ""
    accession_number: str = ""
    rows: int = 0
    cols: int = 0
    pixel_spacing: tuple = (1.0, 1.0)               # (行間隔, 列間隔) = (y, x)
    slice_spacing: float = 1.0
    spacing_consistent: bool = True
    spacing_warning: str = ""
    row_cosine: tuple = (1.0, 0.0, 0.0)
    col_cosine: tuple = (0.0, 1.0, 0.0)
    origin: tuple = (0.0, 0.0, 0.0)
    frame_of_reference: str = ""

    @property
    def n_slices(self) -> int:
        return len(self.files)

    @property
    def shape_zyx(self) -> tuple:
        return (self.n_slices, self.rows, self.cols)

    @property
    def nbytes(self) -> int:
        return self.n_slices * self.rows * self.cols * 2

    def label(self) -> str:
        desc = self.description or "(無題)"
        return f"{desc} — {self.n_slices} スライス / {self.cols}x{self.rows}"


# ----------------------------------------------------------------------
def _as_floats(value, default):
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return default


def scan_folder(folder, progress_cb=None, cancel_cb=None) -> list[SeriesInfo]:
    """フォルダを再帰走査し SeriesInstanceUID ごとにシリーズを構築する。

    ピクセルデータは読まない (stop_before_pixels=True) ので高速。
    """
    folder = Path(folder)
    paths = [p for p in folder.rglob("*") if p.is_file() and not p.name.startswith(".")]
    total = max(len(paths), 1)
    groups: dict[str, list] = {}
    headers: dict[str, pydicom.Dataset] = {}

    for idx, path in enumerate(paths):
        if cancel_cb and cancel_cb():
            return []
        if progress_cb and idx % 25 == 0:
            progress_cb(int(idx / total * 100), f"DICOM を走査中… {idx}/{total}")
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=False)
        except (InvalidDicomError, OSError, AttributeError):
            continue
        uid = getattr(ds, "SeriesInstanceUID", None)
        if uid is None or not hasattr(ds, "PixelSpacing"):
            continue
        groups.setdefault(uid, []).append((path, ds))
        headers.setdefault(uid, ds)

    series: list[SeriesInfo] = []
    for uid, items in groups.items():
        info = _build_series(uid, folder, items, headers[uid])
        if info is not None:
            series.append(info)

    series.sort(key=lambda s: (-s.n_slices, s.description))
    if progress_cb:
        progress_cb(100, f"{len(series)} 件のシリーズを検出しました")
    return series


def _build_series(uid, folder, items, ref) -> SeriesInfo | None:
    iop = _as_floats(getattr(ref, "ImageOrientationPatient", None),
                     (1.0, 0.0, 0.0, 0.0, 1.0, 0.0))
    row_cos = np.array(iop[:3], float)
    col_cos = np.array(iop[3:], float)
    n = np.cross(row_cos, col_cos)
    norm = np.linalg.norm(n)
    if norm < 1e-9:
        log.warning("シリーズ %s: ImageOrientationPatient が不正", uid)
        return None
    n /= norm

    # 法線方向への射影でソートする (InstanceNumber は信用しない)
    keyed = []
    for path, ds in items:
        ipp = _as_floats(getattr(ds, "ImagePositionPatient", None), (0.0, 0.0, 0.0))
        keyed.append((float(np.dot(np.array(ipp, float), n)), path, np.array(ipp, float)))
    keyed.sort(key=lambda t: t[0])

    projections = np.array([k[0] for k in keyed], float)
    spacing_z, consistent, warn = _slice_spacing(projections, ref)

    ps = _as_floats(getattr(ref, "PixelSpacing", None), (1.0, 1.0))

    return SeriesInfo(
        uid=uid,
        folder=Path(folder),
        files=[(k[0], k[1]) for k in keyed],
        description=str(getattr(ref, "SeriesDescription", "") or ""),
        modality=str(getattr(ref, "Modality", "") or ""),
        patient_id=str(getattr(ref, "PatientID", "") or ""),
        patient_name=str(getattr(ref, "PatientName", "") or ""),
        patient_birth_date=str(getattr(ref, "PatientBirthDate", "") or ""),
        patient_sex=str(getattr(ref, "PatientSex", "") or ""),
        study_uid=str(getattr(ref, "StudyInstanceUID", "") or ""),
        study_date=str(getattr(ref, "StudyDate", "") or ""),
        study_time=str(getattr(ref, "StudyTime", "") or ""),
        study_id=str(getattr(ref, "StudyID", "") or ""),
        study_description=str(getattr(ref, "StudyDescription", "") or ""),
        accession_number=str(getattr(ref, "AccessionNumber", "") or ""),
        rows=int(getattr(ref, "Rows", 0)),
        cols=int(getattr(ref, "Columns", 0)),
        pixel_spacing=(float(ps[0]), float(ps[1])),
        slice_spacing=spacing_z,
        spacing_consistent=consistent,
        spacing_warning=warn,
        row_cosine=tuple(row_cos),
        col_cosine=tuple(col_cos),
        origin=tuple(keyed[0][2]) if keyed else (0.0, 0.0, 0.0),
        frame_of_reference=str(getattr(ref, "FrameOfReferenceUID", "") or ""),
    )


def _slice_spacing(projections: np.ndarray, ref) -> tuple[float, bool, str]:
    """スライス間隔とその一貫性を判定する。"""
    if len(projections) < 2:
        fallback = float(getattr(ref, "SliceThickness", 1.0) or 1.0)
        return fallback, True, ""
    diffs = np.diff(projections)
    median = float(np.median(diffs))
    if abs(median) < 1e-9:
        return 1.0, False, "スライス位置が重複しています"
    spread = float(np.max(np.abs(diffs - median)))
    if spread > max(0.01 * abs(median), 1e-3):
        warn = (f"スライス間隔が不均等です (中央値 {median:.3f} mm, "
                f"最大ずれ {spread:.3f} mm)。等間隔として読み込みます。")
        return abs(median), False, warn
    return abs(median), True, ""


# ----------------------------------------------------------------------
def load_series(info: SeriesInfo, scratch_dir=None,
                progress_cb=None, cancel_cb=None) -> Volume | None:
    """シリーズを HU の int16 memmap として読み込み Volume を返す。

    キャンセルされた場合は None を返す。
    """
    nz, ny, nx = info.shape_zyx
    if nz == 0 or ny == 0 or nx == 0:
        raise ValueError("シリーズにスライスがありません")

    arr, path = new_memmap((nz, ny, nx), scratch_dir, prefix="series")

    try:
        for k, (_, fpath) in enumerate(info.files):
            if cancel_cb and cancel_cb():
                arr.flush()
                del arr
                Path(path).unlink(missing_ok=True)
                return None
            if progress_cb and (k % 5 == 0 or k == nz - 1):
                progress_cb(int((k + 1) / nz * 100),
                            f"{info.description or 'シリーズ'} を読込中… {k + 1}/{nz}")
            arr[k] = _read_hu_slice(fpath, ny, nx)
        arr.flush()
    except Exception:
        del arr
        Path(path).unlink(missing_ok=True)
        raise

    direction = np.stack([np.array(info.row_cosine, float),
                          np.array(info.col_cosine, float),
                          np.cross(info.row_cosine, info.col_cosine)], axis=1)
    # PixelSpacing = [行間隔(y), 列間隔(x)] の順である点に注意
    spacing = (float(info.pixel_spacing[1]),
               float(info.pixel_spacing[0]),
               float(info.slice_spacing))

    return Volume(
        array=arr,
        spacing=spacing,
        origin=tuple(float(v) for v in info.origin),
        direction=direction,
        name=info.description or f"Series {info.uid[-8:]}",
        meta={
            "series_uid": info.uid,
            "modality": info.modality,
            "frame_of_reference": info.frame_of_reference,
            "n_slices": nz,
            "spacing_warning": info.spacing_warning,
            # DICOM 書き出しで患者・検査の同一性を保つために引き継ぐ
            "patient_id": info.patient_id,
            "patient_name": info.patient_name,
            "patient_birth_date": info.patient_birth_date,
            "patient_sex": info.patient_sex,
            "study_uid": info.study_uid,
            "study_date": info.study_date,
            "study_time": info.study_time,
            "study_id": info.study_id,
            "study_description": info.study_description,
            "accession_number": info.accession_number,
        },
        path=Path(path),
    )


def _read_hu_slice(path, ny, nx) -> np.ndarray:
    ds = pydicom.dcmread(str(path))
    px = ds.pixel_array
    if px.shape != (ny, nx):
        raise ValueError(f"{Path(path).name}: スライス寸法が不一致 "
                         f"({px.shape} != {(ny, nx)})")
    slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
    intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
    hu = px.astype(np.float32) * slope + intercept
    return np.clip(hu, config.HU_MIN, config.HU_MAX).astype(np.int16)
