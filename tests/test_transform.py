"""剛体変換の検証。"""
import numpy as np
import pytest

from app.core.transform import RigidTransform, UndoStack, initial_placement


def test_identity():
    assert np.allclose(RigidTransform().matrix(), np.eye(4))


def test_translation():
    m = RigidTransform(tx=3.0, ty=-2.0, tz=7.5).matrix()
    p = np.array([1.0, 1.0, 1.0, 1.0])
    assert np.allclose((m @ p)[:3], [4.0, -1.0, 8.5])


def test_rotation_is_about_center():
    """中心まわりに回ること = 中心点が動かないこと。"""
    center = (10.0, 20.0, 30.0)
    t = RigidTransform(rz=90.0, center=center)
    m = t.matrix()
    p = np.array([*center, 1.0])
    assert np.allclose((m @ p)[:3], center, atol=1e-9)


def test_rotation_90_degrees():
    t = RigidTransform(rz=90.0, center=(0.0, 0.0, 0.0))
    out = (t.matrix() @ np.array([1.0, 0.0, 0.0, 1.0]))[:3]
    assert np.allclose(out, [0.0, 1.0, 0.0], atol=1e-9)


def test_inverse_round_trip():
    t = RigidTransform(tx=5, ty=-3, tz=2, rx=10, ry=-20, rz=35,
                       center=(1.0, 2.0, 3.0))
    assert np.allclose(t.matrix() @ t.inverse_matrix(), np.eye(4), atol=1e-9)


def test_rotation_stays_orthonormal():
    t = RigidTransform(rx=33, ry=-77, rz=125)
    r = t.matrix()[:3, :3]
    assert np.allclose(r @ r.T, np.eye(3), atol=1e-9)
    assert np.isclose(np.linalg.det(r), 1.0)


def test_undo_redo():
    s = UndoStack()
    s.push({"tx": 0.0})
    s.push({"tx": 1.0})
    assert s.undo({"tx": 2.0}) == {"tx": 1.0}
    assert s.undo({"tx": 1.0}) == {"tx": 0.0}
    assert s.undo({"tx": 0.0}) is None
    assert s.redo({"tx": 0.0}) == {"tx": 1.0}


def test_initial_placement_centroid(volumes):
    va, vb = volumes
    m = initial_placement(va, vb, "centroid")
    assert np.allclose(va.center_world(), vb.center_world(m), atol=1e-6)


def test_inv44_matches_numpy():
    """解析的な逆行列が np.linalg.inv と一致すること。"""
    from app.core.transform import inv44
    rng = np.random.default_rng(0)
    for _ in range(20):
        m = np.eye(4)
        m[:3, :3] = rng.normal(size=(3, 3))
        m[:3, 3] = rng.normal(size=3)
        if abs(np.linalg.det(m[:3, :3])) < 1e-6:
            continue
        assert np.allclose(inv44(m), np.linalg.inv(m), atol=1e-9)


def test_inv44_avoids_lapack_in_thread():
    """★ QThread 内で落ちないこと (SIGBUS 再発防止の回帰テスト)。

    np.linalg.inv は 4x4 でも OpenBLAS の並列 LAPACK を通り、QThread の
    既定スタックを溢れさせて SIGBUS になる。inv44 は LAPACK を通らない。
    """
    import threading
    from app.core.transform import inv44
    out = {}

    def job():
        m = np.eye(4)
        m[:3, 3] = [1.0, 2.0, 3.0]
        out["r"] = inv44(m)

    t = threading.Thread(target=job)
    t.start()
    t.join()
    assert np.allclose(out["r"][:3, 3], [-1.0, -2.0, -3.0])


def test_inv44_rejects_singular():
    from app.core.transform import inv44
    m = np.eye(4)
    m[:3, :3] = 0.0
    with pytest.raises(np.linalg.LinAlgError):
        inv44(m)
