"""Own-tag proof: every link a creator's channel posts carries *their* tag.

The operator's rule, in their words: "akkada tag lo em tag isthe valla channel
ah channels loney valla tag tho links cheyali … vallaki commission miss
avvakunda" — whatever tag is saved for a creator, the Amazon links posted on
that creator's channels (approval, main, and the WhatsApp mirror) carry exactly
that tag. A link signed with anybody else's id is a commission miss, and at
1000+ creators it must be caught by the machine, not by reading payout reports.

This module pins the rule end to end:

* :mod:`influencer_hub.tag_proof` reading the tag chain (channel override →
  creator tag → hub-wide fallback) and calling the fallback *not* an own tag;
* the pipeline gate: an Amazon deal for a creator without an own tag is HELD
  (``skipped:no_own_amazon_tag``), parked in the deferred queue with the reason
  and audited — nothing is posted under the shared id;
* the deliberate all-roles rule (an approval channel is held too);
* the escape hatch (``REQUIRE_OWN_AMAZON_TAG=off``) for a hub that really wants
  one shared account;
* the surfaces the owner checks: ``cli tag-proof``, the dashboard
  ``/api/tag-proof``, the self-check line and the fleet pass that scales.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from influencer_hub import config, db, link_router, pipeline, tag_proof, worker

AMAZON_DEAL = "🔥 Boat Airdopes at ₹1299 https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21"
FLIPKART_DEAL = "🔥 Shoes ₹499 https://www.flipkart.com/p/itm123"
SHARED = config.AMAZON_ASSOCIATE_TAG          # the hub-wide fallback (mama086-21)


# --------------------------------------------------------------------------
# 1. The tag chain, told plainly
# --------------------------------------------------------------------------
def _creator(name="Ravi Loots", tag="ravi-21", **kwargs) -> int:
    return db.add_influencer(name, tag, **kwargs)


def _channel(inf_id: int, channel_id: int) -> dict:
    """The channel row by id (``db.get_channel`` looks a channel up by role)."""
    return next(row for row in db.list_channels(inf_id) if int(row["id"]) == channel_id)


def test_a_creators_own_tag_signs_their_channel():
    inf_id = _creator()
    ch_id = db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready",
                           role="broadcast")
    profile = db.get_influencer(inf_id)
    channel = _channel(inf_id, ch_id)

    info = tag_proof.signing(profile, channel)
    assert (info["tag"], info["source"], info["own"], info["usable"]) == (
        "ravi-21", "creator", True, True)
    assert info["reason"] == "" and info["fallback_tag"] == SHARED

    deal_url = "https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21"
    link = tag_proof.tagged_link(profile, url=deal_url)
    assert link == "https://www.amazon.in/dp/B09N3ZNHTY?tag=ravi-21"
    proof = tag_proof.verify(link, profile, channel)
    assert proof["ok"] is True and proof["wrong"] == [] and proof["amazon_links"] == 1
    assert proof["tag"] == "ravi-21" and proof["own"] is True


def test_the_shared_default_is_not_an_own_tag():
    """A blank tag is stored as the hub-wide default — that is our id, not theirs."""
    inf_id = db.add_influencer("No Tag Yet", "")
    ch_id = db.add_channel(inf_id, "telegram", "@notag", status="ready",
                           role="broadcast")
    profile = db.get_influencer(inf_id)
    assert profile["amazon_tag"] == SHARED          # the database filled it in
    assert tag_proof.stored_tag(profile) == SHARED
    assert tag_proof.own_tag(profile) == ""         # …but it is not *theirs*

    info = tag_proof.signing(profile, _channel(inf_id, ch_id))
    assert info["own"] is False and info["shared_default"] is True
    # ``source`` says where the value lives (the creator row, filled in for us);
    # ``shared_default`` says it is the hub's id, not theirs — together they are
    # why the channel is held.
    assert info["source"] == "creator" and info["reason"] == tag_proof.HOLD_REASON
    assert info["tag"] == SHARED and info["usable"] is True

    proof = tag_proof.verify(tag_proof.tagged_link(profile), profile)
    assert proof["ok"] is False
    assert any("no own Amazon tag" in issue for issue in proof["issues"])


def test_a_channel_override_wins_and_is_judged_the_same_way():
    inf_id = _creator()
    own_override = db.add_channel(inf_id, "telegram", "@override", status="ready",
                                  role="broadcast", amazon_override_tag="second-21")
    shared_override = db.add_channel(inf_id, "telegram", "@shared", status="ready",
                                    role="broadcast", amazon_override_tag=SHARED)
    profile = db.get_influencer(inf_id)

    first = tag_proof.signing(profile, _channel(inf_id, own_override))
    assert (first["tag"], first["source"], first["own"]) == ("second-21", "channel", True)

    second = tag_proof.signing(profile, _channel(inf_id, shared_override))
    assert second["own"] is False and second["shared_default"] is True
    assert second["source"] == "channel"          # the override, judged not-own


def test_verify_catches_a_foreign_or_untagged_amazon_link():
    profile = db.get_influencer(_creator(name="Tag Owner", tag="tageowner-21"))
    rendered = (
        "Deal https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21\n"
        "and https://www.amazon.in/dp/B08XY2ABCD\n"
        "and https://www.flipkart.com/p/itm123 (not Amazon, not judged)"
    )
    proof = tag_proof.verify(rendered, profile)
    assert proof["ok"] is False and proof["amazon_links"] == 2
    assert [row["why"] for row in proof["wrong"]] == ["different_tag", "untagged"]
    assert proof["wrong"][0]["tag"] == "someoneelse-21"
    assert "someoneelse-21" in proof["issues"][0]

    tags = tag_proof.amazon_tags_in(rendered)
    assert list(tags.values()) == ["someoneelse-21", ""]
    assert tag_proof.deal_has_amazon(rendered) is True
    assert tag_proof.deal_has_amazon(FLIPKART_DEAL) is False


def test_url_summary_never_leaks_the_shortener_or_a_secret():
    link = tag_proof.tagged_link(db.get_influencer(_creator()))
    assert tag_proof.url_summary(link) == f"www.amazon.in/dp/{tag_proof.SAMPLE_ASIN}?tag=ravi-21"


# --------------------------------------------------------------------------
# 2. The gate itself: what is posted, what is held
# --------------------------------------------------------------------------
def test_holds_matrix():
    profile = db.get_influencer(_creator())
    ch_id = db.add_channel(profile["id"], "telegram", "@ravi_loots",
                           status="ready", role="broadcast")
    channel = _channel(profile["id"], ch_id)
    assert tag_proof.holds(profile, channel, AMAZON_DEAL)["hold"] is False

    # A deal with no Amazon link is none of this gate's business.
    assert tag_proof.holds(profile, channel, FLIPKART_DEAL)["hold"] is False

    bare = db.get_influencer(db.add_influencer("No Tag Yet", ""))
    decision = tag_proof.holds(bare, channel, AMAZON_DEAL)
    assert decision["hold"] is True
    assert decision["reason"] == tag_proof.HOLD_REASON
    assert decision["signing"]["own"] is False

    # An Amazon-only channel that strips Amazon links posts nothing Amazon at all.
    assert tag_proof.holds(bare, channel, AMAZON_DEAL, strip_amazon=True)["hold"] is False

    # The escape hatch: opting out restores the old fallback signing (an owner
    # decision, recorded once, never a default).
    original = config.REQUIRE_OWN_AMAZON_TAG
    try:
        config.REQUIRE_OWN_AMAZON_TAG = False
        assert tag_proof.holds(bare, channel, AMAZON_DEAL)["hold"] is False
    finally:
        config.REQUIRE_OWN_AMAZON_TAG = original


def _wired(monkeypatch):
    """A hub whose posts are captured instead of sent (the mirror test's shape)."""
    posted: list[tuple[str, str]] = []
    sent: list[tuple[str, str, str]] = []

    async def fake_post_to_channel(identifier, text, *args, **kwargs):
        posted.append((identifier, text))

    async def fake_send_text(session_key, to_jid, text):
        sent.append((session_key, to_jid, text))
        return {"ok": True}

    monkeypatch.setattr("influencer_hub.telegram_ops.post_to_channel", fake_post_to_channel)
    monkeypatch.setattr(pipeline.whatsapp_client, "send_text", fake_send_text)
    monkeypatch.setattr(pipeline, "apply_whatsapp_safety_pacing", lambda _key: _no_wait())
    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    return {"posted": posted, "sent": sent}


async def _no_wait():
    return None


def test_an_amazon_deal_is_held_when_the_creator_has_no_own_tag(monkeypatch):
    wired = _wired(monkeypatch)
    inf_id = db.add_influencer("No Tag Yet", "")
    ch_id = db.add_channel(inf_id, "telegram", "@notag", status="ready",
                           role="broadcast")

    result = asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[inf_id], source_channel="loot_source"))

    assert result[inf_id][ch_id] == f"skipped:{tag_proof.HOLD_REASON}"
    assert wired["posted"] == [] and wired["sent"] == []      # nothing went out
    queued = db.list_deferred_deals()
    assert [row["reason"] for row in queued] == [tag_proof.HOLD_REASON]
    assert queued[0]["source"] == "loot_source"
    assert SHARED not in queued[0]["text"] or "tag=" not in queued[0]["text"]

    logged = [row for row in db.list_audit(action="amazon_tag_hold")]
    assert len(logged) == 1
    assert logged[0]["actor"] == "pipeline" and "No Tag Yet" in logged[0]["target"]
    assert SHARED in logged[0]["detail"] or "shared default" in logged[0]["detail"]


