import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.make_phantom import build     # noqa: E402


@pytest.fixture(scope="session")
def phantom(tmp_path_factory):
    """セッション中 1 回だけ合成 DICOM を生成する。"""
    return build(tmp_path_factory.mktemp("phantom"))


@pytest.fixture(scope="session")
def scratch(tmp_path_factory):
    return tmp_path_factory.mktemp("scratch")


@pytest.fixture(scope="session")
def volumes(phantom, scratch):
    from app.core import dicom_io
    a = dicom_io.scan_folder(phantom["folder_a"])[0]
    b = dicom_io.scan_folder(phantom["folder_b"])[0]
    return (dicom_io.load_series(a, scratch), dicom_io.load_series(b, scratch))
