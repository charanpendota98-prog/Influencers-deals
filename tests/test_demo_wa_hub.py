"""The rehearsal kit must speak the same language as the real WhatsApp hub.

`tools/demo_wa_hub.py` exists so the operator can click the whole pairing flow
("scan or code → pick the channel where I am admin → it saves with its name →
posts flow") **without** logging a real WhatsApp number in, and without risking
the number. That is only useful while it stays honest: same JSON keys as
`wa_manager.js`/`server.js`, same soft-fail behaviour, and the picker must never
show a chat the account is not an admin of.

These tests drive the real client (`influencer_hub.whatsapp_client`) against the
stand-in over HTTP, so a drift between the two is caught here instead of during
a live demo.
"""
from __future__ import annotations

import asyncio

import pytest

from influencer_hub import config, db, pipeline, sources, whatsapp_client

from tools import demo_wa_hub


@pytest.fixture()
def hub(monkeypatch):
    """The stand-in, reachable exactly the way the dashboard reaches the real one."""
    token = "demo-token-not-a-real-secret-000000000000"
    server, url = demo_wa_hub.make_server(port=0, token=token)
    monkeypatch.setattr(config, "WA_HUB_URL", url)
    monkeypatch.setattr(config, "WA_HUB_TOKEN", token)
    demo_wa_hub.SENT.clear()
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# 1. The client speaks to it
# --------------------------------------------------------------------------
def test_the_pairing_status_reads_back_as_connected(hub):
    status = asyncio.run(whatsapp_client.session_status("inf-1-demo"))
    assert status["status"] == "connected"
    assert status["phone"] == demo_wa_hub.DEMO_PHONE


def test_the_admin_list_is_the_only_thing_the_picker_gets(hub):
    """Names, roles and sizes arrive; the followed-but-not-admin chat does not."""
    chats = asyncio.run(whatsapp_client.list_admin_chats("inf-1-demo"))
    names = [chat["name"] for chat in chats]
    assert names == ["Ravi Loot Deals", "Loot Zone Premium", "Family Loot Group"]
    assert "Somebody Else's Channel" not in names
    assert {chat["role"] for chat in chats} <= {"owner", "admin", "superadmin"}
    assert all(chat["jid"].endswith(("@newsletter", "@g.us")) for chat in chats)
    assert all(isinstance(chat["members"], int) for chat in chats)


def test_the_plain_chat_list_still_has_everything(hub):
    """The fallback list (invite links, plain JIDs) is unaffected by the filter."""
    chats = asyncio.run(whatsapp_client.list_chats("inf-1-demo"))
    assert len(chats) == len(demo_wa_hub.ALL_CHATS)


def test_a_send_is_accepted_and_logged(hub):
    result = asyncio.run(whatsapp_client.send_text(
        "inf-1-demo", "120363002222222222@newsletter", "hello from the rehearsal"))
    assert result.get("ok") is True
    assert demo_wa_hub.SENT[-1]["to"] == "120363002222222222@newsletter"
    assert "hello from the rehearsal" in demo_wa_hub.SENT[-1]["text"]


# --------------------------------------------------------------------------
# 2. Fail-soft, never fail-loud, never leak
# --------------------------------------------------------------------------
def test_a_wrong_token_yields_an_empty_picker_not_an_error(monkeypatch):
    """A misconfigured token must not break the page — and must not list chats."""
    server, url = demo_wa_hub.make_server(port=0, token="the-real-token")
    monkeypatch.setattr(config, "WA_HUB_URL", url)
    monkeypatch.setattr(config, "WA_HUB_TOKEN", "a-different-token")
    try:
        assert asyncio.run(whatsapp_client.list_admin_chats("inf-1-demo")) == []
    finally:
        server.shutdown()
        server.server_close()


def test_a_hub_that_is_down_yields_an_empty_picker(monkeypatch):
    """Port 1 on loopback: nothing listens, the operator still gets their page."""
    monkeypatch.setattr(config, "WA_HUB_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(config, "WA_HUB_TOKEN", "irrelevant")
    assert asyncio.run(whatsapp_client.list_admin_chats("inf-1-demo")) == []


# --------------------------------------------------------------------------
# 3. End to end: save the channel by name, then a deal lands in it
# --------------------------------------------------------------------------
def test_a_deal_reaches_the_channel_that_was_saved_from_the_picker(hub, monkeypatch, tmp_path):
    """The point of the whole feature: pick by name → save → posts keep flowing."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "demo.sqlite3")
    db.init()
    inf_id = db.add_influencer("Rehearsal Ravi", "rehearsal-21")
    jid, name = "120363002222222222@newsletter", "Loot Zone Premium"
    channel_id = db.add_channel(inf_id, "whatsapp", jid, role="whatsapp",
                                status="ready", title=name, mirror_whatsapp="1")
    channel = db.get_channel(inf_id, "whatsapp")
    assert (channel["identifier"], channel["title"], channel["mirror_whatsapp"]) == (
        jid, name, "1")

    db.update_influencer(inf_id,
                        allowed_sources=sources.normalize_selection("LootZoneIndia11"),
                        price_filter="", categories="", posting_schedule="")
    sent: list[str] = []

    async def fake_send(session_key, to_jid, text):
        sent.append(text)
        return {"ok": True}

    monkeypatch.setattr(whatsapp_client, "send_text", fake_send)
    monkeypatch.setattr(pipeline, "WA_SEND_GAP_WAIT", False, raising=False)
    deal = ("🔥 BOAT AIRDOPES 141 at ₹1299 (MRP ₹2999)\n"
            "https://www.amazon.in/dp/B09N3ZNHTY\n")
    inf = db.get_influencer(inf_id)
    result = asyncio.run(pipeline.dispatch_to_channel(inf, channel, deal))
    assert result == "posted", result
    assert sent, "the fake hub saw nothing"


def test_the_tools_page_is_not_a_service_and_talks_to_nobody():
    """Guard rail: the stand-in must not be wired into any real service unit."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for unit in (root / "deploy").glob("*.service"):
        text = unit.read_text(encoding="utf-8")
        assert "demo_wa_hub" not in text, f"{unit.name} starts the demo stand-in"
    source = (root / "tools" / "demo_wa_hub.py").read_text(encoding="utf-8")
    # It listens; it never dials out — so it cannot accidentally reach WhatsApp.
    assert "socket.create_connection" not in source
    assert "aiohttp" not in source