def test_the_same_deal_posts_with_the_creators_own_tag(monkeypatch):
    wired = _wired(monkeypatch)
    inf_id = _creator(tag="ravi-21")
    ch_id = db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready",
                           role="broadcast")

    result = asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[inf_id], source_channel="loot_source"))

    assert result[inf_id][ch_id] == "posted"
    text = wired["posted"][0][1]
    assert "tag=ravi-21" in text
    assert "tag=someoneelse-21" not in text and f"tag={SHARED}" not in text
    assert db.list_deferred_deals() == []


def test_every_channel_of_the_creator_gets_the_same_own_tag(monkeypatch):
    """One tag saved once: approval, main and the WhatsApp mirror all carry it."""
    wired = _wired(monkeypatch)
    inf_id = _creator(tag="ravi-21")
    approval = db.add_channel(inf_id, "telegram", "@ravi_approve", status="ready",
                              role="approval")
    main = db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready",
                          role="broadcast")
    wa = db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                        status="ready", role="whatsapp")

    asyncio.run(pipeline.render_and_dispatch(AMAZON_DEAL, influencer_ids=[inf_id],
                                             source_channel="loot_source"))

    posted = dict(wired["posted"])
    assert set(posted) == {"@ravi_approve", "@ravi_loots"}      # both Telegram roles
    for text in posted.values():
        assert "tag=ravi-21" in text
    assert wired["sent"], "the main channel's post is mirrored to WhatsApp"
    for _session, _jid, text in wired["sent"]:
        assert "tag=ravi-21" in text


