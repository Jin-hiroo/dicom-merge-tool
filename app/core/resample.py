"""共通グリッドへのリサンプルとボリューム融合。

「結合」の本体。位置合わせ済みの 2 つのボリュームを、患者座標系に
軸平行な共通グリッド上でボクセル単位に融合し、新しい Volume を返す。

メモリ対策として出力 Z スラブ単位で処理し、結果は memmap に直接書く。
出力全体を RAM に持たないので 500 スライス超でも安全。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import vtk
from vtkmodules.util import numpy_support

from app import config
from app.core import blend as blend_mod
from app.core.memory import fit_spacing_to_budget, human, volume_bytes
from app.core.transform import inv44
from app.core.volume import Volume, new_memmap


# ----------------------------------------------------------------------
def _plain_image(volume: Volume):
    """origin=0 / 単位方向の vtkImageData を作る。

    vtkImageReslice の DirectionMatrix 対応はバージョン差があるため頼らず、
    ボリュームの真のアフィンはリサンプル変換側へ折り込む。
    """
    arr = np.ascontiguousarray(volume.array)
    img = vtk.vtkImageData()
    nx, ny, nz = volume.shape_xyz
    img.SetDimensions(nx, ny, nz)
    img.SetSpacing(*[float(s) for s in volume.spacing])
    img.SetOrigin(0.0, 0.0, 0.0)
    va = numpy_support.numpy_to_vtk(arr.ravel(order="C"), deep=0,
                                    array_type=vtk.VTK_SHORT)
    va.SetName("HU")
    img.GetPointData().SetScalars(va)
    img._numpy_ref = arr        # noqa: SLF001
    img._vtk_arr_ref = va       # noqa: SLF001
    return img


def _ones_image(volume: Volume):
    """有効領域判定用の uint8 全 1 ボリューム。"""
    nx, ny, nz = volume.shape_xyz
    ones = np.full((nz, ny, nx), 255, dtype=np.uint8)
    img = vtk.vtkImageData()
    img.SetDimensions(nx, ny, nz)
    img.SetSpacing(*[float(s) for s in volume.spacing])
    img.SetOrigin(0.0, 0.0, 0.0)
    va = numpy_support.numpy_to_vtk(ones.ravel(order="C"), deep=0,
                                    array_type=vtk.VTK_UNSIGNED_CHAR)
    va.SetName("valid")
    img.GetPointData().SetScalars(va)
    img._numpy_ref = ones       # noqa: SLF001
    img._vtk_arr_ref = va       # noqa: SLF001
    return img


def _reslice_matrix(volume: Volume, world_transform: np.ndarray | None) -> vtk.vtkMatrix4x4:
    """出力世界座標 → 入力 vtkImageData 座標 の 4x4。

        p(出力世界) → T^-1 で元の患者座標へ戻す → A^-1 でボクセル添字へ
        → spacing 倍して origin=0 の入力画像座標へ
    """
    a_inv = inv44(volume.affine)
    scale = np.eye(4)
    scale[:3, :3] = np.diag([float(s) for s in volume.spacing])
    m = scale @ a_inv
    if world_transform is not None:
        m = m @ inv44(world_transform)

    mat = vtk.vtkMatrix4x4()
    for r in range(4):
        for c in range(4):
            mat.SetElement(r, c, float(m[r, c]))
    return mat


def _make_reslicer(image, matrix, grid, interpolate: str, background: float):
    r = vtk.vtkImageReslice()
    r.SetInputData(image)
    r.SetResliceAxes(matrix)
    r.SetOutputOrigin(*[float(v) for v in grid["origin"]])
    r.SetOutputSpacing(*[float(v) for v in grid["spacing"]])
    r.SetBackgroundLevel(float(background))
    if interpolate == "linear":
        r.SetInterpolationModeToLinear()
    else:
        r.SetInterpolationModeToNearestNeighbor()
    r.AutoCropOutputOff()
    return r


def _slab_to_numpy(reslicer, grid, z0: int, z1: int, dtype) -> np.ndarray:
    nx, ny, _ = grid["dims"]
    reslicer.SetOutputExtent(0, nx - 1, 0, ny - 1, z0, z1)
    reslicer.Update()
    out = reslicer.GetOutput()
    arr = numpy_support.vtk_to_numpy(out.GetPointData().GetScalars())
    return arr.reshape(z1 - z0 + 1, ny, nx).astype(dtype, copy=False)


# ----------------------------------------------------------------------
def compute_output_grid(fixed: Volume, moving: Volume,
                        transform: np.ndarray | None = None,
                        spacing_override=None,
                        budget_bytes: int = config.MAX_VOLUME_BYTES) -> dict:
    """融合結果を載せる軸平行グリッドを決める。

    spacing は既定で「細かいほう」を採用し、予算超過なら収まるまで粗くする。
    """
    fb = fixed.world_bounds()
    mb = moving.world_bounds(transform)
    bounds = np.stack([np.minimum(fb[0], mb[0]), np.maximum(fb[1], mb[1])])

    if spacing_override is not None:
        spacing = np.asarray(spacing_override, float)
    else:
        spacing = np.minimum(np.asarray(fixed.spacing, float),
                             np.asarray(moving.spacing, float))

    spacing, dims, adjusted, scale = fit_spacing_to_budget(
        bounds, spacing, budget_bytes=budget_bytes, itemsize=2)

    note = ""
    if adjusted:
        note = (f"メモリ上限のため出力解像度を "
                f"({spacing[0]:.3f}, {spacing[1]:.3f}, {spacing[2]:.3f}) mm "
                f"に調整しました (元の {scale:.2f} 倍)")

    return {
        "origin": tuple(float(v) for v in bounds[0]),
        "spacing": tuple(float(v) for v in spacing),
        "dims": tuple(int(v) for v in dims),
        "bounds": bounds,
        "adjusted": adjusted,
        "note": note,
        "bytes": volume_bytes((dims[2], dims[1], dims[0]), 2),
        "fixed_bounds": fb,
        "moving_bounds": mb,
    }


def describe_grid(grid: dict) -> str:
    nx, ny, nz = grid["dims"]
    sx, sy, sz = grid["spacing"]
    return (f"{nx} x {ny} x {nz} ボクセル / "
            f"{sx:.3f} x {sy:.3f} x {sz:.3f} mm / {human(grid['bytes'])}")


# ----------------------------------------------------------------------
def merge_volumes(fixed: Volume, moving: Volume,
                  transform: np.ndarray | None = None,
                  mode: str = blend_mod.MODE_FEATHER,
                  spacing_override=None,
                  scratch_dir=None,
                  name: str | None = None,
                  progress_cb=None, cancel_cb=None) -> tuple[Volume | None, dict]:
    """2 つのボリュームを融合し、新しい Volume を返す。

    Returns (volume | None, info)。キャンセル時は (None, info)。
    """
    grid = compute_output_grid(fixed, moving, transform,
                               spacing_override=spacing_override)
    nx, ny, nz = grid["dims"]

    if progress_cb:
        progress_cb(0, f"出力グリッド: {describe_grid(grid)}")

    fixed_img, fixed_ones = _plain_image(fixed), _ones_image(fixed)
    moving_img, moving_ones = _plain_image(moving), _ones_image(moving)

    m_fixed = _reslice_matrix(fixed, None)
    m_moving = _reslice_matrix(moving, transform)

    r_f = _make_reslicer(fixed_img, m_fixed, grid, "linear", config.AIR_HU)
    r_fm = _make_reslicer(fixed_ones, m_fixed, grid, "linear", 0)
    r_m = _make_reslicer(moving_img, m_moving, grid, "linear", config.AIR_HU)
    r_mm = _make_reslicer(moving_ones, m_moving, grid, "linear", 0)

    # feather 用の 1 次元 ramp を継ぎ目の軸に沿って用意する
    axis, lo, hi, a_first = blend_mod.seam_axis(grid["fixed_bounds"],
                                                grid["moving_bounds"])
    origin = np.asarray(grid["origin"], float)
    spacing = np.asarray(grid["spacing"], float)

    out, path = new_memmap((nz, ny, nx), scratch_dir, prefix="merged")
    slab = max(1, int(config.RESLICE_SLAB_SLICES))
    cancelled = False

    try:
        for z0 in range(0, nz, slab):
            if cancel_cb and cancel_cb():
                cancelled = True
                break
            z1 = min(z0 + slab - 1, nz - 1)
            if progress_cb:
                progress_cb(int(z0 / max(nz, 1) * 100),
                            f"ボリュームを融合中… {z0}/{nz} スライス")

            a_vals = _slab_to_numpy(r_f, grid, z0, z1, np.int16)
            a_mask = _slab_to_numpy(r_fm, grid, z0, z1, np.uint8) >= 250
            b_vals = _slab_to_numpy(r_m, grid, z0, z1, np.int16)
            b_mask = _slab_to_numpy(r_mm, grid, z0, z1, np.uint8) >= 250

            weights = None
            if mode == blend_mod.MODE_FEATHER:
                weights = _slab_weights(axis, lo, hi, a_first, origin, spacing,
                                        z0, z1, ny, nx)

            out[z0:z1 + 1] = blend_mod.blend_slab(a_vals, a_mask, b_vals, b_mask,
                                                  mode=mode, weights_b=weights)
        out.flush()
    except Exception:
        del out
        Path(path).unlink(missing_ok=True)
        raise

    info = {
        "grid": grid,
        "seam_axis": "xyz"[axis],
        "overlap_mm": max(0.0, hi - lo),
        "note": grid["note"],
    }

    if cancelled:
        del out
        Path(path).unlink(missing_ok=True)
        return None, info

    if progress_cb:
        progress_cb(100, "融合が完了しました")

    merged = Volume(
        array=out,
        spacing=grid["spacing"],
        origin=grid["origin"],
        direction=np.eye(3),
        name=name or f"{fixed.name} + {moving.name}",
        meta=_merged_meta(fixed, moving, mode, info),
        path=Path(path),
    )
    return merged, info


# 結合を重ねても患者・検査の同一性が失われないよう Fixed 側から引き継ぐ。
# DICOM 書き出しのときにここが空だと、別患者のデータに見えてしまう。
_INHERITED_META = ("patient_id", "patient_name", "patient_birth_date",
                   "patient_sex", "study_uid", "study_date", "study_time",
                   "study_id", "study_description", "accession_number")


def _merged_meta(fixed: Volume, moving: Volume, mode: str, info: dict) -> dict:
    meta = {k: fixed.meta.get(k) or moving.meta.get(k, "")
            for k in _INHERITED_META}
    sources = list(fixed.meta.get("source_names", [fixed.name]))
    sources += list(moving.meta.get("source_names", [moving.name]))
    meta.update({
        "merged_from": [fixed.name, moving.name],
        "source_names": sources,
        "blend_mode": mode,
        "overlap_mm": info["overlap_mm"],
        "seam_axis": info["seam_axis"],
    })
    return meta


def _slab_weights(axis: int, lo: float, hi: float, a_first: bool,
                  origin: np.ndarray, spacing: np.ndarray,
                  z0: int, z1: int, ny: int, nx: int) -> np.ndarray:
    """スラブ内の各ボクセルに対する B の重み (nz_slab, ny, nx)。"""
    nzs = z1 - z0 + 1
    if axis == 2:
        coords = origin[2] + spacing[2] * np.arange(z0, z1 + 1, dtype=np.float32)
        w = blend_mod.feather_weights(coords, lo, hi, a_first)
        return np.broadcast_to(w[:, None, None], (nzs, ny, nx))
    if axis == 1:
        coords = origin[1] + spacing[1] * np.arange(ny, dtype=np.float32)
        w = blend_mod.feather_weights(coords, lo, hi, a_first)
        return np.broadcast_to(w[None, :, None], (nzs, ny, nx))
    coords = origin[0] + spacing[0] * np.arange(nx, dtype=np.float32)
    w = blend_mod.feather_weights(coords, lo, hi, a_first)
    return np.broadcast_to(w[None, None, :], (nzs, ny, nx))


# ----------------------------------------------------------------------
# 2D 直交スライスのサンプリング (位置合わせ補助ビュー用)
# ----------------------------------------------------------------------
# 出力の (u, v, w) をどの世界軸に割り当てるか。w がスライス法線。
_PLANE_AXES = {
    2: (0, 1, 2),   # axial   : u=x, v=y, w=z
    1: (0, 2, 1),   # coronal : u=x, v=z, w=y
    0: (1, 2, 0),   # sagittal: u=y, v=z, w=x
}


def _plane_matrix(axis: int) -> np.ndarray:
    """スライス空間 (u,v,w) → 世界 (x,y,z) の 4x4。"""
    au, av, aw = _PLANE_AXES[axis]
    p = np.zeros((4, 4))
    p[au, 0] = 1.0
    p[av, 1] = 1.0
    p[aw, 2] = 1.0
    p[3, 3] = 1.0
    return p


def plane_extent(bounds: np.ndarray, axis: int) -> tuple:
    """面内 2 軸の (min_u, max_u, min_v, max_v)。"""
    au, av, _ = _PLANE_AXES[axis]
    return (float(bounds[0][au]), float(bounds[1][au]),
            float(bounds[0][av]), float(bounds[1][av]))


def sample_plane(volume: Volume, axis: int, position: float,
                 bounds: np.ndarray, width: int, height: int,
                 world_transform: np.ndarray | None = None):
    """任意の直交平面上でボリュームをサンプルし (値, 有効マスク) を返す。

    2D 重ね合わせビュー用。1 枚ぶんなので毎フレーム呼んでも軽い。
    """
    min_u, max_u, min_v, max_v = plane_extent(bounds, axis)
    su = (max_u - min_u) / max(width - 1, 1)
    sv = (max_v - min_v) / max(height - 1, 1)
    su = su if su > 0 else 1.0
    sv = sv if sv > 0 else 1.0

    p = _plane_matrix(axis)
    a_inv = inv44(volume.affine)
    scale = np.eye(4)
    scale[:3, :3] = np.diag([float(s) for s in volume.spacing])
    m = scale @ a_inv
    if world_transform is not None:
        m = m @ inv44(world_transform)
    m = m @ p

    mat = vtk.vtkMatrix4x4()
    for r in range(4):
        for c in range(4):
            mat.SetElement(r, c, float(m[r, c]))

    grid = {"origin": (min_u, min_v, float(position)),
            "spacing": (su, sv, 1.0),
            "dims": (width, height, 1)}

    r_val = _make_reslicer(_plain_image(volume), mat, grid, "linear", config.AIR_HU)
    r_msk = _make_reslicer(_ones_image(volume), mat, grid, "linear", 0)

    vals = _slab_to_numpy(r_val, grid, 0, 0, np.int16)[0]
    mask = _slab_to_numpy(r_msk, grid, 0, 0, np.uint8)[0] >= 250
    return vals, mask
