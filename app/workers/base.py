"""キャンセル可能なワーカーの基底。

制約1: 重い処理をメインスレッドで動かすと UI が返ってこなくなる。
すべての重処理はここを継承して QThread 上で実行する。

重要な約束:
  * ワーカーから Renderer / RenderWindow / Actor に触らない。
    返すのは numpy 配列か vtkPolyData のみで、GL コンテキストには触れない。
  * 長いループは必ず is_cancelled() を見る。
"""
from __future__ import annotations

import traceback

from PyQt5 import QtCore

# ワーカースレッドのスタックサイズ (既定では OpenBLAS の並列 LAPACK に足りない)
WORKER_STACK_BYTES = 64 * 1024 * 1024


class Worker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, str)      # 0-100, メッセージ
    finished = QtCore.pyqtSignal(object)        # 結果 (キャンセル時は None)
    failed = QtCore.pyqtSignal(str)             # エラーメッセージ
    cancelled = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self._cancel = False

    # -- サブクラスが実装する ------------------------------------------
    def run_task(self):
        raise NotImplementedError

    # -- 進捗/キャンセル -----------------------------------------------
    def is_cancelled(self) -> bool:
        return self._cancel

    @QtCore.pyqtSlot()
    def cancel(self):
        self._cancel = True

    def emit_progress(self, pct: int, message: str = ""):
        self.progress.emit(int(max(0, min(100, pct))), message)

    # -- エントリポイント ------------------------------------------------
    @QtCore.pyqtSlot()
    def run(self):
        try:
            result = self.run_task()
            if self._cancel:
                self.cancelled.emit()
                self.finished.emit(None)
            else:
                self.finished.emit(result)
        except Exception as exc:                        # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}\n\n"
                             f"{traceback.format_exc(limit=6)}")
            self.finished.emit(None)


class TaskRunner(QtCore.QObject):
    """ワーカーを QThread に載せて回すヘルパ。

    1 つの TaskRunner は同時に 1 つのタスクだけを実行する。UI 側は
    started/progress/done/failed を繋ぐだけでよい。
    """
    progress = QtCore.pyqtSignal(int, str)
    done = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    busy_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: QtCore.QThread | None = None
        self._worker: Worker | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, worker: Worker) -> bool:
        if self.busy:
            return False

        thread = QtCore.QThread()
        # QThread の既定スタックは OpenBLAS/LAPACK の大きなフレームに耐えられず、
        # ワーカー内で線形代数を呼ぶと SIGBUS (stack size exceeded) で落ちる。
        # 自前コードでは inv44() で LAPACK を避けているが、numpy/scipy の別経路
        # でも同じ罠を踏みうるので、保険としてスタックを広げておく。
        thread.setStackSize(WORKER_STACK_BYTES)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self.progress)
        worker.failed.connect(self.failed)
        worker.finished.connect(self._on_finished)

        self._thread, self._worker = thread, worker
        self.busy_changed.emit(True)
        thread.start()
        return True

    @QtCore.pyqtSlot(object)
    def _on_finished(self, result):
        thread, self._thread = self._thread, None
        worker, self._worker = self._worker, None
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()
        self.busy_changed.emit(False)
        self.done.emit(result)

    def cancel(self):
        if self._worker is not None:
            self._worker.cancel()

    def shutdown(self):
        """アプリ終了時に走行中のスレッドを畳む。"""
        self.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)


class FunctionWorker(Worker):
    """関数 1 つを包むだけの汎用ワーカー。

    対象の関数は progress_cb / cancel_cb を受け取れること。
    """

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs

    def run_task(self):
        return self._fn(*self._args,
                        progress_cb=self.emit_progress,
                        cancel_cb=self.is_cancelled,
                        **self._kwargs)
