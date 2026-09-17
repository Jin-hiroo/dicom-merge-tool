"""プレビューモードの検証。

プレビューは「1 つの DICOM を選んで *それのみ* を表示する」独立したモード。
位置合わせ ([Fixed | Moving]) とは排他で、往復しても作業が失われないこと。
"""
import os, sys, time
from pathlib import Path
os.environ["HEAD3DV1_NO_RESTORE"] = "1"
os.environ["HEAD3DV1_SETTINGS_SCOPE"] = "test-preview-mode"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np, vtk
from PyQt5 import QtWidgets
from app import config
from app.ui.main_window import MODE_ALIGN, MODE_PREVIEW, DARK_QSS, MainWindow
from app.ui.series_panel import PREVIEW_KEY, ROLE_FIXED, ROLE_MOVING
from tests.make_phantom import build

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
for p in config.SCRATCH_DIR.glob("*.raw"): p.unlink()
ph = build(OUT / "phantom")

app = QtWidgets.QApplication(sys.argv[:1]); app.setStyleSheet(DARK_QSS)
def pump(s=0.1):
    e=time.time()+s
    while time.time()<e: app.processEvents(); time.sleep(0.003)
def wait(w, t=90):
    t0=time.time()
    while w.runner.busy and time.time()-t0<t: app.processEvents(); time.sleep(0.003)
    pump(0.2)
def settle(w, n=4):
    for _ in range(n): wait(w)
    pump(0.2)
def add_folder(w, folder):
    o = QtWidgets.QFileDialog.getExistingDirectory
    QtWidgets.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(folder))
    try: w.series_panel.add_btn.click(); wait(w)
    finally: QtWidgets.QFileDialog.getExistingDirectory = o
def visible(w, key):
    a = w.viewer3d._actors.get(key)
    return bool(a and a.GetVisibility())
def shot(w, name):
    rw = w.viewer3d.interactor.GetRenderWindow(); rw.Render()
    g = vtk.vtkWindowToImageFilter(); g.SetInput(rw); g.ReadFrontBufferOff(); g.Update()
    wr = vtk.vtkPNGWriter(); wr.SetFileName(str(OUT / f"{name}.png"))
    wr.SetInputConnection(g.GetOutputPort()); wr.Write()
    w.grab().save(str(OUT / f"{name}_layout.png"))

# モーダルは出ない想定だが、出たら検知できるようにしておく
modals = []
QtWidgets.QMessageBox.information = staticmethod(
    lambda *a, **k: modals.append(a[1] if len(a) > 1 else "?"))

w = MainWindow(); w.show(); w.viewer3d.initialize(); pump(0.4)
add_folder(w, ph["folder_a"]); add_folder(w, ph["folder_b"])
for row in (0, 1):
    w.series_panel.list.setCurrentRow(row); w.series_panel.load_btn.click(); wait(w)
sp = w.series_panel
w.segment_panel.set_threshold(250); w.segment_panel.apply_btn.click(); settle(w)

print("=== [1] 位置合わせモードで Fixed / Moving を並べる ===")
assert w._mode == MODE_ALIGN, "起動時は位置合わせモードのはず"
sp.list.setCurrentRow(1); sp.moving_btn.click(); settle(w)
c = ph["correction"]
w.align_panel.transform.set_values(tx=c[0], ty=c[1], tz=c[2]); w.on_transform_changed()
align_vals = dict(w.align_panel.transform.values())
align_metric = w.metric.evaluate(w.align_panel.transform.matrix())["mean_mm"]
fixed_poly = w._meshes[ROLE_FIXED]["poly"]
moving_poly = w._meshes[ROLE_MOVING]["poly"]
align_camera = w.viewer3d.save_camera()
print(f"  Fixed/Moving 表示: {visible(w, ROLE_FIXED)} / {visible(w, ROLE_MOVING)}")
print(f"  重なり誤差 {align_metric:.3f} mm")
assert visible(w, ROLE_FIXED) and visible(w, ROLE_MOVING)
shot(w, "01_align_mode")

