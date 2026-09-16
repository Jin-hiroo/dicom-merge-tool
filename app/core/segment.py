"""HU 閾値によるセグメンテーション。

通常は閾値を等値面値として FlyingEdges に直接渡すのが最も安価なので、
マスク配列は「最大連結成分」や「小島除去」が要求された時だけ実体化する。
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from app import config
from app.core.volume import Volume, new_memmap


def needs_mask(largest_component: bool, min_island_voxels: int) -> bool:
    return bool(largest_component) or int(min_island_voxels) > 0


def clean_volume(volume: Volume, threshold: float,
                 largest_component: bool = False,
                 min_island_voxels: int = 0,
                 scratch_dir=None,
                 progress_cb=None, cancel_cb=None) -> Volume:
    """閾値以上の領域から不要な連結成分を除いた Volume を返す。

    除去した領域は AIR_HU で埋める。二値マスクではなく HU を残すことで、
    マーチングキューブの補間が効き階段状のギザギザを避けられる。
    """
    if not needs_mask(largest_component, min_island_voxels):
        return volume

    if progress_cb:
        progress_cb(5, "閾値マスクを作成中…")
    mask = volume.array >= threshold
    if cancel_cb and cancel_cb():
        return volume

    if progress_cb:
        progress_cb(25, "連結成分をラベリング中…")
    structure = ndimage.generate_binary_structure(3, 1)
    labels, n = ndimage.label(mask, structure=structure)
    if n == 0:
        return volume
    if cancel_cb and cancel_cb():
        return volume

    if progress_cb:
        progress_cb(55, f"{n} 個の成分を評価中…")
    counts = np.bincount(labels.ravel())
    counts[0] = 0

    if largest_component:
        keep_ids = {int(counts.argmax())}
    else:
        keep_ids = {i for i in range(1, len(counts))
                    if counts[i] >= int(min_island_voxels)}
    if not keep_ids:
        keep_ids = {int(counts.argmax())}

    if progress_cb:
        progress_cb(75, "不要領域を除去中…")
    lut = np.zeros(len(counts), dtype=bool)
    for i in keep_ids:
        lut[i] = True
    keep = lut[labels]
    del labels, mask

    out, path = new_memmap(volume.shape_zyx, scratch_dir, prefix="seg")
    nz = volume.shape_zyx[0]
    for z in range(nz):
        if cancel_cb and cancel_cb():
            out.flush()
            del out
            from pathlib import Path
            Path(path).unlink(missing_ok=True)
            return volume
        out[z] = np.where(keep[z], volume.array[z], np.int16(config.AIR_HU))
        if progress_cb and z % 32 == 0:
            progress_cb(75 + int(z / max(nz, 1) * 25), f"不要領域を除去中… {z}/{nz}")
    out.flush()

    from pathlib import Path
    return Volume(array=out, spacing=volume.spacing, origin=volume.origin,
                  direction=np.array(volume.direction, float).copy(),
                  name=volume.name, meta=dict(volume.meta), path=Path(path))


def mask_stats(volume: Volume, threshold: float) -> dict:
    """閾値マスクの簡易統計 (UI 表示用)。"""
    count = 0
    nz = volume.shape_zyx[0]
    for z in range(nz):
        count += int(np.count_nonzero(volume.array[z] >= threshold))
    vox_mm3 = float(np.prod(volume.spacing))
    return {"voxels": count, "volume_ml": count * vox_mm3 / 1000.0}
