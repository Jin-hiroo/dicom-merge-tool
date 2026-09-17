"""アプリ全体の定数。メモリ予算とセグメント既定値をここに集約する。"""
from pathlib import Path

APP_NAME = "head3Dv1 — DICOM 結合支援ツール"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRATCH_DIR = PROJECT_ROOT / "scratch"

# --- HU 定数 -------------------------------------------------------------
AIR_HU = -1024          # リサンプル時の背景値
HU_MIN, HU_MAX = -1024, 3071

# --- メモリ予算 (bytes) ---------------------------------------------------
# 16GB ユニファイドメモリの Apple Silicon 前提。GPU も同じ RAM を食うため
# 余裕をもって低めに設定している。超過時はスワップさせず自動的に粗くする。
MAX_VOLUME_BYTES = 2.0 * 1024**3       # 単一ボリュームの上限
MAX_TOTAL_VOLUME_BYTES = 6.0 * 1024**3  # 常駐ボリューム合計の上限
PREVIEW_VOLUME_BYTES = 64 * 1024**2     # プレビュー用ダウンサンプル後の目標
RESLICE_SLAB_SLICES = 16                # 結合時に一度に処理する出力スライス数

# --- メッシュ -------------------------------------------------------------
# プレビューの三角形上限。Apple Silicon の GPU は 100 万三角形程度なら余裕で
# 回せるので、高く取って「そもそも間引かない」ほうが体感は速い。
# (間引き自体のほうがマーチングキューブより桁違いに重いため)
PREVIEW_TARGET_TRIANGLES = 1_200_000
EXPORT_TARGET_TRIANGLES = 2_000_000
EXPORT_SMOOTH_ITERATIONS = 20           # vtkWindowedSincPolyDataFilter
METRIC_SAMPLE_POINTS = 20_000           # 重なり誤差の計算に使う表面点数

# --- セグメント プリセット (表示名 -> 既定 HU 閾値) --------------------------
SEGMENT_PRESETS = {
    "骨 (Bone)": 250,
    "軟部組織 (Soft tissue)": -100,
    "皮膚・体表 (Skin)": -400,
}
DEFAULT_PRESET = "骨 (Bone)"
THRESHOLD_RANGE = (-500, 1500)

# --- 位置合わせ -----------------------------------------------------------
TRANSLATE_STEPS_MM = [0.1, 0.5, 1.0, 5.0, 10.0]
ROTATE_STEPS_DEG = [0.1, 0.5, 1.0, 5.0, 10.0]
DEFAULT_TRANSLATE_STEP = 1.0
DEFAULT_ROTATE_STEP = 1.0

# --- 表示色 ---------------------------------------------------------------
COLOR_FIXED = (0.92, 0.88, 0.78)    # アイボリー
COLOR_MOVING = (0.30, 0.78, 0.90)   # シアン
COLOR_MERGED = (0.85, 0.85, 0.88)
# 役割を割り当てずに単体で確認するためのプレビュー色。
# Fixed(アイボリー) / Moving(シアン) のどちらとも見分けがつく色にする。
COLOR_PREVIEW = (0.78, 0.55, 0.95)  # 紫
OPACITY_FIXED = 1.0
OPACITY_MOVING = 0.55
OPACITY_PREVIEW = 1.0

# シリーズごとの表示色を選ぶときの候補 (表示名 -> RGB)。
# 暗いグラデーション背景の上で互いに判別でき、かつ既定色 3 色と衝突しない並び。
SERIES_COLOR_PRESETS = [
    ("アイボリー", COLOR_FIXED),
    ("シアン", COLOR_MOVING),
    ("紫", COLOR_PREVIEW),
    ("赤", (0.90, 0.38, 0.38)),
    ("緑", (0.45, 0.82, 0.48)),
    ("橙", (0.95, 0.66, 0.30)),
    ("黄", (0.93, 0.88, 0.40)),
    ("青", (0.42, 0.58, 0.95)),
]

BLEND_MODES = ["feather (推奨)", "max", "mean"]
DEFAULT_BLEND = "feather (推奨)"

DISCLAIMER = (
    "本ソフトウェアは診断用医療機器ではありません。\n"
    "研究・造形補助を目的としたツールであり、臨床診断には使用しないでください。\n"
    "患者データはすべてこの PC 内でのみ処理され、外部送信は一切行いません。"
)
