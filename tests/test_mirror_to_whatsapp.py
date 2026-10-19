"""Main Telegram channel -> creator's WhatsApp channel mirror.

The operator's rule: "valla main channel lo vachinavi WhatsApp channel lo
raval" — a deal that really went out on the creator's main Telegram channel also
lands on their WhatsApp Channel, rendered for WhatsApp and signed with the
creator's own Amazon tag. This module pins the rules that make that safe:

* the mirror only copies a post that the main channel actually delivered;
* a destination that already received the deal is never written twice;
* the WhatsApp destination's own affiliate policy still decides what may appear
  there (Amazon-only stays Amazon-only, strip-Amazon stays stripped);
* the creator's own tag is used — a channel override wins, the creator tag is
  the default, and another creator's tag can never appear;
* the mirror is switchable (per main channel and globally) and is reported
  honestly by ``pipeline.mirror_plan``.
"""
from __future__ import annotations

import asyncio

import pytest

from influencer_hub import accounts, config, db, pipeline, whatsapp_client

AMAZON_DEAL = "🔥 Boat Airdopes 141 at ₹1299 https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21"
WA_CHANNEL_JID = "120363012345678901@newsletter"


@pytest.fixture
def wired(monkeypatch):
    """A creator with a ready Telegram main channel and a ready WhatsApp channel."""
    monkeypatch.setattr(pipeline, "apply_whatsapp_safety_pacing", lambda _key: _no_wait())

    posted: list[tuple[str, str]] = []
    sent: list[tuple[str, str, str]] = []

    async def fake_post_to_channel(identifier, text, *args, **kwargs):
        posted.append((identifier, text))

    async def fake_send_text(session_key, to_jid, text):
        sent.append((session_key, to_jid, text))
        return {"ok": True}

    monkeypatch.setattr("influencer_hub.telegram_ops.post_to_channel", fake_post_to_channel)
    monkeypatch.setattr(whatsapp_client, "send_text", fake_send_text)
    return {"posted": posted, "sent": sent}


async def _no_wait():
    return None


def _creator(name="Mirror Creator", tag="mirrorcreator-21", **inf_kwargs) -> int:
    inf_id = db.add_influencer(name, tag, **inf_kwargs)
    return inf_id


def _channels(inf_id: int, *, wa_kwargs=None, tg_kwargs=None) -> tuple[int, int]:
    tg_id = db.add_channel(
        inf_id, "telegram", "@mirror_main", status="ready", role="broadcast",
        **(tg_kwargs or {}),
    )
    wa_id = db.add_channel(
        inf_id, "whatsapp_channel", WA_CHANNEL_JID, status="ready", role="whatsapp",
        **(wa_kwargs or {}),
    )
    return tg_id, wa_id


def test_main_channel_post_is_mirrored_to_the_creators_whatsapp_channel(wired):
    inf_id = _creator()
    # The WhatsApp channel only wants one specific source, so the direct pass
    # filters the deal out — the mirror is what carries it over from main.
    tg_id, wa_id = _channels(inf_id, wa_kwargs={"allowed_sources": "some_other_source"})

    result = asyncio.run(
        pipeline.render_and_dispatch(AMAZON_DEAL, influencer_ids=[inf_id],
                                     source_channel="loot_source")
    )

    assert result[inf_id][tg_id] == "posted"
    assert result[inf_id][wa_id] == "posted"
    assert len(wired["posted"]) == 1
    assert len(wired["sent"]) == 1
    session_key, to_jid, text = wired["sent"][0]
    assert session_key == f"inf-{inf_id}-wa"
    assert to_jid == WA_CHANNEL_JID
    # Creator's own Associate tag signs the mirrored post; the source tag is gone.
    assert "tag=mirrorcreator-21" in text
    assert "someoneelse-21" not in text
    # WhatsApp is not HTML: the post must not carry Telegram markup.
    assert "<b>" not in text and "</b>" not in text

    # The delivery is recorded as a mirror of the main channel, and counted.
    assert db.mirrored_post_count(wa_id, hours=24) == 1
    assert db.mirrored_post_count(tg_id, hours=24) == 0
    plan = pipeline.mirror_plan(db.get_influencer(inf_id))
    assert plan["enabled"] is True
    assert plan["active"] is True
    assert [src["id"] for src in plan["sources"]] == [tg_id]
    assert [dst["id"] for dst in plan["targets"]] == [wa_id]
    assert plan["mirrored_posts_24h"] == 1


