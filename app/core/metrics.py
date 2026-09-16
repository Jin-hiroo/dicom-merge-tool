"""位置合わせの客観指標。

自動レジストレーションは使わない方針なので、ユーザーが「今の操作で
良くなったか」を数値で確認できることが重要になる。

2 種類を用意する:
  * SurfaceMetric  — 表面点の最近傍距離。KD木を一度作れば 1 回 10ms 程度で
                     済むため、ボタンを押すたびにライブ更新できる。
  * dice_score()   — ボクセル単位の Dice 係数。厳密だが重いのでオンデマンド。
"""
from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from app import config


def _polydata_points(poly) -> np.ndarray:
    from vtkmodules.util import numpy_support
    if poly is None or poly.GetNumberOfPoints() == 0:
        return np.zeros((0, 3), float)
    return numpy_support.vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)


def _subsample(points: np.ndarray, n: int, seed: int = 0) -> np.ndarray:
    if len(points) <= n:
        return points
    rng = np.random.default_rng(seed)
    return points[rng.choice(len(points), n, replace=False)]


class SurfaceMetric:
    """Fixed の表面に KD木を張り、Moving 表面との距離をライブ評価する。

    KD木の構築は Fixed が変わった時だけ。以降は Moving の点を変換して
    問い合わせるだけなので、移動ボタン連打にも追従できる。
    """

    def __init__(self, fixed_poly, moving_poly,
                 n_points: int = config.METRIC_SAMPLE_POINTS):
        self._fixed_pts = _subsample(_polydata_points(fixed_poly), n_points, seed=1)
        self._moving_pts = _subsample(_polydata_points(moving_poly), n_points, seed=2)
        self._tree = cKDTree(self._fixed_pts) if len(self._fixed_pts) else None
        if len(self._fixed_pts):
            self._fixed_bounds = np.stack([self._fixed_pts.min(axis=0),
                                           self._fixed_pts.max(axis=0)])
        else:
            self._fixed_bounds = None

    @property
    def ready(self) -> bool:
        return self._tree is not None and len(self._moving_pts) > 0

    def evaluate(self, transform: np.ndarray | None = None) -> dict:
        """変換後の Moving 表面と Fixed 表面の一致度。

        Returns dict(mean_mm, median_mm, rms_mm, n_overlap, coverage)
        重複領域に入っている Moving 点だけを対象にする (非重複部の距離は
        位置合わせの良し悪しと無関係なため)。
        """
        empty = {"mean_mm": float("nan"), "median_mm": float("nan"),
                 "rms_mm": float("nan"), "n_overlap": 0, "coverage": 0.0}
        if not self.ready:
            return empty

        pts = self._moving_pts
        if transform is not None:
            t = np.asarray(transform, float)
            pts = pts @ t[:3, :3].T + t[:3, 3]

        b = self._fixed_bounds
        inside = np.all((pts >= b[0] - 1e-6) & (pts <= b[1] + 1e-6), axis=1)
        n_in = int(np.count_nonzero(inside))
        if n_in == 0:
            return empty

        d, _ = self._tree.query(pts[inside], k=1, workers=-1)
        return {
            "mean_mm": float(np.mean(d)),
            "median_mm": float(np.median(d)),
            "rms_mm": float(np.sqrt(np.mean(d ** 2))),
            "n_overlap": n_in,
            "coverage": float(n_in / len(pts)),
        }

    @staticmethod
    def format(result: dict) -> str:
        if not result or result.get("n_overlap", 0) == 0:
            return "重なりなし — まだ 2 つが離れています"
        return (f"重なり誤差 平均 {result['mean_mm']:.2f} mm / "
                f"RMS {result['rms_mm']:.2f} mm "
                f"(重複点 {result['n_overlap']:,} / 被覆 {result['coverage']*100:.0f}%)")


def dice_score(fixed, moving, threshold: float,
               transform: np.ndarray | None = None,
               max_dim: int = 128,
               progress_cb=None, cancel_cb=None) -> dict:
    """重複領域におけるボクセル Dice 係数 (厳密・オンデマンド)。

    粗いグリッド (既定 128^3 以下) にリサンプルして計算するため数秒で済む。
    """
    from app.core.resample import (_make_reslicer, _plain_image, _ones_image,
                                   _reslice_matrix, _slab_to_numpy)

    fb = fixed.world_bounds()
    mb = moving.world_bounds(transform)
    lo = np.maximum(fb[0], mb[0])
    hi = np.minimum(fb[1], mb[1])
    if np.any(hi <= lo):
        return {"dice": 0.0, "overlap_voxels": 0, "note": "重なりがありません"}

    extent = hi - lo
    spacing = np.maximum(extent / float(max_dim), 1e-3)
    dims = np.maximum(np.floor(extent / spacing).astype(int) + 1, 1)
    grid = {"origin": tuple(lo), "spacing": tuple(spacing),
            "dims": (int(dims[0]), int(dims[1]), int(dims[2]))}

    if progress_cb:
        progress_cb(20, "Dice 係数を計算中…")

    r_f = _make_reslicer(_plain_image(fixed), _reslice_matrix(fixed, None),
                         grid, "linear", config.AIR_HU)
    r_fm = _make_reslicer(_ones_image(fixed), _reslice_matrix(fixed, None),
                          grid, "linear", 0)
    r_m = _make_reslicer(_plain_image(moving), _reslice_matrix(moving, transform),
                         grid, "linear", config.AIR_HU)
    r_mm = _make_reslicer(_ones_image(moving), _reslice_matrix(moving, transform),
                          grid, "linear", 0)

    nz = grid["dims"][2]
    inter = a_only = b_only = 0
    for z in range(nz):
        if cancel_cb and cancel_cb():
            return {"dice": float("nan"), "overlap_voxels": 0, "note": "中断されました"}
        av = _slab_to_numpy(r_f, grid, z, z, np.int16)
        am = _slab_to_numpy(r_fm, grid, z, z, np.uint8) >= 250
        bv = _slab_to_numpy(r_m, grid, z, z, np.int16)
        bm = _slab_to_numpy(r_mm, grid, z, z, np.uint8) >= 250
        a = am & (av >= threshold)
        b = bm & (bv >= threshold)
        inter += int(np.count_nonzero(a & b))
        a_only += int(np.count_nonzero(a))
        b_only += int(np.count_nonzero(b))
        if progress_cb and z % 16 == 0:
            progress_cb(20 + int(z / max(nz, 1) * 75), "Dice 係数を計算中…")

    denom = a_only + b_only
    dice = (2.0 * inter / denom) if denom else 0.0
    return {"dice": dice, "overlap_voxels": inter,
            "note": f"Dice = {dice:.4f} (重複ボクセル {inter:,})"}
