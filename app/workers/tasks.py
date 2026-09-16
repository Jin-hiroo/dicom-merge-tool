"""具体的なワーカー群。いずれも Worker を継承し、キャンセルと進捗に対応する。"""
from __future__ import annotations

from pathlib import Path

from app.core import dicom_io, meshing, resample, segment, session
from app.core.export_dicom import export_volume_to_dicom
from app.core.export_stl import export_volume_to_stl
from app.core.metrics import dice_score
from app.workers.base import Worker


class ScanWorker(Worker):
    """フォルダを走査してシリーズ一覧を作る (ピクセルは読まない)。"""

    def __init__(self, folder):
        super().__init__()
        self.folder = folder

    def run_task(self):
        return dicom_io.scan_folder(self.folder,
                                    progress_cb=self.emit_progress,
                                    cancel_cb=self.is_cancelled)


class LoadWorker(Worker):
    """シリーズを HU memmap として読み込む。"""

    def __init__(self, info, scratch_dir=None):
        super().__init__()
        self.info, self.scratch_dir = info, scratch_dir

    def run_task(self):
        return dicom_io.load_series(self.info, self.scratch_dir,
                                    progress_cb=self.emit_progress,
                                    cancel_cb=self.is_cancelled)


class MeshWorker(Worker):
    """プレビュー用サーフェスを作る。

    必要なら先に連結成分クリーニングを行う。結果は (role, polydata,
    cleaned_volume, factor) のタプルで返し、UI 側が actor に繋ぐ。
    """

    def __init__(self, role: str, volume, threshold: float,
                 largest_component: bool = False, min_island_voxels: int = 0,
                 scratch_dir=None):
        super().__init__()
        self.role = role
        self.volume = volume
        self.threshold = float(threshold)
        self.largest_component = largest_component
        self.min_island_voxels = int(min_island_voxels)
        self.scratch_dir = scratch_dir

    def run_task(self):
        vol = self.volume
        if segment.needs_mask(self.largest_component, self.min_island_voxels):
            vol = segment.clean_volume(
                vol, self.threshold,
                largest_component=self.largest_component,
                min_island_voxels=self.min_island_voxels,
                scratch_dir=self.scratch_dir,
                progress_cb=lambda p, m: self.emit_progress(int(p * 0.5), m),
                cancel_cb=self.is_cancelled,
            )
            if self.is_cancelled():
                return None
            poly, factor, small = meshing.preview_surface(
                vol, self.threshold,
                progress_cb=lambda p, m: self.emit_progress(50 + int(p * 0.5), m),
                cancel_cb=self.is_cancelled)
        else:
            poly, factor, small = meshing.preview_surface(
                vol, self.threshold,
                progress_cb=self.emit_progress,
                cancel_cb=self.is_cancelled)
        if self.is_cancelled():
            return None
        return {"role": self.role, "poly": poly, "volume": vol,
                "preview_volume": small, "factor": factor}


class MergeWorker(Worker):
    """位置合わせ済みの 2 ボリュームをボリューム空間で融合する。"""

    def __init__(self, fixed, moving, transform, mode, scratch_dir=None, name=None):
        super().__init__()
        self.fixed, self.moving = fixed, moving
        self.transform, self.mode = transform, mode
        self.scratch_dir, self.name = scratch_dir, name

    def run_task(self):
        volume, info = resample.merge_volumes(
            self.fixed, self.moving, self.transform, mode=self.mode,
            scratch_dir=self.scratch_dir, name=self.name,
            progress_cb=self.emit_progress, cancel_cb=self.is_cancelled)
        if volume is None:
            return None
        return {"volume": volume, "info": info}


class ExportWorker(Worker):
    """フル解像度で 1 回だけメッシュ化し STL を書く。"""

    def __init__(self, volume, path, threshold, target_triangles,
                 smooth_iterations, fill_holes=False, high_quality=False):
        super().__init__()
        self.high_quality = bool(high_quality)
        self.volume, self.path = volume, path
        self.threshold = float(threshold)
        self.target_triangles = int(target_triangles)
        self.smooth_iterations = int(smooth_iterations)
        self.fill_holes = bool(fill_holes)

    def run_task(self):
        return export_volume_to_stl(
            self.volume, self.path, self.threshold,
            target_triangles=self.target_triangles,
            smooth_iterations=self.smooth_iterations,
            fill_holes=self.fill_holes,
            high_quality=self.high_quality,
            progress_cb=self.emit_progress, cancel_cb=self.is_cancelled)


