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


def test_delete_influencer_security_password():
    """Verify that delete influencer route enforces admin password."""
    from dashboard.app import app
    client = app.test_client()

    db.init()
    iid = db.add_influencer("Delete Protect Test", "deltest-21")

    # Attempt delete with wrong password
    resp_wrong = client.post(f"/influencer/{iid}/delete", data={"admin_password": "wrongpassword"})
    assert resp_wrong.status_code == 302
    assert "err=invalid_password" in resp_wrong.headers["Location"]
    assert db.get_influencer(iid) is not None  # Not deleted!

    # Attempt delete with correct password
    resp_correct = client.post(f"/influencer/{iid}/delete", data={"admin_password": config.ADMIN_DELETE_PASSWORD})
    assert resp_correct.status_code == 302
    assert db.get_influencer(iid) is None  # Successfully deleted!
