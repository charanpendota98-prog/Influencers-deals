"""Keep every pytest run away from the operator's configured/live database."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_hub_database(tmp_path, monkeypatch):
    """Give every test a fresh SQLite DB, even tests without their own fixture."""
    from influencer_hub import config, db

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hub-test.sqlite3")
    db.init()
    yield
