"""重複領域のブレンド。

2 つの CT は重なって撮られているため、重複部では両方に値がある。そこを
どう混ぜるかで継ぎ目の見え方が決まる。
"""
from __future__ import annotations

import numpy as np

from app import config

MODE_FEATHER = "feather"
MODE_MAX = "max"
MODE_MEAN = "mean"


def normalize_mode(label: str) -> str:
    """UI のラベル ("feather (推奨)" 等) を内部モード名へ。"""
    low = str(label).lower()
    if low.startswith("max"):
        return MODE_MAX
    if low.startswith("mean"):
        return MODE_MEAN
    return MODE_FEATHER


def seam_axis(bounds_a: np.ndarray, bounds_b: np.ndarray) -> tuple[int, float, float, bool]:
    """継ぎ目の軸と重複区間を求める。

    2 つの AABB が「ずれている」軸 = 重複が相対的に最も薄い軸 を継ぎ目とみなす。
    通常は撮影方向 (Z) になる。

    Returns
    -------
    (axis, lo, hi, a_first)
        axis    : 0=x, 1=y, 2=z
        lo, hi  : その軸の重複区間 (mm)
        a_first : 座標が小さい側を A が占めているなら True
    """
    lo = np.maximum(bounds_a[0], bounds_b[0])
    hi = np.minimum(bounds_a[1], bounds_b[1])
    overlap = np.clip(hi - lo, 0.0, None)

    ext_a = bounds_a[1] - bounds_a[0]
    ext_b = bounds_b[1] - bounds_b[0]
    denom = np.minimum(ext_a, ext_b)
    denom[denom <= 0] = 1.0
    ratio = overlap / denom
    # 重複していない軸は継ぎ目になりえないので除外する
    ratio[overlap <= 0] = np.inf

    axis = int(np.argmin(ratio))
    if not np.isfinite(ratio[axis]):
        axis = 2  # 重なりがない: Z を既定にして単純連結する
    a_first = bool(bounds_a[0][axis] <= bounds_b[0][axis])
    return axis, float(lo[axis]), float(hi[axis]), a_first


def feather_weights(coords: np.ndarray, lo: float, hi: float, a_first: bool) -> np.ndarray:
    """重複区間にわたって 0→1 の線形 ramp を返す (B の重み)。

    区間の外では 0 または 1 に飽和する。1 次元なので全ボリュームの
    距離変換に比べ桁違いに安い。
    """
    span = hi - lo
    if span <= 1e-9:
        return np.where(coords >= hi, 1.0, 0.0).astype(np.float32)
    w = (np.asarray(coords, np.float32) - lo) / span
    w = np.clip(w, 0.0, 1.0)
    return w if a_first else (1.0 - w)


def blend_slab(a_vals: np.ndarray, a_mask: np.ndarray,
               b_vals: np.ndarray, b_mask: np.ndarray,
               mode: str = MODE_FEATHER,
               weights_b: np.ndarray | None = None) -> np.ndarray:
    """1 スラブ分をブレンドして int16 で返す。

    片方にしか値がない領域はその値をそのまま採用し、両方にある領域だけ
    mode に従って混ぜる。どちらにも無い領域は AIR。
    """
    both = a_mask & b_mask
    only_a = a_mask & ~b_mask
    only_b = b_mask & ~a_mask

    out = np.full(a_vals.shape, config.AIR_HU, dtype=np.int16)
    out[only_a] = a_vals[only_a]
    out[only_b] = b_vals[only_b]

    if np.any(both):
        av = a_vals[both].astype(np.float32)
        bv = b_vals[both].astype(np.float32)
        if mode == MODE_MAX:
            mixed = np.maximum(av, bv)
        elif mode == MODE_MEAN:
            mixed = 0.5 * (av + bv)
        else:
            if weights_b is None:
                mixed = 0.5 * (av + bv)
            else:
                w = np.asarray(weights_b, np.float32)[both]
                mixed = av * (1.0 - w) + bv * w
        out[both] = np.clip(mixed, config.HU_MIN, config.HU_MAX).astype(np.int16)

    return out
