import asyncio
from unittest.mock import patch
import pytest

from influencer_hub import db, pipeline, link_router as lr, bitly_client


def test_multi_key_bitly_support():
    """Verify that multiple influencers with different Bitly keys use their own key,
    and influencers without Bitly keys post normal links without shortening."""
    db.init()
    iid1 = db.add_influencer("Influencer One", "tag1-21", bitly_api_key="token-one")
    iid2 = db.add_influencer("Influencer Two", "tag2-21", bitly_api_key="")  # no key
    iid3 = db.add_influencer("Influencer Three", "tag3-21", bitly_api_key="token-three")

    # Add custom channel bitly override
    c1 = db.add_channel(iid1, "telegram", "@chan1", role="broadcast", status="ready")
    c2 = db.add_channel(iid2, "telegram", "@chan2", role="broadcast", status="ready")
    c3 = db.add_channel(iid3, "telegram", "@chan3", role="broadcast", status="ready", bitly_api_key="token-channel-override")

    inf1 = db.get_influencer(iid1)
    inf2 = db.get_influencer(iid2)
    inf3 = db.get_influencer(iid3)

    assert inf1["bitly_api_key"] == "token-one"
    assert inf2["bitly_api_key"] == ""
    assert inf3["bitly_api_key"] == "token-three"

    chan3 = [c for c in db.list_channels(iid3) if c["id"] == c3][0]
    assert chan3["bitly_api_key"] == "token-channel-override"

    # Cleanup
    db.delete_influencer(iid1)
    db.delete_influencer(iid2)
    db.delete_influencer(iid3)


def test_custom_wa_session_per_channel():
    """Verify that adding an extra WhatsApp channel with a separate phone number/session key
    dispatches to that specific session key."""
    db.init()
    iid = db.add_influencer("Multi WA Partner", "partner-21")
    # Primary channel uses default session
    c_primary = db.add_channel(iid, "whatsapp_group", "120363001@g.us", role="whatsapp", status="ready")
    # Secondary channel on second mobile phone uses dedicated custom session
    c_secondary = db.add_channel(
        iid, "whatsapp_group", "120363002@g.us", role="whatsapp", status="ready",
        wa_session_key="inf-partner-phone2-wa"
    )

    chs = db.list_channels(iid)
    ch1 = next(c for c in chs if c["id"] == c_primary)
    ch2 = next(c for c in chs if c["id"] == c_secondary)

    assert ch1["wa_session_key"] == ""
    assert ch2["wa_session_key"] == "inf-partner-phone2-wa"

    # Verify dispatch resolution
    sent_sessions = []

    async def fake_send_text(session_key, jid, text):
        sent_sessions.append((session_key, jid))
        return {"ok": True}

    inf = db.get_influencer(iid)
    with patch("influencer_hub.whatsapp_client.send_text", side_effect=fake_send_text), \
         patch("asyncio.sleep", return_value=None):
        res1 = asyncio.run(pipeline.dispatch_to_channel(inf, ch1, "Deal 1"))
        res2 = asyncio.run(pipeline.dispatch_to_channel(inf, ch2, "Deal 2"))

    assert res1 == "posted"
    assert res2 == "posted"
    # Primary used default influencer session
    assert sent_sessions[0] == (pipeline.WA_SESSION_KEY(iid), "120363001@g.us")
    # Secondary used custom second phone session
    assert sent_sessions[1] == ("inf-partner-phone2-wa", "120363002@g.us")

    db.delete_influencer(iid)


def test_pipeline_uses_influencer_bitly_key_for_multilink():
    """Ensure pipeline passes the influencer's specific Bitly token when post has 2+ links."""
    db.init()
    iid = db.add_influencer("Bitly VIP", "vip-21", bitly_api_key="vip-secret-bitly-token")
    cid = db.add_channel(iid, "telegram", "@viploots", role="broadcast", status="ready")

    deal_multilink = (
        "🔥 Combo Offer\n"
        "Item 1: https://www.amazon.in/dp/B081111111\n"
        "Item 2: https://www.amazon.in/dp/B082222222"
    )

    shortened_tokens_used = []

    async def fake_shorten_urls(urls, token=None):
        shortened_tokens_used.append(token)
        return {u: f"https://bit.ly/{u[-6:]}" for u in urls}

    with patch("influencer_hub.bitly_client.shorten_urls", side_effect=fake_shorten_urls), \
         patch("influencer_hub.telegram_ops.post_to_channel", return_value=None):
        res = asyncio.run(pipeline.render_and_dispatch(deal_multilink, influencer_ids=[iid]))

    assert res[iid][cid] == "posted"
    assert "vip-secret-bitly-token" in shortened_tokens_used

    db.delete_influencer(iid)


def test_pipeline_skips_bitly_when_influencer_has_no_key():
    """If influencer hasn't provided a Bitly key, standard affiliate links are used without Bitly."""
    db.init()
    iid = db.add_influencer("No Bitly User", "nobitly-21", bitly_api_key="")
    cid = db.add_channel(iid, "telegram", "@nobitly", role="broadcast", status="ready")

    deal_multilink = (
        "🔥 Combo Offer\n"
        "Item 1: https://www.amazon.in/dp/B081111111\n"
        "Item 2: https://www.amazon.in/dp/B082222222"
    )

    shorten_called = False

    async def fake_shorten_urls(urls, token=None):
        nonlocal shorten_called
        shorten_called = True
        return {}

    with patch("influencer_hub.bitly_client.shorten_urls", side_effect=fake_shorten_urls), \
         patch("influencer_hub.telegram_ops.post_to_channel", return_value=None):
        res = asyncio.run(pipeline.render_and_dispatch(deal_multilink, influencer_ids=[iid]))

    assert res[iid][cid] == "posted"
    # Never called shorten_urls because user didn't provide a key!
    assert shorten_called is False

    db.delete_influencer(iid)
