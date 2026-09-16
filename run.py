#!/usr/bin/env python3
"""head3Dv1 の起動スクリプト。

必ず venv の Python で実行すること:
    ./.venv/bin/python run.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _check_env():
    missing = []
    for mod, label in (("vtk", "vtk"), ("pydicom", "pydicom"),
                       ("PyQt5", "PyQt5"), ("scipy", "scipy")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(label)
    if missing:
        print("依存パッケージが見つかりません:", ", ".join(missing), file=sys.stderr)
        print("\nvenv の Python で起動してください:", file=sys.stderr)
        print("    ./.venv/bin/python run.py", file=sys.stderr)
        print("\n未作成の場合:", file=sys.stderr)
        print("    /opt/anaconda3/bin/python3 -m venv .venv", file=sys.stderr)
        print("    ./.venv/bin/python -m pip install -r requirements.txt",
              file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    if not _check_env():
        sys.exit(1)
    from app.main import main
    sys.exit(main())
