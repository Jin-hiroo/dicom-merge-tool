"""手動位置合わせのための剛体変換。

平行移動 (tx,ty,tz mm) と回転 (rx,ry,rz 度) を保持する。回転は必ず
``center`` まわりで行う — 原点まわりに回すとモデルが画面外へ吹き飛ぶため。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np

AXES = ("x", "y", "z")
ROT_AXES = ("rx", "ry", "rz")
AXIS_LABELS = {"rx": "Roll (X)", "ry": "Pitch (Y)", "rz": "Yaw (Z)"}


def inv44(matrix) -> np.ndarray:
    """4x4 アフィン行列の解析的な逆行列。

    np.linalg.inv を使わないのには理由がある: numpy は 4x4 でも OpenBLAS の
    dgetrf_parallel を通り、そこで大きなスタックフレームを要求する。QThread の
    既定スタックでは溢れて SIGBUS になるため、ワーカー内で呼ぶと落ちる。
    ここでは 3x3 の余因子展開で閉形式に解くので LAPACK を一切通らない。
    """
    m = np.asarray(matrix, float)
    a = m[:3, :3]
    t = m[:3, 3]

    det = (a[0, 0] * (a[1, 1] * a[2, 2] - a[1, 2] * a[2, 1])
           - a[0, 1] * (a[1, 0] * a[2, 2] - a[1, 2] * a[2, 0])
           + a[0, 2] * (a[1, 0] * a[2, 1] - a[1, 1] * a[2, 0]))
    if abs(det) < 1e-12:
        raise np.linalg.LinAlgError("特異行列のため逆行列を計算できません")

    inv = np.empty((3, 3))
    inv[0, 0] = (a[1, 1] * a[2, 2] - a[1, 2] * a[2, 1]) / det
    inv[0, 1] = (a[0, 2] * a[2, 1] - a[0, 1] * a[2, 2]) / det
    inv[0, 2] = (a[0, 1] * a[1, 2] - a[0, 2] * a[1, 1]) / det
    inv[1, 0] = (a[1, 2] * a[2, 0] - a[1, 0] * a[2, 2]) / det
    inv[1, 1] = (a[0, 0] * a[2, 2] - a[0, 2] * a[2, 0]) / det
    inv[1, 2] = (a[0, 2] * a[1, 0] - a[0, 0] * a[1, 2]) / det
    inv[2, 0] = (a[1, 0] * a[2, 1] - a[1, 1] * a[2, 0]) / det
    inv[2, 1] = (a[0, 1] * a[2, 0] - a[0, 0] * a[2, 1]) / det
    inv[2, 2] = (a[0, 0] * a[1, 1] - a[0, 1] * a[1, 0]) / det

    out = np.eye(4)
    out[:3, :3] = inv
    out[:3, 3] = -inv @ t
    return out


def _rot(axis: int, deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    m = np.eye(3)
    if axis == 0:
        m[1:, 1:] = [[c, -s], [s, c]]
    elif axis == 1:
        m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    else:
        m[:2, :2] = [[c, -s], [s, c]]
    return m


@dataclass
class RigidTransform:
    tx: float = 0.0
    ty: float = 0.0
    tz: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    center: tuple = (0.0, 0.0, 0.0)
    base: np.ndarray = field(default_factory=lambda: np.eye(4))

    # ------------------------------------------------------------------
    def rotation(self) -> np.ndarray:
        return _rot(2, self.rz) @ _rot(1, self.ry) @ _rot(0, self.rx)

    def matrix(self) -> np.ndarray:
        """Moving を世界座標で動かす 4x4。base(初期配置) の上に乗る。"""
        c = np.asarray(self.center, float)
        r = self.rotation()
        m = np.eye(4)
        m[:3, :3] = r
        m[:3, 3] = c - r @ c + np.array([self.tx, self.ty, self.tz], float)
        return m @ np.asarray(self.base, float)

    def inverse_matrix(self) -> np.ndarray:
        return inv44(self.matrix())

    # ------------------------------------------------------------------
    def translate(self, axis: str, delta: float) -> None:
        setattr(self, f"t{axis}", getattr(self, f"t{axis}") + float(delta))

    def rotate(self, axis: str, delta: float) -> None:
        cur = getattr(self, f"r{axis}") + float(delta)
        setattr(self, f"r{axis}", (cur + 180.0) % 360.0 - 180.0)

    def reset(self) -> None:
        self.tx = self.ty = self.tz = 0.0
        self.rx = self.ry = self.rz = 0.0

    def values(self) -> dict:
        return {"tx": self.tx, "ty": self.ty, "tz": self.tz,
                "rx": self.rx, "ry": self.ry, "rz": self.rz}

    def set_values(self, **kw) -> None:
        for k, v in kw.items():
            if hasattr(self, k):
                setattr(self, k, float(v))

    def describe(self) -> str:
        return (f"移動 ({self.tx:+.2f}, {self.ty:+.2f}, {self.tz:+.2f}) mm   "
                f"回転 ({self.rx:+.2f}, {self.ry:+.2f}, {self.rz:+.2f})°")

    def clone(self) -> "RigidTransform":
        return copy.deepcopy(self)

    def to_vtk_transform(self):
        import vtk
        m = self.matrix()
        mat = vtk.vtkMatrix4x4()
        for r in range(4):
            for c in range(4):
                mat.SetElement(r, c, float(m[r, c]))
        t = vtk.vtkTransform()
        t.SetMatrix(mat)
        return t


class UndoStack:
    """位置合わせ操作の Undo/Redo。手探りの調整では必須。"""

    def __init__(self, limit: int = 200):
        self._undo: list = []
        self._redo: list = []
        self._limit = limit

    def push(self, state: dict) -> None:
        self._undo.append(dict(state))
        if len(self._undo) > self._limit:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self, current: dict) -> dict | None:
        if not self._undo:
            return None
        self._redo.append(dict(current))
        return self._undo.pop()

    def redo(self, current: dict) -> dict | None:
        if not self._redo:
            return None
        self._undo.append(dict(current))
        return self._redo.pop()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)


def initial_placement(fixed, moving, mode: str = "dicom") -> np.ndarray:
    """Moving の初期配置 4x4 を返す。

    mode
        "dicom"    : DICOM 患者座標のまま (恒等)
        "centroid" : 両者のバウンディングボックス中心を一致させる
        "stack_z"  : Z 方向にバウンディングボックスを積む
    """
    if mode == "centroid":
        d = fixed.center_world() - moving.center_world()
        m = np.eye(4)
        m[:3, 3] = d
        return m
    if mode == "stack_z":
        fb, mb = fixed.world_bounds(), moving.world_bounds()
        d = np.zeros(3)
        d[:2] = fb.mean(axis=0)[:2] - mb.mean(axis=0)[:2]
        d[2] = fb[1, 2] - mb[0, 2]
        m = np.eye(4)
        m[:3, 3] = d
        return m
    return np.eye(4)
