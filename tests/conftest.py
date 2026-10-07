"""Keep every pytest run away from the operator's configured/live database.

Two layers, because fixtures are too late for the first one:

* importing this file points the default DB path at a fresh temporary file, so a
  test module that executes code while being *collected* (older tests called
  themselves at import time) can never touch the operator's database;
* the autouse fixture then hands every individual test its own ``tmp_path`` DB.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

_COLLECTION_DB = Path(tempfile.mkdtemp(prefix="hub-collection-")) / "hub.sqlite3"
try:  # pragma: no cover - import order guarantees the package is importable
    from influencer_hub import config as _config

    _config.DB_PATH = _COLLECTION_DB
except Exception:  # pragma: no cover - defensive
    pass


@pytest.fixture(autouse=True)
def isolate_hub_database(tmp_path, monkeypatch):
    """Give every test a fresh SQLite DB, even tests without their own fixture."""
    from influencer_hub import config, db

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hub-test.sqlite3")
    db.init()
    yield