def test_an_approval_channel_is_held_too_by_design(monkeypatch):
    """Deliberate all-roles rule: the approval copy is a post in that creator's channel.

    The commission guard exempts approval channels; this gate does not, because
    the link in the approval copy is the one the owner is asked to approve — a
    wrongly-signed link there is one click from going out to followers.
    """
    _wired(monkeypatch)
    inf_id = db.add_influencer("No Tag Yet", "")
    approval = db.add_channel(inf_id, "telegram", "@notag_approve", status="ready",
                              role="approval")

    result = asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[inf_id], source_channel="loot_source"))

    assert result[inf_id][approval] == f"skipped:{tag_proof.HOLD_REASON}"
    assert db.count_deferred_deals() == 1


def test_off_switch_restores_fallback_signing(monkeypatch):
    wired = _wired(monkeypatch)
    monkeypatch.setattr(config, "REQUIRE_OWN_AMAZON_TAG", False)
    inf_id = db.add_influencer("No Tag Yet", "")
    ch_id = db.add_channel(inf_id, "telegram", "@notag", status="ready",
                           role="broadcast")

    result = asyncio.run(pipeline.render_and_dispatch(
        AMAZON_DEAL, influencer_ids=[inf_id], source_channel="loot_source"))

    assert result[inf_id][ch_id] == "posted"
    assert f"tag={SHARED}" in wired["posted"][0][1]
    assert db.count_deferred_deals() == 0