def test_a_deal_the_whatsapp_channel_already_posted_is_not_mirrored_twice(wired):
    inf_id = _creator()
    _tg_id, wa_id = _channels(inf_id)

    result = asyncio.run(pipeline.render_and_dispatch(AMAZON_DEAL, influencer_ids=[inf_id]))

    # Direct delivery already reached WhatsApp; the mirror must add nothing.
    assert result[inf_id][wa_id] == "posted"
    assert len(wired["sent"]) == 1
    assert db.mirrored_post_count(wa_id, hours=24) == 0


def test_mirror_is_switchable_per_main_channel_and_globally(wired):
    inf_id = _creator()
    _tg_id, wa_id = _channels(
        inf_id,
        tg_kwargs={"mirror_whatsapp": "0"},
        wa_kwargs={"allowed_sources": "some_other_source"},
    )

    off = asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[inf_id], source_channel="loot_source",
    ))
    assert off[inf_id][wa_id] == "skipped"
    assert wired["sent"] == []

    # The creator's own opt-out is visible on the plan instead of being silent.
    plan = pipeline.mirror_plan(db.get_influencer(inf_id))
    assert plan["enabled"] is False
    assert [src["identifier"] for src in plan["opted_out"]] == ["@mirror_main"]

    # An explicit channel switch outranks a global switch that was turned off.
    other = _creator("Global Off Creator", "globaloff-21")
    db.set_global_setting("mirror_main_to_whatsapp", "0")
    other_tg = db.add_channel(other, "telegram", "@mirror_main2", status="ready", role="broadcast",
                              mirror_whatsapp="1")
    other_wa = db.add_channel(other, "whatsapp_channel", "120363099999999999@newsletter",
                              status="ready", role="whatsapp",
                              allowed_sources="some_other_source")

    result = asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[other], source_channel="loot_source",
    ))
    assert result[other][other_tg] == "posted"
    assert result[other][other_wa] == "posted"
    assert [to_jid for _key, to_jid, _text in wired["sent"]] == ["120363099999999999@newsletter"]

    # With the global switch off and no channel override, mirroring stops.
    third = _creator("Inherit Off Creator", "inheritoff-21")
    _t, w = _channels(third, wa_kwargs={"allowed_sources": "some_other_source"})
    result = asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[third], source_channel="loot_source",
    ))
    assert result[third][w] == "skipped"
    assert len(wired["sent"]) == 1
    db.set_global_setting("mirror_main_to_whatsapp", "1")


def test_mirror_respects_the_destination_amazon_only_policy(wired):
    inf_id = _creator("Amazon Only Creator", "amazononly-21")
    flipped_deal = "📱 Realme Narzo at ₹8999 https://www.flipkart.com/realme-narzo/p/itm123"
    _tg_id, wa_id = _channels(
        inf_id,
        wa_kwargs={"only_amazon": True, "allowed_sources": "some_other_source"},
    )

    async def fake_convert(urls, include_meesho=False):
        publisher_id = str(
            db.get_global_setting("earnkaro_publisher_id", config.EARNKARO_PUBLISHER_ID)
            or config.EARNKARO_PUBLISHER_ID
        )
        converted = {}
        for url in urls:
            short = f"https://ekaro.in/mirror?affExtParam2={publisher_id}"
            db.record_verified_earnkaro_link(short, publisher_id, url)
            converted[url] = short
        return converted

    monkeypatch_convert = pytest.MonkeyPatch()
    monkeypatch_convert.setattr("influencer_hub.earnkaro.convert_links", fake_convert)
    try:
        result = asyncio.run(
            pipeline.render_and_dispatch(flipped_deal, influencer_ids=[inf_id])
        )
    finally:
        monkeypatch_convert.undo()

    assert result[inf_id][_tg_id] == "posted"
    # The WhatsApp destination is Amazon-only, so the mirrored deal is refused
    # there instead of publishing a non-Amazon link.
    assert result[inf_id][wa_id] == "skipped"
    assert wired["sent"] == []


