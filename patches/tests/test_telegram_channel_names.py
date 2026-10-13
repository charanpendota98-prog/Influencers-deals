"""A Telegram destination gets the same smart handling as the WhatsApp one.

The operator asked for both messengers to behave alike: "WhatsApp channel ni
admin ga unna channel ni isthanu adi save cheskovali … Telegram channel ki kuda
same smart handling … channel names dropdown lo chupinchali".

So a Telegram channel pasted as a link or a JID is now **peeked at, read-only**:
Telegram says what the destination is called and whether this account may post
there, and the name is stored on the channel (so the creator page and every list
show `Ravi Loot Deals`, not `-100123…`). A peek that cannot happen — no session
on the box, Telegram unreachable, a private invite we are not in — must never
block the add or raise at the operator: what they pasted is saved as pasted.

Nothing here joins a group or posts anything, and the peek is asserted to be
read-only.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from influencer_hub import config, db, telegram_ops

from telethon.tl.types import Channel as TgChannel, Chat as TgChat


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _FakePermissions:
    def __init__(self, is_admin=True, is_creator=False, post_messages=True,
                 send_messages=False):
        self.is_admin = is_admin
        self.is_creator = is_creator
        self.post_messages = post_messages
        self.send_messages = send_messages


class _FakeClient:
    """The smallest thing `inspect_channel` talks to."""

    def __init__(self, entity, permissions=None, delay=0.0):
        self.entity = entity
        self.permissions = permissions or _FakePermissions()
        self.delay = delay
        self.calls: list[str] = []

    def is_connected(self) -> bool:
        return True

    async def get_entity(self, identifier):
        self.calls.append("get_entity")
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.entity

    async def get_permissions(self, entity):
        self.calls.append("get_permissions")
        return self.permissions


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "names.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "owner-password-16chars")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_COOKIE_SECURE", False)
    db.init()
    import dashboard.app as dashboard_module

    monkeypatch.setattr(dashboard_module.app, "testing", False)
    dashboard_module.app.config.update(TESTING=False, WTF_CSRF_ENABLED=True)
    monkeypatch.setattr(dashboard_module, "_dashboard_secret_key_is_persistent",
                        lambda: True)
    with dashboard_module.app.test_client() as test_client:
        page = test_client.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)
        test_client.post("/login", data={"_csrf_token": token,
                                        "password": "owner-password-16chars"})
        test_client.dashboard_module = dashboard_module
        yield test_client
        test_client.get("/logout")


def _csrf(client, url="/setup"):
    page = client.get(url).get_data(as_text=True)
    return re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)


# --------------------------------------------------------------------------
# 1. the peek itself
# --------------------------------------------------------------------------
def test_a_channel_comes_back_with_its_real_name(monkeypatch):
    entity = TgChannel(id=3333333333, title="Ravi Loot Deals", photo=None, date=None,
                       creator=True, broadcast=True, username="raviloots",
                       admin_rights=None, left=False, megagroup=False)
    fake = _FakeClient(entity)
    monkeypatch.setattr(telegram_ops, "_client", lambda: fake)

    info = asyncio.run(telegram_ops.inspect_channel("@raviloots"))
    assert info["ok"] is True
    assert info["title"] == "Ravi Loot Deals"
    assert info["username"] == "raviloots"
    assert info["kind"] == "channel"
    assert info["can_post"] is True and info["is_admin"] is True
    # read-only: it looked, it never posted or joined
    assert fake.calls == ["get_entity", "get_permissions"]


def test_a_group_without_admin_rights_says_so(monkeypatch):
    entity = TgChat(id=4444, title="Deals Group", photo=None, date=None, version=1,
                    creator=False, left=False, participants_count=42)
    fake = _FakeClient(entity, permissions=_FakePermissions(
        is_admin=False, post_messages=False, send_messages=True))
    monkeypatch.setattr(telegram_ops, "_client", lambda: fake)

    info = asyncio.run(telegram_ops.inspect_channel("4444"))
    assert info["ok"] is True and info["kind"] == "group"
    assert info["can_post"] is True, "a chat where we may send is postable"
    assert info["is_admin"] is False


def test_a_silent_telegram_does_not_hang_the_add(monkeypatch):
    """A slow network comes back as a reason, not as a stuck dashboard."""
    entity = TgChannel(id=1, title="Slow", photo=None, date=None, creator=True,
                       broadcast=True, username="", admin_rights=None, left=False,
                       megagroup=False)
    monkeypatch.setattr(telegram_ops, "_client", lambda: _FakeClient(entity, delay=5))

    info = asyncio.run(telegram_ops.inspect_channel("@slow", timeout=0.05))
    assert info["ok"] is False
    assert "did not answer" in info["error"]


def test_an_unreachable_telegram_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set")

    monkeypatch.setattr(telegram_ops, "_client", _boom)
    info = asyncio.run(telegram_ops.inspect_channel("@ravi"))
    assert info["ok"] is False
    assert "TELEGRAM_API_ID" in info["error"]

    assert asyncio.run(telegram_ops.inspect_channel(""))["ok"] is False


def test_the_session_check_is_filesystem_only(monkeypatch, tmp_path):
    """No session file → no network attempt at all."""
    monkeypatch.setattr(config, "TELEGRAM_SESSION", str(tmp_path / "absent"))
    assert telegram_ops.session_ready() is False

    session = tmp_path / "present.session"
    session.write_bytes(b"sqlite-ish")
    monkeypatch.setattr(config, "TELEGRAM_SESSION", str(session))
    assert telegram_ops.session_ready() is True


# --------------------------------------------------------------------------
# 2. the dashboard stores the name
# --------------------------------------------------------------------------
def test_adding_a_telegram_channel_stores_the_name_it_reports(client, monkeypatch):
    module = client.dashboard_module
    monkeypatch.setattr(module, "_telegram_destination_info",
                        lambda ident: {"ok": True, "title": "Ravi Loot Deals",
                                       "kind": "channel", "can_post": True})
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")

    client.post(f"/influencer/{inf_id}/add-manual-channel", data={
        "_csrf_token": _csrf(client, f"/influencer/{inf_id}"),
        "platform": "telegram", "identifier": "@raviloots", "role": "broadcast",
    })

    channel = db.get_channel(inf_id, "broadcast")
    assert channel["identifier"] == "@raviloots"
    assert channel["title"] == "Ravi Loot Deals"
    page = client.get(f"/influencer/{inf_id}").get_data(as_text=True)
    assert "Ravi Loot Deals" in page


def test_a_destination_we_cannot_post_to_is_saved_with_a_warning(client, monkeypatch):
    """The operator's real question: "am I admin here?" — answered, not guessed."""
    module = client.dashboard_module
    monkeypatch.setattr(module, "_telegram_destination_info",
                        lambda ident: {"ok": True, "title": "Someone Else Deals",
                                       "kind": "channel", "can_post": False})
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    client.post(f"/influencer/{inf_id}/add-manual-channel", data={
        "_csrf_token": _csrf(client, f"/influencer/{inf_id}"),
        "platform": "telegram", "identifier": "@theirs", "role": "broadcast",
    })
    channel = db.get_channel(inf_id, "broadcast")
    assert channel["title"] == "Someone Else Deals"
    assert channel["status"] == "ready", "it is still saved — approval use is fine"
    page = client.get(f"/influencer/{inf_id}").get_data(as_text=True)
    assert "cannot post there yet" in page


