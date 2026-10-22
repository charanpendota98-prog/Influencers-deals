"""Two roles, one promise: the team adds, the owner edits.

These tests pin the security properties that matter to the person who owns the
hub:

* the team signs in with a *different* password and cannot use the admin one;
* the team can add creators and read what it added — nothing else, on no route,
  however the request is shaped (bookmark, crafted form post, API call);
* the admin (and only the admin) can edit, and with a second factor when one is
  configured;
* every add and every change is recorded with the actor's name, their role and
  the client IP, and refused attempts are recorded too.
"""
from __future__ import annotations

import time

import pytest

from influencer_hub import config, db, security

ADMIN_PASSWORD = "owner-password-16chars"
TEAM_PASSWORD = "team-password-16chars"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A real hub (not Flask TESTING) so the route guard actually runs."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "team.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", TEAM_PASSWORD)
    monkeypatch.setattr(config, "ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_COOKIE_SECURE", False)
    db.init()
    import dashboard.app as dashboard_module

    monkeypatch.setattr(dashboard_module.app, "testing", False)
    dashboard_module.app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    monkeypatch.setattr(dashboard_module, "_dashboard_secret_key_is_persistent", lambda: True)
    client = dashboard_module.app.test_client()
    return dashboard_module, client


def _csrf_from(client, path="/login") -> str:
    page = client.get(path).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start:page.index('"', start)]


def _admin_login(client) -> None:
    token = _csrf_from(client, "/login")
    response = client.post("/login", data={"_csrf_token": token, "password": ADMIN_PASSWORD})
    assert response.status_code == 302


def _team_login(client, name="Ravi") -> None:
    token = _csrf_from(client, "/team/login")
    response = client.post("/team/login",
                           data={"_csrf_token": token, "name": name, "password": TEAM_PASSWORD})
    assert response.status_code == 302


# --------------------------------------------------------------------------
# 1. Two doors
# --------------------------------------------------------------------------
def test_the_team_password_never_opens_the_admin_door(env):
    _, client = env

    token = _csrf_from(client, "/login")
    response = client.post("/login", data={"_csrf_token": token, "password": TEAM_PASSWORD})

    assert response.status_code == 401
    assert "Sign-in failed" in response.get_data(as_text=True)


def test_the_admin_password_never_opens_the_team_door(env):
    _, client = env

    token = _csrf_from(client, "/team/login")
    response = client.post("/team/login", data={"_csrf_token": token, "name": "Ravi",
                                               "password": ADMIN_PASSWORD})

    assert response.status_code == 401


def test_team_access_can_be_switched_off(env, monkeypatch):
    _, client = env
    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", "")

    response = client.get("/team/login")

    assert response.status_code == 503
    assert "not switched on yet" in response.get_data(as_text=True)


def test_the_team_login_records_who_signed_in(env):
    _, client = env
    _team_login(client, "Sita")

    rows = db.list_audit(limit=5)

    assert rows[0]["action"] == "team_login_ok"
    assert rows[0]["actor"] == "Sita" and rows[0]["role"] == security.TEAM


def test_repeated_wrong_team_passwords_block_that_ip(env):
    _, client = env
    for _ in range(5):
        token = _csrf_from(client, "/team/login")
        client.post("/team/login", data={"_csrf_token": token, "name": "Ravi",
                                         "password": "wrong-password"})

    token = _csrf_from(client, "/team/login")
    response = client.post("/team/login", data={"_csrf_token": token, "name": "Ravi",
                                                "password": TEAM_PASSWORD})

    assert response.status_code == 429
    assert "Too many attempts" in response.get_data(as_text=True)
    # The failures live in the database, so a restart does not reset the budget.
    assert db.count_login_failures(security.TEAM, "127.0.0.1") == 5
    assert any(row["action"] == "team_login_blocked" for row in db.list_audit(limit=10))


