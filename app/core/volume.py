"""CT ボリュームのデータモデル。

配列は常に (nz, ny, nx) の C 連続・int16(HU)。実体は scratch 上の
numpy.memmap なので、RAM に全展開せずに扱える。

世界座標は DICOM 患者座標系 (LPS, mm)。
    patient = direction @ diag(spacing) @ (i, j, k) + origin
ここで i が x 方向(列方向/最速変化), j が y 方向(行方向), k が z 方向(スライス)。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app import config


@dataclass
class Volume:
    array: np.ndarray            # (nz, ny, nx) int16, HU
    spacing: tuple               # (sx, sy, sz) mm
    origin: tuple                # ボクセル(0,0,0)中心の患者座標 (mm)
    direction: np.ndarray        # 3x3, 各列が i/j/k 軸の単位方向ベクトル
    name: str = "volume"
    meta: dict = field(default_factory=dict)
    path: Path | None = None     # memmap の実体 (あれば)

    # ------------------------------------------------------------------
    @property
    def shape_zyx(self) -> tuple:
        return tuple(int(v) for v in self.array.shape)

    @property
    def shape_xyz(self) -> tuple:
        nz, ny, nx = self.array.shape
        return (int(nx), int(ny), int(nz))

    @property
    def nbytes(self) -> int:
        return int(self.array.size * self.array.dtype.itemsize)

    @property
    def affine(self) -> np.ndarray:
        """(i,j,k,1) -> (x,y,z,1) の 4x4 アフィン。"""
        m = np.eye(4)
        m[:3, :3] = np.asarray(self.direction, float) @ np.diag(self.spacing)
        m[:3, 3] = self.origin
        return m

    def index_to_world(self, ijk: np.ndarray) -> np.ndarray:
        ijk = np.atleast_2d(np.asarray(ijk, float))
        return ijk @ (np.asarray(self.direction, float) @ np.diag(self.spacing)).T + np.asarray(self.origin, float)

    def corners_world(self) -> np.ndarray:
        """ボリューム 8 隅の患者座標 (8,3)。"""
        nx, ny, nz = self.shape_xyz
        c = np.array([[i, j, k]
                      for i in (0, nx - 1)
                      for j in (0, ny - 1)
                      for k in (0, nz - 1)], float)
        return self.index_to_world(c)

    def world_bounds(self, transform: np.ndarray | None = None) -> np.ndarray:
        """軸平行バウンディングボックス [[xmin,ymin,zmin],[xmax,ymax,zmax]]。

        transform を渡すとその 4x4 剛体変換を適用した後の範囲を返す。
        """
        pts = self.corners_world()
        if transform is not None:
            t = np.asarray(transform, float)
            pts = pts @ t[:3, :3].T + t[:3, 3]
        return np.stack([pts.min(axis=0), pts.max(axis=0)])

    def center_world(self, transform: np.ndarray | None = None) -> np.ndarray:
        b = self.world_bounds(transform)
        return b.mean(axis=0)

    # ------------------------------------------------------------------
    def is_axis_aligned(self, tol: float = 1e-6) -> bool:
        return bool(np.allclose(np.asarray(self.direction, float), np.eye(3), atol=tol))

    def preview_binning(self, target_bytes: int = config.PREVIEW_VOLUME_BYTES) -> int:
        """プレビュー用の等方ビニング係数を返す (1 以上の整数)。"""
        from app.core.memory import binning_for_budget
        return binning_for_budget(self.shape_zyx, self.array.dtype.itemsize, target_bytes)

    def downsample(self, factor: int, name_suffix: str = " (preview)") -> "Volume":
        """factor でビニング(平均)したボリュームを新規に返す。

        平均を取ることで単純な間引きより表面が滑らかになる。factor==1 なら自身を返す。
        """
        if factor <= 1:
            return self
        nz, ny, nx = self.array.shape
        cz, cy, cx = nz // factor, ny // factor, nx // factor
        cz, cy, cx = max(cz, 1), max(cy, 1), max(cx, 1)

        out = np.empty((cz, cy, cx), dtype=np.int16)
        # メモリを抑えるため z スラブごとに平均する
        for z in range(cz):
            sl = self.array[z * factor:(z + 1) * factor,
                            :cy * factor, :cx * factor].astype(np.float32)
            sl = sl.reshape(sl.shape[0], cy, factor, cx, factor).mean(axis=(0, 2, 4))
            out[z] = np.clip(sl, config.HU_MIN, config.HU_MAX).astype(np.int16)

        new_spacing = tuple(float(s) * factor for s in self.spacing)
        # ビニング後のボクセル中心は元の factor 個のボクセル中心の平均位置になる
        shift = (factor - 1) / 2.0
        new_origin = tuple(self.index_to_world(np.array([[shift, shift, shift]]))[0])
        return Volume(array=out, spacing=new_spacing, origin=new_origin,
                      direction=np.array(self.direction, float).copy(),
                      name=self.name + name_suffix, meta=dict(self.meta))

    # ------------------------------------------------------------------
    def to_vtk(self):
        """メモリを共有する vtkImageData を返す。

        返り値には ``_numpy_ref`` で元配列への参照を持たせ、GC による
        解放でダングリングポインタにならないようにしている。
        """
        import vtk
        from vtkmodules.util import numpy_support

        arr = np.ascontiguousarray(self.array)
        img = vtk.vtkImageData()
        nx, ny, nz = self.shape_xyz
        img.SetDimensions(nx, ny, nz)
        img.SetSpacing(*[float(s) for s in self.spacing])
        img.SetOrigin(*[float(o) for o in self.origin])

        d = np.asarray(self.direction, float)
        dm = vtk.vtkMatrix3x3()
        for r in range(3):
            for c in range(3):
                dm.SetElement(r, c, float(d[r, c]))
        img.SetDirectionMatrix(dm)

        vtk_arr = numpy_support.numpy_to_vtk(arr.ravel(order="C"),
                                             deep=0, array_type=vtk.VTK_SHORT)
        vtk_arr.SetName("HU")
        img.GetPointData().SetScalars(vtk_arr)

        img._numpy_ref = arr        # noqa: SLF001 - GC 回避のため意図的
        img._vtk_arr_ref = vtk_arr  # noqa: SLF001
        return img


# ----------------------------------------------------------------------
def new_memmap(shape_zyx: tuple, scratch_dir: Path | None = None,
               prefix: str = "vol", dtype=np.int16) -> tuple[np.memmap, Path]:
    """scratch 上に memmap を新規作成して (配列, パス) を返す。"""
    scratch_dir = Path(scratch_dir or config.SCRATCH_DIR)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    path = scratch_dir / f"{prefix}_{uuid.uuid4().hex[:10]}.raw"
    arr = np.memmap(path, dtype=dtype, mode="w+", shape=tuple(int(s) for s in shape_zyx))
    return arr, path


def cleanup_scratch(scratch_dir: Path | None = None) -> int:
    """scratch 内の memmap ファイルを削除し、削除件数を返す。"""
    scratch_dir = Path(scratch_dir or config.SCRATCH_DIR)
    n = 0
    if scratch_dir.exists():
        for p in scratch_dir.glob("*.raw"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n
