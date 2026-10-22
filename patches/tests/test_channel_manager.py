"""Large-fleet channel inventory, approvals, counts and export."""
from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from dashboard.app import app
from influencer_hub import db


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(app.config, "TESTING", True)
    return app.test_client()


def test_channel_inventory_searches_and_paginates_server_side(client):
    inf_id = db.add_influencer("Creator Search Target", "creator-21", added_by="worker-a")
    for index in range(73):
        channel_id = db.add_channel(
            inf_id, "telegram", f"@store_{index:03d}", role="broadcast",
            status="ready", title=f"Store {index:03d}",
            categories="electronics" if index % 2 == 0 else "fashion",
            allowed_sources="@powerloot" if index % 2 == 0 else "@dailyloot",
        )
        if index % 2 == 0:
            db.record_channel_member_count(channel_id, 1000 + index)
            if index == 2:
                con = db._connect()
                try:
                    con.execute(
                        "UPDATE channels SET member_count_checked_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                        (channel_id,),
                    )
                    con.commit()
                finally:
                    con.close()
    first = client.get("/channels?size=25&page=1")
    assert first.status_code == 200
    body = first.get_data(as_text=True)
    assert "73 matching channels" in body
    assert "page 1 of 3" in body
    assert "Channel added" in body and "Creator added" in body
    assert "IST" in body

    oldest = client.get("/channels?size=25&sort=oldest").get_data(as_text=True)
    assert "Store 000" in oldest
    assert "Store 072" not in oldest

    largest = client.get("/channels?size=25&sort=members_desc").get_data(as_text=True)
    assert "Store 072" in largest

    filtered = client.get("/channels?q=store_000&platform=telegram&category=electronics&size=25")
    assert filtered.status_code == 200
    assert "Store 000" in filtered.get_data(as_text=True)
    assert "Store 001" not in filtered.get_data(as_text=True)

    by_source_and_audience = client.get(
        "/channels?source=powerloot&min_members=1020&max_members=1020&size=25"
    ).get_data(as_text=True)
    assert "Store 020" in by_source_and_audience
    assert "Effective source filter: @powerloot" in by_source_and_audience
    assert "Store 000" not in by_source_and_audience
    assert "Store 021" not in by_source_and_audience

    missing_count = client.get("/channels?q=store_001&count_state=missing").get_data(as_text=True)
    fresh_count = client.get("/channels?q=store_000&count_state=fresh").get_data(as_text=True)
    assert "Store 001" in missing_count
    assert "Store 000" in fresh_count
    stale_count = client.get("/channels?q=store_002&count_state=stale").get_data(as_text=True)
    assert "Store 002" in stale_count
    ist = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(timezone.utc).astimezone(ist).date().isoformat()
    added_today = client.get(f"/channels?q=store_000&added_from={today_ist}&added_to={today_ist}")
    assert "Store 000" in added_today.get_data(as_text=True)

    third = client.get("/channels?size=25&page=3")
    assert "Showing 51–73 of 73" in third.get_data(as_text=True)


def test_inventory_paginates_beyond_twelve_thousand_channels(client):
    inf_id = db.add_influencer("Large Inventory", "large-inventory-21")
    con = db._connect()
    try:
        con.executemany(
            "INSERT INTO channels (influencer_id, platform, identifier, role, status) "
            "VALUES (?, 'telegram', ?, 'broadcast', 'ready')",
            [(inf_id, f"@large_{index:05d}") for index in range(12005)],
        )
        con.commit()
    finally:
        con.close()

    response = client.get("/channels?size=100&page=121")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "12005 matching channels" in body
    assert "page 121 of 121" in body