def test_without_a_session_the_old_behaviour_is_unchanged(client, monkeypatch):
    """A fresh box has no Telegram session: the add must still work, quietly."""
    module = client.dashboard_module
    monkeypatch.setattr(module.telegram_ops, "session_ready", lambda: False)
    monkeypatch.setattr(
        module.telegram_ops, "inspect_channel",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    client.post(f"/influencer/{inf_id}/add-manual-channel", data={
        "_csrf_token": _csrf(client, f"/influencer/{inf_id}"),
        "platform": "telegram", "identifier": "@raviloots", "role": "broadcast",
    })
    channel = db.get_channel(inf_id, "broadcast")
    assert channel["identifier"] == "@raviloots" and channel["title"] == ""


def test_the_one_screen_add_names_both_telegram_channels(client, monkeypatch):
    module = client.dashboard_module
    names = {"@ravi_amazon": "Ravi Approval", "@ravi_loots": "Ravi Loot Deals"}

    def _info(ident):
        title = names.get(ident)
        if not title:
            return {"ok": False, "error": "unknown"}
        return {"ok": True, "title": title, "kind": "channel",
                "can_post": ident != "@ravi_amazon"}

    monkeypatch.setattr(module, "_telegram_destination_info", _info)
    client.post("/quick-add", data={
        "_csrf_token": _csrf(client, "/setup"),
        "name": "Ravi Loots", "tag": "ravi-21",
        "approval_tg": "@ravi_amazon", "broadcast_tg": "@ravi_loots",
    })
    ravi = next(row for row in db.list_influencers() if row["name"] == "Ravi Loots")
    titles = {c["role"]: c["title"] for c in db.list_channels(ravi["id"])}
    assert titles == {"approval": "Ravi Approval", "broadcast": "Ravi Loot Deals"}
    page = client.get(f"/influencer/{ravi['id']}").get_data(as_text=True)
    assert "Ravi Approval" in page and "Ravi Loot Deals" in page
    assert "cannot post there yet" in page, "the approval-only rights are reported"


# --------------------------------------------------------------------------
# 3. the backfill command
# --------------------------------------------------------------------------
def test_channel_names_is_read_only_until_apply(monkeypatch, capsys, tmp_path):
    from influencer_hub import cli

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "names.sqlite3")
    db.init()
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    db.add_channel(inf_id, "telegram", "@raviloots", role="broadcast", status="ready")
    db.add_channel(inf_id, "telegram", "@named_already", role="approval", status="ready",
                   title="Already Named")

    monkeypatch.setattr(telegram_ops, "session_ready", lambda: True)

    async def _fake(identifier, timeout=8.0):
        return {"ok": True, "title": f"Real {identifier}", "kind": "channel",
                "can_post": identifier != "@raviloots"}

    monkeypatch.setattr(telegram_ops, "inspect_channel", _fake)

    assert cli.main(["channel-names"]) == 0
    out = capsys.readouterr().out
    assert "Real @raviloots" in out and "NOT an admin here" in out
    assert "nothing was changed" in out
    assert db.get_channel(inf_id, "broadcast")["title"] == ""
    assert "Already Named" not in out, "named channels are skipped unless --all"

    assert cli.main(["channel-names", "--apply"]) == 0
    assert db.get_channel(inf_id, "broadcast")["title"] == "Real @raviloots"
    assert db.get_channel(inf_id, "approval")["title"] == "Already Named"


def test_channel_names_says_what_is_missing_without_a_session(monkeypatch, capsys, tmp_path):
    from influencer_hub import cli

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "names.sqlite3")
    monkeypatch.setattr(config, "TELEGRAM_SESSION", str(tmp_path / "absent"))
    db.init()
    assert cli.main(["channel-names"]) == 2
    out = capsys.readouterr().out
    assert "no Telegram session" in out
    assert "saved exactly as pasted" in out
