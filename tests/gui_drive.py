"""GUI をプログラムから操作して一連のワークフローを検証する。

実際のボタン/シグナルを叩き、各段階でスクリーンショットを保存する。
制約1 (UI が固まらない) の確認も兼ねる: 重い処理の最中も
processEvents() が回り続けることを計測する。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pydicom                                                # noqa: E402
import vtk                                                    # noqa: E402
from PyQt5 import QtCore, QtWidgets                           # noqa: E402

from app import config                                        # noqa: E402
from app.ui.main_window import DARK_QSS, MainWindow           # noqa: E402
from app.ui.series_panel import (ROLE_FIXED, ROLE_MOVING,     # noqa: E402
                                 color_for_key)
from tests.make_phantom import build                          # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/gui_out")
OUT.mkdir(parents=True, exist_ok=True)
# 起動時のセッション復元プロンプトはこの検証の対象外なので抑止する
# (これを忘れると、復元がシリーズ一覧を差し替えてしまう)
os.environ["HEAD3DV1_NO_RESTORE"] = "1"
# ユーザーの実際の環境設定を自動テストが上書きしないよう別スコープにする
os.environ["HEAD3DV1_SETTINGS_SCOPE"] = "test-gui-drive"

_ticks = 0


def pump(app, seconds=0.05):
    global _ticks
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        _ticks += 1
        time.sleep(0.002)


def wait_idle(app, window, timeout=120, label=""):
    """処理完了まで待つ。待っている間 UI が応答し続けているかも数える。"""
    global _ticks
    _ticks = 0
    t0 = time.time()
    while window.runner.busy and time.time() - t0 < timeout:
        app.processEvents()
        _ticks += 1
        time.sleep(0.002)
    pump(app, 0.15)
    elapsed = time.time() - t0
    if label:
        print(f"    {label}: {elapsed:.2f}s, UI イベントループ {_ticks} 回転"
              f" ({'応答維持' if _ticks > 5 else '★ブロックの疑い'})")
    return elapsed


def shot(window, name):
    """Qt レイアウトと VTK 描画をそれぞれ保存する。"""
    window.grab().save(str(OUT / f"{name}_layout.png"))
    rw = window.viewer3d.interactor.GetRenderWindow()
    rw.Render()
    g = vtk.vtkWindowToImageFilter()
    g.SetInput(rw)
    g.ReadFrontBufferOff()
    g.Update()
    w = vtk.vtkPNGWriter()
    w.SetFileName(str(OUT / f"{name}_3d.png"))
    w.SetInputConnection(g.GetOutputPort())
    w.Write()
    print(f"    -> {name}_layout.png / {name}_3d.png")


def add_folder(app, window, folder, label):
    orig = QtWidgets.QFileDialog.getExistingDirectory
    QtWidgets.QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: str(folder))
    try:
        window.series_panel.add_btn.click()
        wait_idle(app, window, label=f"走査 {label}")
    finally:
        QtWidgets.QFileDialog.getExistingDirectory = orig


def main():
    print("=== 合成ファントムを生成 ===")
    ph = build(OUT / "phantom")
    print(f"    仕込んだズレ {ph['offset']} / 正解の補正 {ph['correction']}")

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyleSheet(DARK_QSS)
    window = MainWindow()
    window.show()
    window.viewer3d.initialize()
    pump(app, 0.4)

    print("\n=== [1] シリーズ A/B を読み込む ===")
    add_folder(app, window, ph["folder_a"], "A")
    add_folder(app, window, ph["folder_b"], "B")
    print(f"    一覧: {[e.title for e in window.series_panel.entries]}")
    assert len(window.series_panel.entries) == 2, "2 シリーズ検出されていない"

    for row in (0, 1):
        window.series_panel.list.setCurrentRow(row)
        window.series_panel.load_btn.click()
        wait_idle(app, window, label=f"読込 row{row}")
    assert all(e.loaded for e in window.series_panel.entries), "読込未完了"

    print("\n=== [2] Fixed / Moving を割り当ててプレビュー生成 ===")
    # 1 本目は読込時に自動で Fixed になる。ボタンは冪等なので押しても変わらない。
    window.series_panel.list.setCurrentRow(0)
    window.series_panel.fixed_btn.click()
    wait_idle(app, window, label="Fixed プレビュー")
    assert window.series_panel.entries[0].role == ROLE_FIXED, "Fixed 割当が冪等でない"
    window.series_panel.list.setCurrentRow(1)
    window.series_panel.moving_btn.click()
    wait_idle(app, window, label="Moving プレビュー")
    pump(app, 0.6)

    assert window.viewer3d.has(ROLE_FIXED), "Fixed が表示されていない"
    assert window.viewer3d.has(ROLE_MOVING), "Moving が表示されていない"
    window.viewer3d.set_view("前 (A)")
    pump(app, 0.3)
    print("    " + window.align_panel.metric_label.text().replace("\n", " | "))
    shot(window, "01_misaligned")

    print("\n=== [2b] シリーズごとの表示色を変える ===")
    red = (0.90, 0.38, 0.38)
    window.series_panel.list.setCurrentRow(1)
    window.series_panel._apply_color(red)
    pump(app, 0.3)
    got = window.viewer3d._actors[ROLE_MOVING].GetProperty().GetColor()
    print(f"    Moving の色: {tuple(round(c, 3) for c in got)}")
    assert all(abs(a - b) < 1e-3 for a, b in zip(got, red)), \
        f"指定色が 3D に反映されていない: {got}"
    # 色替えでメッシュを作り直していないこと (位置合わせ中でも巻き戻らない)
    assert not window.runner.busy, "色替えで再メッシュが走っている"
    # 役割を跨いでも指定色が保たれること
    assert color_for_key(window.series_panel.entries[1], ROLE_FIXED) == red
    shot(window, "01b_series_color")

    print("    既定に戻す")
    window.series_panel._apply_color(None)
    pump(app, 0.3)
    got = window.viewer3d._actors[ROLE_MOVING].GetProperty().GetColor()
    assert all(abs(a - b) < 1e-3 for a, b in zip(got, config.COLOR_MOVING)), \
        f"既定色に戻っていない: {got}"

    print("\n=== [3] X/Y/Z・回転ボタンで位置合わせ ===")
    before = window.metric.evaluate(window.align_panel.transform.matrix())
    print(f"    調整前: 平均 {before['mean_mm']:.3f} mm")

    # 正解は (-7, +4, -11)。1mm ステップでボタンを連打して追い込む。
    t0 = time.time()
    plan = [("tx", -1.0, 7), ("ty", +1.0, 4), ("tz", -1.0, 11)]
    for key, sign, count in plan:
        row = window.align_panel.rows[key]
        btn = row.findChildren(QtWidgets.QPushButton)[0 if sign < 0 else 1]
        for _ in range(count):
            btn.click()
            app.processEvents()
    clicks = sum(c for _, _, c in plan)
    dt = time.time() - t0
    print(f"    ボタン {clicks} 回クリック: {dt*1000:.0f} ms "
          f"({dt/clicks*1000:.1f} ms/回 — メッシュ再構築なし)")

    vals = window.align_panel.transform.values()
    print(f"    現在の変換: tx={vals['tx']:.1f} ty={vals['ty']:.1f} tz={vals['tz']:.1f}")
    assert (vals["tx"], vals["ty"], vals["tz"]) == ph["correction"], \
        f"ボタン操作の結果が正解と違う: {vals}"

    after = window.metric.evaluate(window.align_panel.transform.matrix())
    print(f"    調整後: 平均 {after['mean_mm']:.3f} mm "
          f"(重複点 {after['n_overlap']:,})")
    assert after["mean_mm"] < before["mean_mm"], "位置合わせで改善していない"
    pump(app, 0.3)
    shot(window, "02_aligned")

    print("\n=== [4] 回転ボタンの確認 ===")
    rrow = window.align_panel.rows["rz"]
    rbtn = rrow.findChildren(QtWidgets.QPushButton)[1]
    for _ in range(5):
        rbtn.click()
        app.processEvents()
    assert abs(window.align_panel.transform.rz - 5.0) < 1e-6, "回転が効いていない"
    rotated = window.metric.evaluate(window.align_panel.transform.matrix())
    print(f"    Yaw +5° 後: 平均 {rotated['mean_mm']:.3f} mm (悪化するはず)")
    assert rotated["mean_mm"] > after["mean_mm"], "回転しても指標が変わらない"
    window.align_panel.undo_btn.click() if False else None
    for _ in range(5):
        rrow.findChildren(QtWidgets.QPushButton)[0].click()
        app.processEvents()
    assert abs(window.align_panel.transform.rz) < 1e-6
    print("    回転を戻して復帰: OK")

    print("\n=== [5] Undo / Redo ===")
    window.align_panel.rows["tz"].findChildren(QtWidgets.QPushButton)[1].click()
    app.processEvents()
    moved = window.align_panel.transform.tz
    window.align_panel.undo_btn.click()
    app.processEvents()
    assert abs(window.align_panel.transform.tz - ph["correction"][2]) < 1e-9, "Undo 失敗"
    window.align_panel.redo_btn.click()
    app.processEvents()
    assert abs(window.align_panel.transform.tz - moved) < 1e-9, "Redo 失敗"
    window.align_panel.undo_btn.click()
    app.processEvents()
    print("    Undo/Redo: OK")

    print("\n=== [6] 2D 重ね合わせビュー ===")
    for i in range(3):
        window.viewer2d.tabs.setCurrentIndex(i)
        pump(app, 0.25)
    window.viewer2d.tabs.setCurrentIndex(1)   # coronal は継ぎ目が見やすい
    pump(app, 0.3)
    canvas = window.viewer2d._canvases[1]
    assert canvas.pixmap() is not None and not canvas.pixmap().isNull(), \
        "2D スライスが描画されていない"
    canvas.pixmap().save(str(OUT / "03_coronal_overlay.png"))
    print("    -> 03_coronal_overlay.png")

    print("\n=== [7] 結合を実行 ===")
    print("    " + window.merge_panel.grid_label.text().replace("\n", " | "))
    orig_q = QtWidgets.QMessageBox.question
    QtWidgets.QMessageBox.question = staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.Ok)
    try:
        window.merge_panel.merge_btn.click()
        wait_idle(app, window, label="融合")
        wait_idle(app, window, label="結合後プレビュー")
    finally:
        QtWidgets.QMessageBox.question = orig_q
    pump(app, 0.5)

    merged = window.series_panel.by_role(ROLE_FIXED)
    assert merged is not None and merged.merged, "結合結果が Fixed になっていない"
    mv = merged.volume
    print(f"    結合結果: {mv.name}")
    print(f"      {mv.shape_xyz} ボクセル / z 範囲 "
          f"{mv.world_bounds()[0][2]:.1f} 〜 {mv.world_bounds()[1][2]:.1f} mm")
    assert abs(mv.world_bounds()[0][2] - 0.0) < 1e-6
    assert abs(mv.world_bounds()[1][2] - 200.0) < 1e-6, "結合後の範囲が正解と違う"
    assert window.series_panel.by_role(ROLE_MOVING) is None, "Moving が残っている"
    window.viewer3d.set_view("前 (A)")
    pump(app, 0.4)
    shot(window, "04_merged")

    print("\n=== [8] STL 書き出し ===")
    stl_path = OUT / "merged.stl"
    orig_s = QtWidgets.QFileDialog.getSaveFileName
    QtWidgets.QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (str(stl_path), "STL"))
    QtWidgets.QMessageBox.question = staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.Ok)
    info_seen = {}
    QtWidgets.QMessageBox.information = staticmethod(
        lambda *a, **k: info_seen.update({"text": a[2] if len(a) > 2 else ""}))
    try:
        window.merge_panel.export_btn.click()
        wait_idle(app, window, label="STL 書出し")
    finally:
        QtWidgets.QFileDialog.getSaveFileName = orig_s
        QtWidgets.QMessageBox.question = orig_q

    assert stl_path.exists(), "STL が出力されていない"
    reader = vtk.vtkSTLReader()
    reader.SetFileName(str(stl_path))
    reader.Update()
    poly = reader.GetOutput()
    b = poly.GetBounds()
    print(f"    {stl_path.name}: {stl_path.stat().st_size/1024:.0f} KB / "
          f"{poly.GetNumberOfPolys():,} 三角形")
    print(f"    バウンディングボックス: {b[1]-b[0]:.1f} x {b[3]-b[2]:.1f} x "
          f"{b[5]-b[4]:.1f} mm")
    assert poly.GetNumberOfPolys() > 1000, "STL の中身が乏しい"
    assert (b[5] - b[4]) > 150, "z 方向の長さが足りない (結合されていない?)"

    print("\n=== [8b] DICOM 書き出し ===")
    dicom_parent = OUT / "dicom"
    dicom_parent.mkdir(parents=True, exist_ok=True)
    orig_dir = QtWidgets.QFileDialog.getExistingDirectory
    QtWidgets.QFileDialog.getExistingDirectory = staticmethod(
        lambda *a, **k: str(dicom_parent))
    QtWidgets.QMessageBox.question = staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.Ok)
    try:
        window.merge_panel.export_dicom_btn.click()
        wait_idle(app, window, label="DICOM 書出し")
    finally:
        QtWidgets.QFileDialog.getExistingDirectory = orig_dir
        QtWidgets.QMessageBox.question = orig_q

    written = sorted(dicom_parent.rglob("*.dcm"))
    assert written, "DICOM が出力されていない"
    folder = written[0].parent
    print(f"    {folder.name}/: {len(written):,} 枚 / "
          f"{sum(f.stat().st_size for f in written)/1024/1024:.1f} MB")

    # 書き出したものを読み戻して、結合結果と一致するか確かめる
    from app.core.export_dicom import read_back_volume
    merged_vol = window.series_panel.by_role(ROLE_FIXED).volume
    back = read_back_volume(folder, OUT / "readback")
    import numpy as _np
    assert back.shape_zyx == merged_vol.shape_zyx, "読み戻しの形状が違う"
    assert _np.allclose(back.origin, merged_vol.origin, atol=1e-6)
    assert _np.allclose(back.spacing, merged_vol.spacing, atol=1e-6)
    assert _np.array_equal(_np.asarray(back.array), _np.asarray(merged_vol.array)), \
        "読み戻した HU が一致しない"
    ds = pydicom.dcmread(str(written[0]))
    print(f"    読み戻し一致: {back.shape_xyz} / HU 完全一致")
    print(f"    ImageType={list(ds.ImageType)} PatientID={ds.PatientID}")
    print(f"    z 範囲 {back.world_bounds()[0][2]:.1f} 〜 "
          f"{back.world_bounds()[1][2]:.1f} mm")

    print("\n=== [9] 3 本目の逐次結合が可能か ===")
    add_folder(app, window, ph["folder_b"], "B(3本目として再利用)")
    window.series_panel.list.setCurrentRow(len(window.series_panel.entries) - 1)
    window.series_panel.load_btn.click()
    wait_idle(app, window, label="3本目の読込")
    window.series_panel.moving_btn.click()
    wait_idle(app, window, label="3本目のプレビュー")
    assert window.series_panel.by_role(ROLE_FIXED).merged, "結合結果が Fixed のまま残っていない"
    assert window.series_panel.by_role(ROLE_MOVING) is not None
    print("    結合済み(Fixed) + 新シリーズ(Moving) の構成を確認: OK")

    # 実際にもう一度結合し、A+B に C を足せることを確かめる
    QtWidgets.QMessageBox.question = staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.Ok)
    try:
        window.merge_panel.merge_btn.click()
        wait_idle(app, window, label="2 回目の融合")
        wait_idle(app, window, label="2 回目の結合後プレビュー")
    finally:
        QtWidgets.QMessageBox.question = orig_q
    second = window.series_panel.by_role(ROLE_FIXED)
    assert second is not None and second.merged
    print(f"    2 回目の結合結果: {second.volume.name}")
    assert "3 シリーズ" in second.volume.name, \
        f"世代表記が期待と違う: {second.volume.name}"
    assert len(second.volume.name) < 40, "名前が伸び続けている"
    shot(window, "05_third_series")

    print("\n" + "=" * 62)
    print("すべての GUI ワークフロー検証をパスしました")
    print("=" * 62)
    window.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
