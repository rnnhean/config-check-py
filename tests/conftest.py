"""测试夹具。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

DATA_PATH = BASE_DIR / "data" / "products.json"


@pytest.fixture(scope="session")
def data_path() -> Path:
    return DATA_PATH


@pytest.fixture(scope="session")
def raw_dataset() -> dict:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def tmp_dataset(tmp_path: Path, raw_dataset: dict) -> Path:
    path = tmp_path / "products.json"
    path.write_text(json.dumps(raw_dataset, ensure_ascii=False), encoding="utf-8")
    return path