def test_mirror_uses_the_whatsapp_channel_override_tag_when_set(wired):
    inf_id = _creator("Override Creator", "overridecreator-21")
    _tg_id, wa_id = _channels(
        inf_id,
        wa_kwargs={"amazon_override_tag": "wachannel-99",
                   "allowed_sources": "some_other_source"},
    )

    asyncio.run(pipeline.render_and_dispatch(AMAZON_DEAL, influencer_ids=[inf_id]))

    assert len(wired["sent"]) == 1
    mirror_text = wired["sent"][0][2]
    assert "tag=wachannel-99" in mirror_text
    assert "overridecreator-21" not in mirror_text
    assert accounts.creator_amazon_tag(db.get_influencer(inf_id),
                                      db.list_channels(inf_id)[1]) == "wachannel-99"


def test_mirror_waits_for_a_ready_whatsapp_destination(wired):
    inf_id = _creator("Pending WA Creator", "pendingwa-21")
    db.add_channel(inf_id, "telegram", "@mirror_main3", status="ready", role="broadcast")
    db.add_channel(inf_id, "whatsapp_channel", WA_CHANNEL_JID, status="pending", role="whatsapp")

    result = asyncio.run(pipeline.render_and_dispatch(AMAZON_DEAL, influencer_ids=[inf_id]))

    assert "posted" in result[inf_id].values()
    assert wired["sent"] == []
    plan = pipeline.mirror_plan(db.get_influencer(inf_id))
    assert plan["active"] is False
    assert plan["targets"] == []


def test_mirrored_post_uses_the_same_rendering_path_as_a_direct_post(wired):
    """A mirror is not a second-class renderer: identical inputs render identically.

    This is the guard against the classic mirror bug — a copied post that skips
    the shortener/commission-guard stages a direct post goes through.
    """
    direct_id = _creator("Direct Creator", "same-21")
    db.add_channel(direct_id, "whatsapp_channel", "120363011111111111@newsletter",
                   status="ready", role="whatsapp")
    mirrored_id = _creator("Mirrored Creator", "same-21")
    _tg_id, _wa_id = _channels(
        mirrored_id, wa_kwargs={"allowed_sources": "some_other_source"})
    # Give the mirrored side the same WhatsApp destination config as the direct one.
    db.update_channel_details(
        _wa_id, identifier="120363022222222222@newsletter", allowed_sources="some_other_source")

    asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[direct_id], source_channel="loot_source"))
    asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[mirrored_id], source_channel="loot_source"))

    direct_text = next(text for key, _to, text in wired["sent"] if key == f"inf-{direct_id}-wa")
    mirror_text = next(text for key, _to, text in wired["sent"] if key == f"inf-{mirrored_id}-wa")
    assert mirror_text == direct_text

    # And the mirrored text passes the same commission audit as a direct post.
    from influencer_hub import commission_guard

    audit = commission_guard.audit_rendered_text(mirror_text, "same-21", "")
    assert audit["ok"] is True, audit["issues"]


def test_mirror_never_breaks_a_whatsapp_failure_into_a_double_post(wired, monkeypatch):
    """A failed mirror is reported as failed; the main channel post stands."""
    inf_id = _creator("Failing WA Creator", "failingwa-21")
    _tg_id, wa_id = _channels(inf_id, wa_kwargs={"allowed_sources": "some_other_source"})

    async def failing_send(session_key, to_jid, text):
        raise RuntimeError("session not connected: " + session_key)

    monkeypatch.setattr(whatsapp_client, "send_text", failing_send)

    result = asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[inf_id], source_channel="loot_source",
    ))

    assert result[inf_id][_tg_id] == "posted"
    assert result[inf_id][wa_id].startswith("failed:")
    assert db.mirrored_post_count(wa_id, hours=24) == 0


def test_mirror_plan_is_honest_when_nothing_can_be_mirrored():
    inf_id = _creator("No WA Creator", "nowa-21")
    db.add_channel(inf_id, "telegram", "@only_main", status="ready", role="broadcast")

    plan = pipeline.mirror_plan(db.get_influencer(inf_id))

    assert plan["enabled"] is True
    assert plan["active"] is False
    assert plan["targets"] == []
    assert plan["mirrored_posts_24h"] == 0
    assert config.MIRROR_MAIN_TO_WHATSAPP is True
