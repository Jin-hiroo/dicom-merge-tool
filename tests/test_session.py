"""セッション保存・復元の検証。

肝心なのは「アプリを閉じても作業が失われない」こと。特に結合結果は
位置合わせをやり直さないと再現できないので、実体が確実に残ること。
"""
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

from app.core import blend, resample, session
from app.core.transform import RigidTransform


@dataclass
class FakeEntry:
    """main_window の SeriesEntry と同じ形の最小オブジェクト。"""
    title: str
    role: str = ""
    merged: bool = False
    volume: object = None
    meta: dict = field(default_factory=dict)
    dicom_ref: dict | None = None


@pytest.fixture(scope="module")
def merged_vol(volumes, phantom, scratch):
    c = phantom["correction"]
    m = RigidTransform(tx=c[0], ty=c[1], tz=c[2]).matrix()
    vol, _ = resample.merge_volumes(volumes[0], volumes[1], m,
                                    mode=blend.MODE_FEATHER, scratch_dir=scratch)
    return vol


@pytest.fixture
def entries(volumes, merged_vol, phantom):
    return [
        FakeEntry(title="PHANTOM A", role="", merged=False, volume=volumes[0],
                  dicom_ref={"folder": str(phantom["folder_a"]),
                             "series_uid": volumes[0].meta["series_uid"]}),
        FakeEntry(title="結合結果 #1", role="fixed", merged=True,
                  volume=merged_vol, meta={"source_count": 2}),
    ]


SETTINGS = {"segment": {"threshold": 321}, "merge": {"blend": "max"}}


# ----------------------------------------------------------------------
def test_save_creates_manifest(entries, tmp_path):
    out = tmp_path / "sess"
    info = session.save_session(out, entries, SETTINGS)
    assert session.is_session(out)
    assert info["entries"] == 2 and info["volumes"] == 1


def test_self_contained_save_copies_volume(entries, tmp_path):
    """明示保存はボリューム実体をコピーし、scratch を消しても壊れないこと。"""
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS, copy_volumes=True)
    copied = list((out / session.VOLUME_SUBDIR).glob("*.raw"))
    assert len(copied) == 1
    assert copied[0].stat().st_size == entries[1].volume.nbytes


def test_autosave_does_not_copy(entries, tmp_path):
    """自動保存は参照のみ (終了時に一瞬で終わる必要がある)。"""
    out = tmp_path / "auto"
    session.save_session(out, entries, SETTINGS, copy_volumes=False)
    assert not (out / session.VOLUME_SUBDIR).exists()
    data = session.load_session(out)
    assert [e["kind"] for e in data["entries"]] == ["dicom", "volume"]


def test_roundtrip_restores_merged_volume_exactly(entries, merged_vol, tmp_path):
    """★ 結合結果が 1 ボクセルも変わらず戻ること。"""
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS, copy_volumes=True)
    data = session.load_session(out)

    vol_rec = [e for e in data["entries"] if e["kind"] == "volume"][0]
    back = vol_rec["volume"]
    assert back.shape_zyx == merged_vol.shape_zyx
    assert np.allclose(back.spacing, merged_vol.spacing)
    assert np.allclose(back.origin, merged_vol.origin)
    assert np.allclose(back.direction, merged_vol.direction)
    assert np.array_equal(np.asarray(back.array), np.asarray(merged_vol.array))


def test_roundtrip_restores_settings_and_roles(entries, tmp_path):
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS, copy_volumes=True)
    data = session.load_session(out)
    assert data["settings"]["segment"]["threshold"] == 321
    assert data["settings"]["merge"]["blend"] == "max"
    roles = {e["title"]: e["role"] for e in data["entries"]}
    assert roles["結合結果 #1"] == "fixed"


def test_dicom_entry_is_reference_only(entries, tmp_path, phantom):
    """元シリーズは実体を複製せずパス参照であること。"""
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS, copy_volumes=True)
    data = session.load_session(out)
    rec = [e for e in data["entries"] if e["kind"] == "dicom"][0]
    assert rec["dicom"]["folder"] == str(phantom["folder_a"])
    assert rec["was_loaded"] is True
    # コピーされたのは結合結果 1 件だけ
    assert len(list((out / session.VOLUME_SUBDIR).glob("*.raw"))) == 1


def test_missing_volume_is_reported_not_fatal(entries, tmp_path):
    """実体が消えていても、残りは復元できること。"""
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS, copy_volumes=True)
    for p in (out / session.VOLUME_SUBDIR).glob("*.raw"):
        p.unlink()
    data = session.load_session(out)
    assert len(data["problems"]) == 1
    assert [e["kind"] for e in data["entries"]] == ["dicom"]


def test_truncated_volume_is_detected(entries, tmp_path):
    """サイズが合わないファイルを黙って読まないこと。"""
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS, copy_volumes=True)
    raw = list((out / session.VOLUME_SUBDIR).glob("*.raw"))[0]
    with open(raw, "r+b") as f:
        f.truncate(raw.stat().st_size - 1024)
    data = session.load_session(out)
    assert any("サイズが合いません" in p for p in data["problems"])


def test_peek_without_loading(entries, tmp_path):
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS, copy_volumes=True)
    info = session.peek(out)
    assert info["entries"] == 2 and info["volumes"] == 1
    assert "結合結果 #1" in info["titles"]
    assert session.peek(tmp_path / "nope") is None


def test_future_version_is_rejected(entries, tmp_path):
    import json
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS)
    p = session.manifest_path(out)
    d = json.loads(p.read_text(encoding="utf-8"))
    d["version"] = session.VERSION + 1
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="新しい形式"):
        session.load_session(out)


def test_cleanup_keeps_referenced_scratch(tmp_path):
    """★ 使用中の実体を消さないこと (消すと復元できなくなる)。

    共有フィクスチャを汚さないよう、ここでは path だけ持つスタブを使う。
    """
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    orphan = scratch / "orphan.raw"
    orphan.write_bytes(b"\0" * 64)
    live = scratch / "in_use.raw"
    live.write_bytes(b"\0" * 64)

    class StubVolume:
        def __init__(self, path):
            self.path = path

    used = [FakeEntry(title="使用中", merged=True, volume=StubVolume(live))]
    n = session.cleanup_unused_scratch(used, scratch)
    assert n == 1
    assert not orphan.exists()
    assert live.exists(), "使用中のファイルを消してはいけない"


def test_meta_with_numpy_is_serializable(entries, tmp_path):
    """meta に numpy 型が混ざっていても保存できること。"""
    entries[1].meta = {"overlap_mm": np.float32(40.0),
                       "count": np.int64(3),
                       "bounds": np.zeros((2, 3))}
    out = tmp_path / "sess"
    session.save_session(out, entries, SETTINGS)
    data = session.load_session(out)
    meta = [e for e in data["entries"] if e["kind"] == "volume"][0]["meta"]
    assert meta["overlap_mm"] == 40.0 and meta["count"] == 3