def test_a_held_deal_keeps_its_retry_slot_instead_of_being_dropped(monkeypatch):
    """The worker must not treat "held for a tag" as "processed"."""
    wired = _wired(monkeypatch)
    inf_id = db.add_influencer("No Tag Yet", "")
    db.add_channel(inf_id, "telegram", "@notag", status="ready", role="broadcast")
    deal = AMAZON_DEAL
    db.defer_deal(deal, source="loot_source", reason=tag_proof.HOLD_REASON,
                  delay_seconds=0)
    first = db.due_deferred_deals()[0]

    # The creator is still on the shared id: the retry re-runs, the deal is not
    # deleted as if it had been delivered, and the queue keeps it for later.
    assert asyncio.run(worker.drain_deferred_deals()) == 0
    assert db.count_deferred_deals() == 1
    assert db.deferred_deal_attempts(deal, "loot_source") == 1
    again = db.due_deferred_deals()
    assert again == [] or again[0]["deal_hash"] == first["deal_hash"]

    # Once the owner saves the tag, the next retry posts it — with their own tag.
    db.update_influencer(inf_id, amazon_tag="notag-21")
    rows = db.list_deferred_deals()
    assert rows and rows[0]["reason"] == tag_proof.HOLD_REASON
    # Make it due now (the worker's own retry gap is minutes).
    db.note_deferred_attempt(rows[0]["deal_hash"], delay_seconds=0,
                             reason=tag_proof.HOLD_REASON)
    assert asyncio.run(worker.drain_deferred_deals()) == 1
    assert db.count_deferred_deals() == 0
    assert "tag=notag-21" in wired["posted"][0][1]


# --------------------------------------------------------------------------
# 3. What the owner looks at
# --------------------------------------------------------------------------
def test_fleet_pass_counts_and_names_the_blocked_creators():
    good = _creator("Ravi Loots", "ravi-21")
    db.add_channel(good, "telegram", "@ravi_loots", status="ready", role="broadcast")
    bad = db.add_influencer("No Tag Yet", "")
    db.add_channel(bad, "telegram", "@notag", status="ready", role="broadcast")
    paused = db.add_influencer("Sleeping", "")
    db.add_channel(paused, "telegram", "@sleeping", status="paused", role="broadcast")

    fleet = tag_proof.fleet()
    assert (fleet["creators"], fleet["channels"], fleet["own"], fleet["fallback"]) == (
        3, 2, 1, 1)
    assert fleet["ok"] is False and fleet["require_own_tag"] is True
    assert [row["name"] for row in fleet["blocked"]] == ["No Tag Yet"]
    assert fleet["blocked"][0]["shared_default"] is True
    assert fleet["blocked"][0]["channels"][0]["identifier"] == "@notag"
    assert "shared tag" in fleet["summary"]

    # The paused channel is not a live money path, so it is not on the list.
    assert "Sleeping" not in json.dumps(fleet["blocked"])

    db.update_influencer(bad, amazon_tag="notag-21")
    fixed = tag_proof.fleet()
    assert fixed["ok"] is True and fixed["blocked"] == []
    assert "sign Amazon links with their own" in fixed["summary"]


