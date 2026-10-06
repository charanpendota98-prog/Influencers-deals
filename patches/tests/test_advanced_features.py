import asyncio
from unittest.mock import patch
import pytest

from influencer_hub import db, pipeline, config


def test_only_amazon_filter_channel_and_influencer():
    db.init()
    # Influencer with only_amazon=True
    iid = db.add_influencer("Amazon Only Partner", "amzonly-21", only_amazon=True)
    cid_broadcast = db.add_channel(iid, "telegram", "@amzdeals", role="broadcast", status="ready")

    # 1. Pure Amazon deal -> Should be posted
    deal_amazon = "🔥 Sony TV 55 inch at ₹49,990 https://www.amazon.in/dp/B08XYZ1234"
    # 2. Pure Flipkart deal -> Should be skipped because only_amazon is ON!
    deal_flipkart = "⚡ Flipkart Shoes at ₹799 https://www.flipkart.com/p/itm12345"

    posted_messages = []

    async def fake_post(chan, text, **kwargs):
        posted_messages.append((chan, text))

    with patch("influencer_hub.telegram_ops.post_to_channel", side_effect=fake_post):
        # Dispatch amazon deal
        res1 = asyncio.run(pipeline.render_and_dispatch(deal_amazon, influencer_ids=[iid]))
        # Dispatch flipkart deal
        res2 = asyncio.run(pipeline.render_and_dispatch(deal_flipkart, influencer_ids=[iid]))

    assert res1[iid][cid_broadcast] == "posted"
    assert res2[iid][cid_broadcast] == "skipped"

    db.delete_influencer(iid)


def test_delete_influencer_uses_the_shared_unlock_not_an_inline_password(monkeypatch):
    """Deleting a creator is a removal: one shared confirmation, no second form.

    The middleware gate itself (locked -> 428, unlocked -> 302) is pinned in
    tests/test_reauth_policy.py; this test only proves the route no longer asks
    for its own admin_password field.
    """
    from dashboard.app import app
    monkeypatch.setitem(app.config, "TESTING", True)
    monkeypatch.setattr(config, "ADMIN_DELETE_PASSWORD", "unit-test-admin-password")
    client = app.test_client()

    db.init()
    iid = db.add_influencer("Delete Protect Test", "deltest-21")

    # No admin_password is required any more — the shared unlock window already
    # confirmed the operator before this removal form was allowed through.
    resp = client.post(f"/influencer/{iid}/delete", data={})
    assert resp.status_code == 302
    assert db.get_influencer(iid) is None