class ExportDicomWorker(Worker):
    """ボリュームを CT DICOM シリーズとして書き出す。

    1000 スライス級だとファイル生成だけで時間がかかるため、
    他の重処理と同様にワーカー上で進捗とキャンセルを扱う。
    """

    def __init__(self, volume, folder, description=None, threshold=None):
        super().__init__()
        self.volume, self.folder = volume, folder
        self.description = description
        self.threshold = threshold

    def run_task(self):
        return export_volume_to_dicom(
            self.volume, self.folder,
            series_description=self.description,
            threshold=self.threshold,
            progress_cb=self.emit_progress, cancel_cb=self.is_cancelled)


class SaveSessionWorker(Worker):
    """セッションを保存する。実体コピーを伴うので時間がかかりうる。"""

    def __init__(self, folder, entries, settings, copy_volumes=True):
        super().__init__()
        self.folder, self.entries = folder, entries
        self.settings, self.copy_volumes = settings, copy_volumes

    def run_task(self):
        return session.save_session(
            self.folder, self.entries, self.settings,
            copy_volumes=self.copy_volumes,
            progress_cb=self.emit_progress, cancel_cb=self.is_cancelled)


class LoadSessionWorker(Worker):
    """セッションを復元する。

    結合結果は memmap を開くだけで即座に戻るが、元 DICOM シリーズは
    フォルダから読み直す必要があるので、そのぶん時間がかかる。
    """

    def __init__(self, folder, scratch_dir=None):
        super().__init__()
        self.folder, self.scratch_dir = folder, scratch_dir

    def run_task(self):
        data = session.load_session(
            self.folder,
            progress_cb=lambda p, m: self.emit_progress(int(p * 0.2), m),
            cancel_cb=self.is_cancelled)
        if data.get("cancelled") or self.is_cancelled():
            return None

        entries = data["entries"]
        needs = [e for e in entries
                 if e["kind"] == "dicom" and e.get("was_loaded")]
        for i, entry in enumerate(needs):
            if self.is_cancelled():
                return None
            base = 20 + int(i / max(len(needs), 1) * 80)
            self.emit_progress(base, f"シリーズを読み直し中… {entry['title']}")
            try:
                info = self._find_series(entry["dicom"])
                if info is None:
                    data["problems"].append(
                        f"{entry['title']}: 元のシリーズが見つかりません "
                        f"({entry['dicom'].get('folder', '')})")
                    continue
                entry["info"] = info
                entry["volume"] = dicom_io.load_series(
                    info, self.scratch_dir,
                    progress_cb=lambda p, m, b=base: self.emit_progress(
                        b + int(p * 0.8 / max(len(needs), 1)), m),
                    cancel_cb=self.is_cancelled)
            except Exception as exc:                    # noqa: BLE001
                data["problems"].append(f"{entry['title']}: {exc}")

        self.emit_progress(100, "セッションを復元しました")
        return data

    def _find_series(self, ref):
        folder = ref.get("folder")
        uid = ref.get("series_uid")
        if not folder or not Path(folder).exists():
            return None
        found = dicom_io.scan_folder(folder, cancel_cb=self.is_cancelled)
        for info in found:
            if info.uid == uid:
                return info
        return None


class DiceWorker(Worker):
    """ボクセル Dice 係数 (オンデマンド、厳密)。"""

    def __init__(self, fixed, moving, threshold, transform):
        super().__init__()
        self.fixed, self.moving = fixed, moving
        self.threshold, self.transform = threshold, transform

    def run_task(self):
        return dice_score(self.fixed, self.moving, self.threshold,
                          transform=self.transform,
                          progress_cb=self.emit_progress,
                          cancel_cb=self.is_cancelled)
