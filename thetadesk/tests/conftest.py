"""Shared fixtures for thetadesk tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import make_chain
from thetadesk.models import Chain


@pytest.fixture
def chain() -> Chain:
    return make_chain()


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "thetadesk-test"
    d.mkdir()
    return d


@pytest.fixture
def examples_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "examples" / "chains"