def test_team_submitted_channel_is_blocked_until_admin_review(client, monkeypatch):
    inf_id = db.add_influencer("Awaiting Review", "awaiting-21", added_by="worker-a")
    channel_id = db.add_channel(
        inf_id, "telegram", "@awaiting_review", status="pending_approval",
    )
    db.request_channel_approval(channel_id, "review-agent")
    channel = db.get_channel_by_id(channel_id)
    assert "Awaiting Review" in client.get("/channels?q=review-agent").get_data(as_text=True)
    from influencer_hub import pipeline
    assert asyncio.run(pipeline.dispatch_to_channel({"id": inf_id}, channel, "test")) == "skipped:admin_approval_required"

    response = client.post(f"/channels/{channel_id}/review", data={"decision": "approved"})
    assert response.status_code == 302
    updated = db.get_channel_by_id(channel_id)
    assert updated["admin_approval_status"] == "approved"
    assert updated["approval_reviewed_by"]
    assert updated["status"] == "ready"


def test_whatsapp_approval_does_not_bypass_pairing_and_test(client):
    inf_id = db.add_influencer("WhatsApp Review", "wa-review-21")
    channel_id = db.add_channel(
        inf_id, "whatsapp_channel", "120363000001@newsletter", status="pending",
    )
    db.request_channel_approval(channel_id, "worker-a")
    client.post(f"/channels/{channel_id}/review", data={"decision": "approved"})
    channel = db.get_channel_by_id(channel_id)
    assert channel["admin_approval_status"] == "approved"
    assert channel["status"] == "pending"


def test_member_count_refresh_is_cached_and_visible(client, monkeypatch):
    inf_id = db.add_influencer("Counted", "counted-21")
    channel_id = db.add_channel(inf_id, "telegram", "@counted_channel", status="ready")
    monkeypatch.setattr("dashboard.app._run", lambda awaitable: 12_345)
    monkeypatch.setattr("dashboard.app.telegram_ops.get_member_count", lambda identifier: 12_345)
    response = client.post(f"/channels/{channel_id}/refresh-count", data={})
    assert response.status_code == 302
    channel = db.get_channel_by_id(channel_id)
    assert channel["member_count"] == 12_345
    assert channel["member_count_checked_at"]
    assert channel["member_count_error"] == ""
    last_success = channel["member_count_checked_at"]

    def unavailable(_awaitable):
        raise TimeoutError("temporary platform outage")

    monkeypatch.setattr("dashboard.app._run", unavailable)
    failed_refresh = client.post(f"/channels/{channel_id}/refresh-count", data={})
    assert failed_refresh.status_code == 302
    after_error = db.get_channel_by_id(channel_id)
    assert after_error["member_count"] == 12_345
    assert after_error["member_count_checked_at"] == last_success
    assert "temporary platform outage" in after_error["member_count_error"]


def test_whatsapp_subscriber_count_uses_the_paired_account_snapshot(client, monkeypatch):
    inf_id = db.add_influencer("WA Counted", "wa-counted-21")
    channel_id = db.add_channel(
        inf_id, "whatsapp_channel", "12345@newsletter", status="pending",
        wa_session_key="inf-1-wa",
    )
    monkeypatch.setattr(
        "dashboard.app.whatsapp_client.list_admin_chats",
        lambda key: [{"jid": "12345@newsletter", "members": 7654}],
    )
    monkeypatch.setattr("dashboard.app._run", lambda value: value)
    response = client.post(f"/channels/{channel_id}/refresh-count", data={})
    assert response.status_code == 302
    assert db.get_channel_by_id(channel_id)["member_count"] == 7654


def test_filtered_csv_export_quotes_formula_like_channel_fields(client):
    inf_id = db.add_influencer("Export", "export-21")
    channel_id = db.add_channel(inf_id, "telegram", "@=HYPERLINK(\"x\")", title="=1+1", status="ready")
    con = db._connect()
    try:
        con.execute("UPDATE channels SET title=? WHERE id=?", ("   =1+1", channel_id))
        con.commit()
    finally:
        con.close()
    response = client.get("/api/channels.csv?q=Export")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert len(rows) == 1
    assert rows[0]["title"] == "'   =1+1"
    assert rows[0]["identifier"].startswith("'@")