print("\n=== [2] プレビューモードへ切替 — それのみが表示される ===")
w.mode_tabs.setCurrentIndex(1); settle(w)
assert w._mode == MODE_PREVIEW
assert visible(w, PREVIEW_KEY), "プレビューが表示されていない"
assert not visible(w, ROLE_FIXED), "★Fixed が見えたままになっている"
assert not visible(w, ROLE_MOVING), "★Moving が見えたままになっている"
print(f"  表示中: プレビューのみ "
      f"(fixed={visible(w, ROLE_FIXED)}, moving={visible(w, ROLE_MOVING)}, "
      f"preview={visible(w, PREVIEW_KEY)})")
print(f"  対象: {w._preview_entry.title}")
assert w._preview_entry is sp.current_entry()
shot(w, "02_preview_mode")

print("\n=== [3] メッシュは破棄されていない (往復で再生成しない) ===")
assert w._meshes[ROLE_FIXED]["poly"] is fixed_poly, "★Fixed のメッシュが作り直された"
assert w._meshes[ROLE_MOVING]["poly"] is moving_poly, "★Moving のメッシュが作り直された"
print("  Fixed / Moving の polydata は同一オブジェクトのまま")

print("\n=== [4] 選択＝即表示 ===")
before = w._meshes[PREVIEW_KEY]["poly"]
before_title = w._preview_entry.title
sp.list.setCurrentRow(0); settle(w)
assert w._preview_entry is sp.entries[0], "選択に追従していない"
assert w._meshes[PREVIEW_KEY]["poly"] is not before, "メッシュが差し替わっていない"
print(f"  {before_title} -> {w._preview_entry.title} に切替")

print("\n=== [5] 連打に耐える ===")
modals.clear()
for row in (1, 0, 1, 0, 1):
    sp.list.setCurrentRow(row); app.processEvents(); time.sleep(0.02)
settle(w, 8)
assert not modals, f"★モーダルが出た: {modals}"
assert w._preview_entry is sp.entries[1], \
    f"最後に選んだものが表示されていない: {w._preview_entry.title}"
assert visible(w, PREVIEW_KEY)
print(f"  5 回連続切替後も正常: {w._preview_entry.title} (モーダルなし)")

print("\n=== [6] 書き出し対象がプレビュー中のシリーズになる ===")
assert w._export_target() is w._preview_entry, "★書き出し対象がプレビューと違う"
assert w.merge_panel.export_btn.isEnabled()
assert w.merge_panel.export_dicom_btn.isEnabled()
assert not w.merge_panel.merge_btn.isEnabled(), "★プレビュー中に結合が押せる"
print(f"  対象: {w._export_target().title} / 結合は無効")
print(f"  パネル表示: {w.merge_panel.export_hint.text()}")

print("\n=== [7] 位置合わせへ戻る — 作業も視点も元通り ===")
w.mode_tabs.setCurrentIndex(0); settle(w)
assert w._mode == MODE_ALIGN
assert visible(w, ROLE_FIXED) and visible(w, ROLE_MOVING), "★戻っても表示されない"
assert not visible(w, PREVIEW_KEY), "★プレビューが残っている"
assert w._meshes[ROLE_FIXED]["poly"] is fixed_poly, "★戻りで再生成された"
assert w._meshes[ROLE_MOVING]["poly"] is moving_poly, "★戻りで再生成された"
after_vals = dict(w.align_panel.transform.values())
after_metric = w.metric.evaluate(w.align_panel.transform.matrix())["mean_mm"]
assert after_vals == align_vals, f"★位置合わせが変わった: {align_vals} -> {after_vals}"
assert abs(after_metric - align_metric) < 1e-9, "★重なり誤差が変わった"
print(f"  重なり誤差 {after_metric:.3f} mm (往復前と一致)")
cam = w.viewer3d.save_camera()
assert np.allclose(cam["position"], align_camera["position"], atol=1e-6), \
    "★視点が復元されていない"
print("  視点も復元")
assert w._export_target() is sp.by_role(ROLE_FIXED), "★書き出し対象が Fixed に戻らない"
assert w.merge_panel.merge_btn.isEnabled(), "★結合が有効に戻らない"
print("  書き出し対象は Fixed / 結合は有効")
shot(w, "03_back_to_align")

print("\n" + "=" * 58)
print("プレビューモードの検証をすべてパスしました")
print("=" * 58)
w.close(); pump(0.3)
