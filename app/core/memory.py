"""メモリ予算の見積もりとガード。

16GB ユニファイドメモリでスワップを起こさないために、重い処理の *前* に
必要バイト数を計算し、超過するなら解像度を落とす。
"""
from __future__ import annotations

import math

import numpy as np

from app import config


def volume_bytes(shape_zyx, itemsize: int = 2) -> int:
    n = 1
    for s in shape_zyx:
        n *= int(s)
    return n * int(itemsize)


def human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024 or unit == "TB":
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024.0
    return f"{nbytes:.1f} TB"


def binning_for_budget(shape_zyx, itemsize: int, target_bytes: int) -> int:
    """等方ビニング係数。予算内に収まる最小の整数を返す (1 以上)。"""
    current = volume_bytes(shape_zyx, itemsize)
    if current <= target_bytes:
        return 1
    factor = math.ceil((current / target_bytes) ** (1.0 / 3.0))
    return max(1, int(factor))


def grid_dims(bounds: np.ndarray, spacing) -> tuple:
    """bounds([[min],[max]]) と spacing から出力グリッドの (nx, ny, nz)。"""
    extent = np.asarray(bounds[1], float) - np.asarray(bounds[0], float)
    dims = np.floor(extent / np.asarray(spacing, float)).astype(int) + 1
    return tuple(int(max(1, d)) for d in dims)


def fit_spacing_to_budget(bounds: np.ndarray, spacing,
                          budget_bytes: int = config.MAX_VOLUME_BYTES,
                          itemsize: int = 2,
                          max_iter: int = 64) -> tuple[tuple, tuple, bool, float]:
    """出力グリッドが予算に収まるまで spacing を一様に粗くする。

    Returns
    -------
    (spacing, dims, was_adjusted, scale)
        spacing : 調整後の (sx, sy, sz)
        dims    : (nx, ny, nz)
        was_adjusted : 粗くしたなら True
        scale   : 元 spacing に対する倍率
    """
    spacing = np.asarray(spacing, float)
    scale = 1.0
    for _ in range(max_iter):
        s = spacing * scale
        dims = grid_dims(bounds, s)
        # 出力ボリューム本体 + リサンプル中間 + マスクぶんの余裕を見て 1.6 倍で評価
        need = volume_bytes(dims[::-1], itemsize) * 1.6
        if need <= budget_bytes:
            return tuple(float(v) for v in s), dims, scale > 1.0, scale
        scale *= 1.15
    s = spacing * scale
    return tuple(float(v) for v in s), grid_dims(bounds, s), True, scale


def check_total(volumes, budget: int = config.MAX_TOTAL_VOLUME_BYTES) -> tuple[bool, int]:
    """常駐ボリューム合計が予算内か。 (ok, total_bytes)"""
    total = sum(int(getattr(v, "nbytes", 0)) for v in volumes)
    return total <= budget, total
