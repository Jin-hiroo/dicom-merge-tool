"""セッションの保存と復元。

アプリを閉じたりコードを更新したりしても、読み込んだシリーズ・位置合わせ・
結合結果が失われないようにする。

2 種類ある:

* **自動保存** — 終了時にマニフェストだけを書き、ボリューム実体は scratch に
  置いたままにする。一瞬で終わるので毎回自動で行える。次回起動時に復元を促す。
* **明示保存** — セッションフォルダにボリューム実体ごとコピーし、自己完結
  させる。scratch を掃除しても壊れない。

元の DICOM シリーズはフォルダパスと SeriesInstanceUID で参照するだけにする
(数 GB を複製しても仕方がないため)。結合結果は再計算に位置合わせのやり直しが
必要なので、必ず実体を保存する。
"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

import numpy as np

from app import config
from app.core.volume import Volume

FORMAT = "head3Dv1-session"
VERSION = 1
MANIFEST = "session.json"
VOLUME_SUBDIR = "volumes"

SESSIONS_DIR = config.PROJECT_ROOT / "sessions"
AUTOSAVE_DIR = SESSIONS_DIR / "_autosave"


# ----------------------------------------------------------------------
# ボリュームのジオメトリ <-> dict
# ----------------------------------------------------------------------
def volume_to_dict(volume: Volume, file_ref: str) -> dict:
    return {
        "file": file_ref,
        "dtype": str(np.dtype(volume.array.dtype)),
        "shape_zyx": [int(v) for v in volume.shape_zyx],
        "spacing": [float(v) for v in volume.spacing],
        "origin": [float(v) for v in volume.origin],
        "direction": np.asarray(volume.direction, float).tolist(),
        "name": volume.name,
        "meta": _jsonable(volume.meta),
    }


def volume_from_dict(d: dict, base_dir: Path) -> Volume:
    path = Path(d["file"])
    if not path.is_absolute():
        path = Path(base_dir) / path
    if not path.exists():
        raise FileNotFoundError(f"ボリューム実体が見つかりません: {path}")

    shape = tuple(int(v) for v in d["shape_zyx"])
    expected = int(np.prod(shape)) * np.dtype(d["dtype"]).itemsize
    actual = path.stat().st_size
    if actual != expected:
        raise ValueError(
            f"ボリューム実体のサイズが合いません ({path.name}): "
            f"{actual} バイト / 期待 {expected} バイト")

    arr = np.memmap(path, dtype=np.dtype(d["dtype"]), mode="r+", shape=shape)
    return Volume(
        array=arr,
        spacing=tuple(float(v) for v in d["spacing"]),
        origin=tuple(float(v) for v in d["origin"]),
        direction=np.array(d["direction"], float),
        name=d.get("name", "volume"),
        meta=dict(d.get("meta") or {}),
        path=path,
    )


def _jsonable(obj):
    """numpy 型が混ざっていても JSON にできる形へ落とす。"""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Path):
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


# ----------------------------------------------------------------------
# 保存
# ----------------------------------------------------------------------
def save_session(folder, entries, settings: dict,
                 copy_volumes: bool = True,
                 progress_cb=None, cancel_cb=None) -> dict | None:
    """セッションを folder に保存する。

    Parameters
    ----------
    entries
        (title, role, merged, volume, dicom_ref, meta) を持つオブジェクトの列。
        ``dicom_ref`` は {"folder":…, "series_uid":…} または None。
    copy_volumes
        True なら結合結果の実体をセッション内へコピーして自己完結させる。
        False (自動保存) なら scratch 上の実体を絶対パスで参照するだけ。
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    vol_dir = folder / VOLUME_SUBDIR
    if copy_volumes:
        vol_dir.mkdir(exist_ok=True)

    records = []
    to_copy = [e for e in entries if _needs_volume(e)] if copy_volumes else []
    done = 0

    for entry in entries:
        if cancel_cb and cancel_cb():
            return None
        rec = {
            "title": entry.title,
            "role": entry.role,
            "merged": bool(entry.merged),
            "meta": _jsonable(entry.meta),
        }

        if _needs_volume(entry):
            # 結合結果は再計算に位置合わせのやり直しが要るので実体を残す
            src = Path(entry.volume.path)
            if copy_volumes:
                if progress_cb:
                    progress_cb(int(done / max(len(to_copy), 1) * 90),
                                f"ボリュームを保存中… {entry.title}")
                dst = vol_dir / src.name
                if src.resolve() != dst.resolve():
                    shutil.copy2(src, dst)
                ref = f"{VOLUME_SUBDIR}/{dst.name}"
                done += 1
            else:
                ref = str(src.resolve())
            rec["kind"] = "volume"
            rec["volume"] = volume_to_dict(entry.volume, ref)
        elif entry.dicom_ref:
            # 元シリーズは参照だけ。数 GB を複製しても仕方がない
            rec["kind"] = "dicom"
            rec["dicom"] = dict(entry.dicom_ref)
            rec["was_loaded"] = entry.volume is not None
        else:
            continue
        records.append(rec)

    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "self_contained": bool(copy_volumes),
        "settings": _jsonable(settings),
        "entries": records,
    }

    if progress_cb:
        progress_cb(95, "マニフェストを書き出し中…")
    # 途中で落ちても壊れたマニフェストを残さないよう一時ファイル経由で置き換える
    tmp = folder / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(folder / MANIFEST)

    if progress_cb:
        progress_cb(100, "セッションを保存しました")
    return {
        "folder": str(folder),
        "entries": len(records),
        "volumes": sum(1 for r in records if r["kind"] == "volume"),
        "self_contained": bool(copy_volumes),
        "bytes": _folder_bytes(folder),
    }