def test_describe_fix_names_the_creator_and_forbids_the_shared_id():
    bad = db.add_influencer("No Tag Yet", "")
    db.add_channel(bad, "telegram", "@notag", status="ready", role="broadcast")
    fleet = tag_proof.fleet()
    fix = tag_proof.describe_fix(fleet["blocked"])
    assert "No Tag Yet" in fix and SHARED in fix
    assert "held" in fix and "own Associate tag" in fix
    assert tag_proof.describe_fix([]) == ""


def test_the_fleet_pass_stays_two_queries_at_1000_creators():
    """The scale claim: the proof is batched, not a query per creator."""
    for index in range(1000):
        inf_id = db.add_influencer(f"Creator {index}", f"creator{index}-21")
        db.add_channel(inf_id, "telegram", f"@creator{index}", status="ready",
                       role="broadcast")
    held = db.add_influencer("No Tag Yet", "")
    db.add_channel(held, "telegram", "@notag", status="ready", role="broadcast")

    statements: list[str] = []
    real_connect = db._connect

    def tracing_connect(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        con.set_trace_callback(statements.append)
        return con

    db._connect = tracing_connect
    try:
        started = time.monotonic()
        fleet = tag_proof.fleet(sample=False)
        elapsed = time.monotonic() - started
    finally:
        db._connect = real_connect

    assert fleet["channels"] == 1001 and fleet["channels"] - fleet["own"] == 1
    assert [row["name"] for row in fleet["blocked"]] == ["No Tag Yet"]
    # one read of the creators, one of the channels — no per-creator query
    assert len([sql for sql in statements if "SELECT" in sql.upper()]) == 2, statements
    assert elapsed < 2.0, f"fleet() took {elapsed:.3f}s for 1001 creators"


def test_active_statuses_agree_with_the_pipeline_without_importing_it():
    """tag_proof must not import pipeline (cycle) — the sets are pinned equal instead."""
    assert tag_proof.ACTIVE_CHANNEL_STATUSES == pipeline.ACTIVE_CHANNEL_STATUSES
    import influencer_hub.tag_proof as module

    source = open(module.__file__, encoding="utf-8").read()
    assert "from . import pipeline" not in source
    assert "import pipeline" not in source


def test_selfcheck_reports_the_hold_in_plain_words():
    from influencer_hub import selfcheck

    inf_id = _creator("Ravi Loots", "ravi-21")
    db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready", role="broadcast")
    bad = db.add_influencer("No Tag Yet", "")
    db.add_channel(bad, "telegram", "@notag", status="ready", role="broadcast")

    report = selfcheck.run_checks()
    item = next(check for check in report["checks"] if check["id"] == "amazon")
    assert item["state"] == "warn"
    assert "No Tag Yet" in item["detail"] and "held" in item["detail"]
    assert SHARED in item["detail"]
    assert "own Associate tag" in item["fix"]

    db.update_influencer(bad, amazon_tag="notag-21")
    item = next(check for check in selfcheck.run_checks()["checks"]
                if check["id"] == "amazon")
    assert item["state"] == "ok"
    assert "2 of 2" in item["detail"] and "own tag" in item["detail"]


def test_cli_tag_proof_prints_the_fleet_and_the_single_creator(capsys, monkeypatch):
    from influencer_hub import cli

    good = _creator("Ravi Loots", "ravi-21")
    db.add_channel(good, "telegram", "@ravi_loots", status="ready", role="broadcast")
    bad = db.add_influencer("No Tag Yet", "")
    db.add_channel(bad, "telegram", "@notag", status="ready", role="broadcast")

    # A hub with somebody on the shared id fails the check (exit 1) and names them.
    assert cli.main(["tag-proof"]) == 1
    out = capsys.readouterr().out
    assert "No Tag Yet" in out and "@notag" in out and "1 of 2" in out
    assert "held" in out

    assert cli.main(["tag-proof", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["fallback"] == 1
    assert payload["blocked"][0]["name"] == "No Tag Yet"

    # Deploys may proceed past an onboarding gap only because those deals are
    # held; a malformed rendered link must remain a hard stop.
    assert cli.main(["tag-proof", "--allow-held"]) == 0
    assert "Amazon deals held" in capsys.readouterr().out
    with monkeypatch.context() as patch:
        patch.setattr(tag_proof, "fleet", lambda **kwargs: {
            "ok": False, "broken_links": [{"creator_id": good, "channel_id": 55,
                                          "issues": ["tag mismatch"]}],
            "blocked": [], "summary": "broken link", "creators": 2, "channels": 2,
        })
        assert cli.main(["tag-proof", "--allow-held"]) == 1

    assert cli.main(["tag-proof", "--creator", str(good)]) == 0
    out = capsys.readouterr().out
    assert "ravi-21" in out and "tag=ravi-21" in out and "✅ own" in out

    assert cli.main(["tag-proof", "--creator", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "⛔ held" in out and "no own Amazon tag" in out
    assert cli.main(["tag-proof", "--creator", "999999"]) == 2

    # Fix the tag: the same command now proves the hub clean.
    db.update_influencer(bad, amazon_tag="notag-21")
    assert cli.main(["tag-proof"]) == 0
    assert "Nothing to fix" in capsys.readouterr().out


def test_dashboard_api_proves_one_creator_and_the_fleet(monkeypatch, tmp_path):
    from dashboard.app import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tag-proof.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "owner-password-16chars")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    db.init()
    app.config.update(TESTING=True)
    client = app.test_client()

    good = _creator("Ravi Loots", "ravi-21")
    db.add_channel(good, "telegram", "@ravi_loots", status="ready", role="broadcast")
    bad = db.add_influencer("No Tag Yet", "")
    db.add_channel(bad, "telegram", "@notag", status="ready", role="broadcast")

    fleet = client.get("/api/tag-proof").get_json()
    assert fleet["ok"] is False
    assert fleet["counts"] == {"creators": 2, "channels": 2, "own": 1, "fallback": 1}
    assert fleet["blocked"][0]["name"] == "No Tag Yet"
    assert fleet["hold_reason"] == tag_proof.HOLD_REASON
    assert SHARED in fleet["fix"]

    single = client.get(f"/api/tag-proof?creator={good}").get_json()
    assert single["ok"] is True
    assert single["own_tag"] == "ravi-21"
    assert single["link"] == f"https://www.amazon.in/dp/{tag_proof.SAMPLE_ASIN}?tag=ravi-21"
    assert single["link_summary"] == f"www.amazon.in/dp/{tag_proof.SAMPLE_ASIN}?tag=ravi-21"
    assert {row["tag"] for row in single["channels"]} == {"ravi-21"}

    held = client.get(f"/api/tag-proof?creator={bad}").get_json()
    assert held["ok"] is False and held["own_tag"] == ""
    assert held["channels"][0]["own"] is False
    assert held["channels"][0]["hold_reason"] == tag_proof.HOLD_REASON
    assert client.get("/api/tag-proof?creator=999999").status_code == 404


def test_setup_page_shows_the_tag_proof_card(monkeypatch, tmp_path):
    from dashboard.app import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tag-card.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "owner-password-16chars")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    db.init()
    app.config.update(TESTING=True)
    client = app.test_client()

    good = _creator("Ravi Loots", "ravi-21")
    db.add_channel(good, "telegram", "@ravi_loots", status="ready", role="broadcast")
    bad = db.add_influencer("No Tag Yet", "")
    db.add_channel(bad, "telegram", "@notag", status="ready", role="broadcast")

    page = client.get("/setup").get_data(as_text=True)
    assert 'id="tag-proof"' in page
    assert "No Tag Yet" in page
    assert "1/2 channel(s) on their own tag" in page
    assert f"tag={SHARED}" in page            # shown as what would be posted today
    assert f"Paste their own tag" in page

    home = client.get("/").get_data(as_text=True)
    assert "1 channel(s) held — no own Amazon tag" in home
    assert "/setup#tag-proof" in home

    db.update_influencer(bad, amazon_tag="notag-21")
    page = client.get("/setup").get_data(as_text=True)
    assert "✅ all own tags" in page
    home = client.get("/").get_data(as_text=True)
    assert "every channel on its own Amazon tag" in home
    assert "held — no own Amazon tag" not in home


def test_dashboard_rejects_a_tampered_amazon_link_before_it_is_posted():
    """The last line of defence at 1000 creators: verify the rendered post itself."""
    profile = db.get_influencer(_creator("Ravi Loots", "ravi-21"))
    tampered = tag_proof.verify("deal https://www.amazon.in/dp/B0X?tag=someoneelse-21",
                                profile)
    assert tampered["ok"] is False and tampered["wrong"][0]["why"] == "different_tag"
    assert tag_proof.verify(tag_proof.tagged_link(profile), profile)["ok"] is True


def test_the_flow_board_explains_why_deals_are_parked(monkeypatch, tmp_path):
    from dashboard.app import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "flow-held.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "owner-password-16chars")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    db.init()
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.get("/api/flow").get_json()["deferred"]["reasons"] == {}

    db.defer_deal(AMAZON_DEAL, source="loot_source", reason=tag_proof.HOLD_REASON)
    db.defer_deal(FLIPKART_DEAL, source="loot_source", reason="unconverted")
    payload = client.get("/api/flow").get_json()["deferred"]
    assert payload["waiting"] == 2
    assert payload["reasons"] == {tag_proof.HOLD_REASON: 1, "unconverted": 1}
    assert {row["reason"] for row in payload["recent"]} == {
        tag_proof.HOLD_REASON, "unconverted"}


def test_the_proof_and_the_link_router_agree_on_the_tag_syntax():
    """The tag the proof builds is the tag the router accepts."""
    assert link_router.amazon_tag_is_usable(tag_proof.tagged_link(
        {"amazon_tag": "ravi-21"}).split("tag=")[-1])
    assert link_router.apply_amazon_tag("https://www.amazon.in/dp/B09N3ZNHTY",
                                        "ravi-21") == tag_proof.tagged_link(
        {"amazon_tag": "ravi-21"},
        url="https://www.amazon.in/dp/B09N3ZNHTY")


def test_the_digest_card_names_held_creators_and_parked_deals():
    """The ops card answers "why is the channel quiet?" without opening the hub."""
    from influencer_hub import ops

    good = _creator("Ravi Loots", "ravi-21")
    db.add_channel(good, "telegram", "@ravi_loots", status="ready", role="broadcast")
    bad = db.add_influencer("No Tag Yet", "")
    db.add_channel(bad, "telegram", "@notag", status="ready", role="broadcast")
    db.defer_deal(AMAZON_DEAL, source="loot_source", reason=tag_proof.HOLD_REASON)

    data = ops.digest()
    assert data["amazon_tags"]["fallback"] == 1
    assert data["retry_reasons"] == {tag_proof.HOLD_REASON: 1}

    text = ops.render_digest(data)
    assert "🏷️ Amazon tags: 1/2 channel(s) own · ⛔ 1 held (No Tag Yet)" in text
    assert "1 waiting for their own Amazon tag" in text
    assert len(text.splitlines()) <= 12          # the card stays a card


def test_the_deferred_row_reads_back_with_its_reason():
    db.defer_deal(AMAZON_DEAL, source="@src", reason=tag_proof.HOLD_REASON,
                  delay_seconds=300)
    rows = db.list_deferred_deals()
    assert len(rows) == 1
    assert rows[0]["reason"] == tag_proof.HOLD_REASON
    assert rows[0]["source"] == "@src"
    assert rows[0]["max_attempts"] >= 1
    assert db.deferred_reasons() == {tag_proof.HOLD_REASON: 1}
    # Idempotent: the same deal is never parked twice.
    db.defer_deal(AMAZON_DEAL, source="@src", reason=tag_proof.HOLD_REASON,
                  delay_seconds=300)
    assert db.count_deferred_deals() == 1
