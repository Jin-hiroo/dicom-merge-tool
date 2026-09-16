"""STL 書出し。ASCII はサイズが 5 倍以上になるため必ずバイナリで書く。"""
from __future__ import annotations

from pathlib import Path

import vtk

from app import config
from app.core.meshing import export_surface, polydata_stats
from app.core.volume import Volume


def write_stl(poly, path, binary: bool = True) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = vtk.vtkSTLWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(poly)
    if binary:
        writer.SetFileTypeToBinary()
    else:
        writer.SetFileTypeToASCII()
    writer.Write()

    stats = polydata_stats(poly)
    stats["path"] = str(path)
    stats["file_bytes"] = path.stat().st_size if path.exists() else 0
    return stats


def export_volume_to_stl(volume: Volume, path, threshold: float,
                         target_triangles: int = config.EXPORT_TARGET_TRIANGLES,
                         smooth_iterations: int = config.EXPORT_SMOOTH_ITERATIONS,
                         fill_holes: bool = False,
                         high_quality: bool = False,
                         progress_cb=None, cancel_cb=None) -> dict | None:
    """フル解像度で 1 回だけメッシュ化して STL へ書き出す。"""
    poly = export_surface(volume, threshold,
                          target_triangles=target_triangles,
                          smooth_iterations=smooth_iterations,
                          fill_holes=fill_holes,
                          high_quality=high_quality,
                          progress_cb=lambda p, m: progress_cb(int(p * 0.9), m)
                          if progress_cb else None,
                          cancel_cb=cancel_cb)
    if cancel_cb and cancel_cb():
        return None
    if poly.GetNumberOfPolys() == 0:
        raise ValueError("閾値に該当する面がありません。HU 閾値を下げてください。")

    if progress_cb:
        progress_cb(92, "STL を書き出し中…")
    stats = write_stl(poly, path, binary=True)
    if progress_cb:
        progress_cb(100, "書き出しが完了しました")
    return stats