def _needs_volume(entry) -> bool:
    return bool(entry.merged and entry.volume is not None
                and getattr(entry.volume, "path", None))


def _folder_bytes(folder: Path) -> int:
    return sum(p.stat().st_size for p in Path(folder).rglob("*") if p.is_file())


# ----------------------------------------------------------------------
# 読み込み
# ----------------------------------------------------------------------
def manifest_path(folder) -> Path:
    return Path(folder) / MANIFEST


def is_session(folder) -> bool:
    p = manifest_path(folder)
    if not p.exists():
        return False
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("format") == FORMAT
    except (json.JSONDecodeError, OSError):
        return False


def peek(folder) -> dict | None:
    """復元を促す前に概要だけ読む。"""
    if not is_session(folder):
        return None
    d = json.loads(manifest_path(folder).read_text(encoding="utf-8"))
    entries = d.get("entries", [])
    return {
        "folder": str(folder),
        "saved_at": d.get("saved_at", ""),
        "entries": len(entries),
        "volumes": sum(1 for e in entries if e.get("kind") == "volume"),
        "self_contained": bool(d.get("self_contained")),
        "titles": [e.get("title", "") for e in entries],
    }


def load_session(folder, progress_cb=None, cancel_cb=None) -> dict:
    """セッションを読み込む。

    復元できなかった項目は例外にせず ``problems`` に積んで返す。
    元 DICOM フォルダが移動・削除されていても、残りは復元できるようにするため。
    """
    folder = Path(folder)
    if not is_session(folder):
        raise ValueError(f"セッションではありません: {folder}")

    data = json.loads(manifest_path(folder).read_text(encoding="utf-8"))
    if int(data.get("version", 0)) > VERSION:
        raise ValueError(
            f"このセッションは新しい形式です (version {data.get('version')})。"
            f"アプリを更新してください。")

    records = data.get("entries", [])
    restored, problems = [], []

    for i, rec in enumerate(records):
        if cancel_cb and cancel_cb():
            return {"cancelled": True, "entries": [], "problems": problems,
                    "settings": data.get("settings", {})}
        if progress_cb:
            progress_cb(int(i / max(len(records), 1) * 100),
                        f"セッションを復元中… {rec.get('title', '')}")
        try:
            if rec.get("kind") == "volume":
                volume = volume_from_dict(rec["volume"], folder)
                restored.append({"kind": "volume", "title": rec.get("title", volume.name),
                                 "role": rec.get("role", ""), "merged": True,
                                 "volume": volume, "meta": rec.get("meta", {})})
            elif rec.get("kind") == "dicom":
                restored.append({"kind": "dicom", "title": rec.get("title", ""),
                                 "role": rec.get("role", ""), "merged": False,
                                 "dicom": rec.get("dicom", {}),
                                 "was_loaded": bool(rec.get("was_loaded")),
                                 "meta": rec.get("meta", {})})
        except (FileNotFoundError, ValueError, OSError) as exc:
            problems.append(f"{rec.get('title', '(無題)')}: {exc}")

    if progress_cb:
        progress_cb(100, "セッションを復元しました")
    return {
        "cancelled": False,
        "folder": str(folder),
        "saved_at": data.get("saved_at", ""),
        "settings": data.get("settings", {}),
        "entries": restored,
        "problems": problems,
    }


# ----------------------------------------------------------------------
def scratch_files_in_use(entries) -> set:
    """セッションが参照している scratch 上のファイル。

    これを除いたものだけ掃除すれば、復元に必要な実体を消さずに済む。
    """
    used = set()
    for e in entries:
        vol = getattr(e, "volume", None)
        p = getattr(vol, "path", None) if vol is not None else None
        if p:
            used.add(Path(p).resolve())
    return used


def cleanup_unused_scratch(entries, scratch_dir=None) -> int:
    """参照されていない scratch ファイルだけ削除する。"""
    scratch_dir = Path(scratch_dir or config.SCRATCH_DIR)
    if not scratch_dir.exists():
        return 0
    keep = scratch_files_in_use(entries)
    n = 0
    for p in scratch_dir.glob("*.raw"):
        if p.resolve() in keep:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