# --------------------------------------------------------------------------
# 2. The team may add — and may not edit
# --------------------------------------------------------------------------
def test_the_team_adds_a_creator_and_sees_it_stamped_with_their_name(env):
    _, client = env
    _team_login(client, "Ravi")
    token = _csrf_from(client, "/team")

    response = client.post("/team/add", data={
        "_csrf_token": token, "name": "Naveen Loots", "amazon_tag": "naveen-21",
        "main_channel": "@naveen_loots", "approval_channel": "@naveen_amazon",
        "whatsapp_id": "120363012345678901@newsletter",
    })
    assert response.status_code == 302

    created = next(i for i in db.list_influencers() if i["name"] == "Naveen Loots")
    assert created["added_by"] == "Ravi"
    assert created["amazon_tag"] == "naveen-21"
    channels = {c["role"]: c for c in db.list_channels(created["id"])}
    assert channels["broadcast"]["identifier"] == "@naveen_loots"
    assert channels["approval"]["identifier"] == "@naveen_amazon"
    assert all(c["admin_approval_status"] == "pending" for c in channels.values())
    assert all(c["approval_requested_by"] == "Ravi" for c in channels.values())
    assert channels["broadcast"]["status"] == "pending_approval"
    assert channels["approval"]["status"] == "pending_approval"
    assert channels["whatsapp"]["status"] == "pending"
    assert db.list_audit(limit=1)[0]["action"] == "creator_added"

    page = client.get("/team").get_data(as_text=True)
    assert "Naveen Loots" in page and "naveen-21" in page
    assert "Team dashboard — add creators" in page


def test_the_team_sees_only_the_creators_it_added(env):
    _, client = env
    db.add_influencer("Owner Made This", "owner-21", added_by="")
    _team_login(client, "Ravi")
    token = _csrf_from(client, "/team")
    client.post("/team/add", data={"_csrf_token": token, "name": "Team Made This",
                                   "amazon_tag": "team-21", "main_channel": "@team_loots"})

    page = client.get("/team").get_data(as_text=True)
    api = client.get("/api/team/creators").get_json()

    assert "Team Made This" in page
    assert "Owner Made This" not in page
    assert [row["name"] for row in api["creators"]] == ["Team Made This"]
    assert api["member"] == "Ravi"


def test_adding_an_existing_creator_is_reported_never_silently_edited(env):
    _, client = env
    db.add_influencer("Existing Creator", "old-tag-21", added_by="")
    _team_login(client, "Ravi")
    token = _csrf_from(client, "/team")

    client.post("/team/add", data={"_csrf_token": token, "name": "Existing Creator",
                                   "amazon_tag": "hijack-21", "main_channel": "@new_loots"})

    existing = next(i for i in db.list_influencers() if i["name"] == "Existing Creator")
    assert existing["amazon_tag"] == "old-tag-21"          # untouched
    assert existing["added_by"] == ""
    page = client.get("/team").get_data(as_text=True)
    assert "already in the hub" in page


EDIT_ROUTES = [
    ("/influencer/1/update-profile", {"name": "Hacked"}),
    ("/channel/1/update", {"identifier": "@hacked"}),
    ("/channel/1/delete", {}),
    ("/influencer/1/delete", {"password": ADMIN_PASSWORD}),
    ("/influencer/1/toggle-active", {}),
    ("/channel/1/toggle-status", {}),
    ("/global-settings/update", {"key": "hypd_store_id", "val": "99999"}),
    ("/influencer/1/test-post", {}),
]


def test_the_team_cannot_edit_anything_even_with_a_hand_crafted_post(env):
    _, client = env
    inf_id = db.add_influencer("Team Creator", "team-21", added_by="Ravi")
    channel = db.add_channel(inf_id, "telegram", "@team_loots", status="ready",
                             role="broadcast")
    _team_login(client, "Ravi")
    token = _csrf_from(client, "/team")

    # Every value below is what the real form would send; the guard refuses
    # before the view runs, so the password field is irrelevant.
    for path, data in EDIT_ROUTES:
        payload = dict(data, _csrf_token=token)
        response = client.post(path.replace("/1/", f"/{inf_id}/")
                               .replace("/channel/1/", f"/channel/{channel}/"),
                               data=payload, follow_redirects=False)
        assert response.status_code in {302, 403}, path
        if response.status_code == 302:
            assert "/team" in str(response.headers.get("Location")), path

    profile = db.get_influencer(inf_id)
    assert profile["name"] == "Team Creator" and profile["amazon_tag"] == "team-21"
    assert db.get_channel(inf_id, "broadcast")["identifier"] == "@team_loots"
    assert db.get_global_setting("hypd_store_id", "") != "99999"
    rows = db.list_audit(limit=60)
    assert any(row["action"] == "team_blocked_route" for row in rows)
    # ...and a refused request is never recorded as if it had changed something.
    assert not any(row["action"].startswith("change:") and row["role"] == security.TEAM
                   for row in rows)


