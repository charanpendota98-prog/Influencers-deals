"""Phone-link (OTP) pairing from the dashboard: the code path beside the QR.

The operator types the influencer's number, WhatsApp shows an 8-character code
on that phone (Linked devices -> Link with phone number) and the same registered
device results — no camera, no QR. These tests pin the dashboard half:

* the number is validated before any session is created;
* the returned code is stored for the operator's own page and reported back;
* the read-only polling endpoint never writes to the database or starts a
  session, exactly like the QR poller;
* the one-time code is cleared the moment the number is actually linked.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, whatsapp_client


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "wa-pairing.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    return app.test_client()


def _session_row(key: str) -> dict:
    return next(row for row in db.list_wa_sessions() if row["session_key"] == key)


def test_the_dashboard_asks_the_hub_for_a_code_and_keeps_it_for_the_operator(client, monkeypatch):
    inf_id = db.add_influencer("Code Creator", "codecreator-21")
    key = f"inf-{inf_id}-wa"
    seen: list[tuple[str, str]] = []

    async def fake_request(session_key, phone, label=""):
        seen.append((session_key, phone))
        return {"ok": True, "code": "ABCD-EFGH", "phone": phone, "status": "pairing"}

    monkeypatch.setattr(whatsapp_client, "request_pairing_code", fake_request)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    response = client.post(
        f"/influencer/{inf_id}/pair-wa-phone",
        data={"pairing_phone": "+91 98765-43210"},
    )

    assert response.status_code == 302
    assert "wa_pair_status=code" in response.headers["Location"]
    assert seen == [(key, "919876543210")]
    row = _session_row(key)
    assert row["status"] == "pairing"
    assert row["pairing_phone"] == "919876543210"
    assert row["pairing_code"] == "ABCD-EFGH"
    assert row["pairing_error"] == ""


def test_a_short_number_is_refused_before_any_session_is_created(client, monkeypatch):
    inf_id = db.add_influencer("Typo Creator", "typo-21")
    called: list[tuple] = []

    async def fake_request(*args, **kwargs):  # pragma: no cover - must not run
        called.append((args, kwargs))
        return {"ok": True, "code": "ZZZZ-YYYY"}

    monkeypatch.setattr(whatsapp_client, "request_pairing_code", fake_request)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    short = client.post(f"/influencer/{inf_id}/pair-wa-phone", data={"pairing_phone": "98765"})
    empty = client.post(f"/influencer/{inf_id}/pair-wa-phone", data={"pairing_phone": "  "})

    assert "wa_pair_status=bad_phone" in short.headers["Location"]
    assert "wa_pair_status=missing_phone" in empty.headers["Location"]
    assert called == []
    assert db.list_wa_sessions(inf_id) == []


def test_a_refused_pairing_reports_the_reason_instead_of_a_silent_wait(client, monkeypatch):
    inf_id = db.add_influencer("Refused Creator", "refused-21")
    key = f"inf-{inf_id}-wa"

    async def refusing_request(session_key, phone, label=""):
        raise RuntimeError("a full international WhatsApp number is required")

    monkeypatch.setattr(whatsapp_client, "request_pairing_code", refusing_request)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    response = client.post(
        f"/influencer/{inf_id}/pair-wa-phone", data={"pairing_phone": "919876543210"},
    )

    assert "wa_pair_status=failed" in response.headers["Location"]
    row = _session_row(key)
    assert row["pairing_code"] == ""
    assert "international WhatsApp number" in row["pairing_error"]
    assert row["status"] == "offline"


def test_linking_the_number_clears_the_one_time_code(client):
    inf_id = db.add_influencer("Linked Creator", "linked-21")
    key = f"inf-{inf_id}-wa"
    db.upsert_wa_session(inf_id, key, phone="")
    db.set_wa_session_pairing(key, phone="919876543210", code="ABCD-EFGH")
    assert _session_row(key)["pairing_code"] == "ABCD-EFGH"

    db.set_wa_session_status(key, "connected")

    row = _session_row(key)
    assert row["status"] == "connected"
    assert row["pairing_code"] == ""
    assert row["pairing_phone"] == ""
    assert row["pairing_error"] == ""


def test_pairing_polling_is_a_read_only_get(client, monkeypatch):
    inf_id = db.add_influencer("Polling Creator", "polling-21")
    key = f"inf-{inf_id}-wa"
    db.upsert_wa_session(inf_id, key, phone="")
    db.set_wa_session_pairing(key, phone="919876543210", code="ABCD-EFGH")
    sessions_before = db.list_wa_sessions(inf_id)
    requests = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({
            "ok": True, "status": "pairing", "phone": "919876543210",
            "code": "ABCD-EFGH", "hasCode": True, "error": "",
        })

    monkeypatch.setattr(dashboard_module.urllib.request, "urlopen", fake_urlopen)
    response = client.get(f"/api/wa/{key}/pairing")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["code"] == "ABCD-EFGH"
    assert payload["hasCode"] is True
    assert [request.get_method() for request, _timeout in requests] == ["GET"]
    assert requests[0][0].full_url.endswith(f"/sessions/{key}/pairing")
    assert requests[0][1] == 10
    # Polling must not persist anything or start a session.
    assert db.list_wa_sessions(inf_id) == sessions_before
