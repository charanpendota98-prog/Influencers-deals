import os
import tempfile

import pytest

# Point the DB at a temp file before importing the module under test.
_TMP = os.path.join(tempfile.mkdtemp(), "hub_test.sqlite3")
os.environ["HUB_DB_PATH"] = _TMP

from influencer_hub import db  # noqa: E402


@pytest.fixture(autouse=True)
def _init():
    db.init()


def test_influencer_crud():
    iid = db.add_influencer("Ravi", "ravi099-21", handle="@ravi", use_dummy_sources=True)
    inf = db.get_influencer(iid)
    assert inf["name"] == "Ravi"
    assert inf["amazon_tag"] == "ravi099-21"
    assert inf["use_dummy_sources"] == 1
    assert any(x["id"] == iid for x in db.list_influencers())
    db.set_influencer_active(iid, False)
    assert db.get_influencer(iid)["active"] == 0


def test_channel_crud():
    iid = db.add_influencer("Ravi", "ravi099-21")
    cid = db.add_channel(iid, "telegram", "@raviloots", invite_link="https://t.me/x")
    chs = db.list_channels(iid)
    assert len(chs) == 1 and chs[0]["identifier"] == "@raviloots"
    db.update_channel(iid, "telegram", "@raviloots", status="ready")
    assert db.list_channels(iid)[0]["status"] == "ready"


def test_posts_dedup():
    iid = db.add_influencer("Ravi", "ravi099-21")
    cid = db.add_channel(iid, "telegram", "@raviloots", status="ready")
    assert not db.already_posted(iid, cid, "sig1")
    db.record_post(iid, cid, "sig1", status="posted")
    assert db.already_posted(iid, cid, "sig1")
    db.record_post(iid, cid, "sig2", status="failed")
    stats = db.post_stats()
    assert stats["posted"] == 1 and stats["failed"] == 1


def test_sources():
    db.add_source("dummy1", "https://t.me/dummy1", kind="dummy")
    db.add_source("prod1", "https://t.me/prod1", kind="production")
    dummies = db.list_sources(kind="dummy")
    assert len(dummies) == 1 and dummies[0]["kind"] == "dummy"


def test_vm_stats():
    db.record_vm(12.0, 34.0, 56.0, True)
    snap = db.latest_vm()
    assert snap["cpu_pct"] == 12.0 and snap["bot_running"] == 1