def test_the_team_cannot_open_or_edit_admin_pages(env):
    _, client = env
    _team_login(client, "Ravi")

    pages = ("/", "/setup", "/easy-setup", "/money", "/channels", "/admin/secret-vault", "/influencer/1")
    for path in pages:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302, path
        assert "/team" in str(response.headers.get("Location")), path
    # APIs answer with a refusal, never with a redirect into the console.
    for path in ("/api/readiness", "/api/digest", "/api/self-check", "/api/readiness.csv",
                 "/api/metrics", "/api/channels.csv"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 403, path
        assert response.get_json()["error"] == "admin_only"

    # The team's own read-only API works; anything else under /api/team is 404.
    assert client.get("/api/team/creators").status_code == 200
    assert client.get("/api/team/creators").get_json()["ok"] is True


def test_the_team_can_add_by_pasting_many_lines(env):
    _, client = env
    _team_login(client, "Naveen")
    token = _csrf_from(client, "/team")

    client.post("/team/add", data={
        "_csrf_token": token,
        "bulk_lines": ("Creator One, one-21, @one_loots, @one_amazon,\n"
                       "Creator Two, two-21, @two_loots, @two_amazon, 120363012345678901@newsletter\n"),
    })

    names = {inf["name"] for inf in db.list_influencers(added_by="Naveen")}
    assert names == {"Creator One", "Creator Two"}
    # A bad row is reported without stopping the good ones.
    token = _csrf_from(client, "/team")
    client.post("/team/add", data={"_csrf_token": token,
                                   "bulk_lines": "Broken Row, bad tag!, @x\nGood Row, good-21, @good_loots\n"})
    assert {inf["name"] for inf in db.list_influencers(added_by="Naveen")} == {
        "Creator One", "Creator Two", "Good Row"}


def test_the_hourly_quota_stops_a_typo_loop(env, monkeypatch):
    _, client = env
    monkeypatch.setattr(config, "TEAM_ADDS_PER_HOUR", 2)
    _team_login(client, "Ravi")

    for index in range(3):
        token = _csrf_from(client, "/team")
        client.post("/team/add", data={"_csrf_token": token, "name": f"Creator {index}",
                                       "amazon_tag": f"c{index}-21", "main_channel": f"@creator{index}_loots"})

    assert len(db.list_influencers(added_by="Ravi")) == 2
    assert any(row["action"] == "team_quota_blocked" for row in db.list_audit(limit=20))


# --------------------------------------------------------------------------
# 3. The owner keeps the keys
# --------------------------------------------------------------------------
def test_the_admin_edits_everything_the_team_cannot(env):
    _, client = env
    inf_id = db.add_influencer("Team Creator", "team-21", added_by="Ravi")
    _admin_login(client)

    token = _csrf_from(client, "/")
    response = client.post(f"/influencer/{inf_id}/toggle-active",
                           data={"_csrf_token": token})

    assert response.status_code in {302, 200}
    assert db.get_influencer(inf_id)["active"] == 0
    assert client.get("/").status_code == 200               # the full console
    assert client.get("/setup").status_code == 200


def test_admin_changes_are_recorded_with_the_actor_and_the_action(env):
    _, client = env
    inf_id = db.add_influencer("Someone", "someone-21")
    _admin_login(client)

    token = _csrf_from(client, "/")
    client.post(f"/influencer/{inf_id}/toggle-active", data={"_csrf_token": token})

    row = db.list_audit(limit=1)[0]
    assert row["actor"] == "admin" and row["role"] == security.ADMIN
    assert row["action"].startswith("change:")
    assert f"/influencer/{inf_id}/toggle-active" in row["target"]


def test_a_stopped_request_is_not_recorded_as_a_change(env):
    """A delete that waits for the password changed nothing, so it logs nothing."""
    _, client = env
    inf_id = db.add_influencer("Someone", "someone-21")
    _admin_login(client)

    token = _csrf_from(client, "/")
    client.post(f"/influencer/{inf_id}/delete", data={"_csrf_token": token})

    assert db.get_influencer(inf_id) is not None            # still there
    assert not any(row["action"].startswith("change:influencer_delete")
                   for row in db.list_audit(limit=20))


# --------------------------------------------------------------------------
# 4. The second factor
# --------------------------------------------------------------------------
def test_the_admin_sign_in_asks_for_a_code_when_two_factor_is_on(env, monkeypatch):
    _, client = env
    secret = security.new_totp_secret()
    monkeypatch.setattr(config, "ADMIN_TOTP_SECRET", secret)

    page = client.get("/login").get_data(as_text=True)
    assert 'name="totp"' in page

    token = _csrf_from(client, "/login")
    wrong = client.post("/login", data={"_csrf_token": token, "password": ADMIN_PASSWORD,
                                        "totp": "000000"})
    assert wrong.status_code == 401
    assert "six-digit code did not match" in wrong.get_data(as_text=True)
    assert db.list_audit(limit=1)[0]["action"] == "admin_login_failed"

    token = _csrf_from(client, "/login")
    right = client.post("/login", data={"_csrf_token": token, "password": ADMIN_PASSWORD,
                                        "totp": security.totp_code(secret)})
    assert right.status_code == 302
    assert client.get("/").status_code == 200


def test_codes_follow_the_clock_and_allow_a_little_drift():
    secret = security.new_totp_secret()
    now = time.time()

    assert security.verify_totp(security.totp_code(secret, at=now), secret=secret, at=now)
    assert security.verify_totp(security.totp_code(secret, at=now - 30), secret=secret, at=now)
    assert not security.verify_totp(security.totp_code(secret, at=now - 300), secret=secret, at=now)
    assert not security.verify_totp("", secret=secret)
    assert not security.verify_totp("abcdef", secret=secret)
    assert security.totp_uri(secret).startswith("otpauth://totp/")


def test_the_totp_cli_helper_creates_and_checks_codes(env, capsys, monkeypatch):
    from influencer_hub import cli

    assert cli.main(["totp", "--new"]) == 0
    out = capsys.readouterr().out
    secret_line = next(line for line in out.splitlines() if "ADMIN_TOTP_SECRET=" in line)
    secret = secret_line.split("=", 1)[1].strip()

    monkeypatch.setattr(config, "ADMIN_TOTP_SECRET", secret)
    assert cli.main(["totp", "--check", security.totp_code(secret)]) == 0
    assert cli.main(["totp", "--check", "000000"]) == 1
    assert cli.main(["totp", "--uri"]) == 0
    out = capsys.readouterr().out
    assert "otpauth://totp/" in out


# --------------------------------------------------------------------------
# 5. The self-check tells the owner where they stand
# --------------------------------------------------------------------------
def test_the_self_check_reports_team_and_two_factor_state(env, monkeypatch):
    from influencer_hub import selfcheck

    check = next(c for c in selfcheck.run_checks()["checks"] if c["id"] == "access")
    assert check["state"] == "warn"
    assert "no second factor" in check["detail"] or "no second factor" in check["fix"]

    monkeypatch.setattr(config, "ADMIN_TOTP_SECRET", security.new_totp_secret())
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "a-much-longer-admin-password")
    check = next(c for c in selfcheck.run_checks()["checks"] if c["id"] == "access")
    assert check["state"] == "ok"
    assert "second factor" in check["detail"]


def test_the_setup_page_shows_the_team_card_with_the_audit_log(env):
    _, client = env
    _team_login(client, "Ravi")
    token = _csrf_from(client, "/team")
    client.post("/team/add", data={"_csrf_token": token, "name": "Shown Creator",
                                   "amazon_tag": "shown-21", "main_channel": "@shown"})
    client.post("/logout", data={"_csrf_token": _csrf_from(client, "/team")})
    _admin_login(client)

    page = client.get("/setup").get_data(as_text=True)

    assert "Team access &amp; audit" in page or "Team access & audit" in page
    assert "Shown Creator" in page
    assert "creator_added" in page and "Ravi" in page
    assert "Add creators for the hub" not in page          # that is the team page

    # The team door is public (it is the way in), and shows only the team page —
    # an admin session is sent to /team instead, never left on the sign-in form.
    module, _ = env
    anonymous = module.app.test_client()
    team_page = anonymous.get("/team/login").get_data(as_text=True)
    assert "TEAM ACCESS" in team_page and "team password" in team_page.lower()
    assert "Add creators for the hub" in team_page
    assert module.app.test_client().get("/team", follow_redirects=False).status_code == 302
