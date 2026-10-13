"""The money map: one Amazon tag belongs to one creator, one channel to one creator.

These tests pin the mistakes that cost real money and used to be invisible until
the payout report: two creators posting with the same Amazon tag, and one
destination saved on two creators (every deal posted twice, each copy signed with
a different tag). The guard must find both anywhere in the hub — and refuse the
row at the door when the team adds a creator.
"""
from __future__ import annotations

import json

import pytest

import dashboard.app as dashboard_module
from influencer_hub import config, db, guard, security

ADMIN_PASSWORD = "owner-password-16chars"
TEAM_PASSWORD = "team-password-16chars"


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "guard.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", TEAM_PASSWORD)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "AMAZON_ASSOCIATE_TAG", "mama086-21")
    db.init()
    return db


def _csrf(client, path):
    page = client.get(path).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start:page.index('"', start)]


def _login(client, path, data):
    return client.post(path, data={**data, "_csrf_token": _csrf(client, path)})


def _client(monkeypatch):
    monkeypatch.setattr(dashboard_module.app, "testing", False)
    monkeypatch.setattr(dashboard_module, "_dashboard_secret_key_is_persistent", lambda: True)
    return dashboard_module.app.test_client()


# --------------------------------------------------------------------------
# 1. The scan finds what pays the wrong person
# --------------------------------------------------------------------------
def test_an_empty_hub_has_nothing_to_fix(hub):
    assert guard.scan() == []
    assert guard.is_clear([]) is True
    assert "nothing mixes" in guard.summary_line([])
    assert guard.counts([])["high"] == 0


def test_two_creators_on_one_tag_is_a_high_finding(hub):
    db.add_influencer("Ravi Loots", "ravi-21")
    db.add_influencer("Sita Deals", "ravi-21")
    db.add_influencer("Kiran Deals", "kiran-21")

    findings = guard.scan()

    assert [item["kind"] for item in findings] == [guard.TAG_COLLISION]
    assert findings[0]["severity"] == guard.HIGH
    assert "ravi-21" in findings[0]["detail"]
    assert {creator["name"] for creator in findings[0]["creators"]} == {"Ravi Loots", "Sita Deals"}
    assert guard.is_clear(findings) is False
    assert guard.counts(findings)["high"] == 1
    assert "pay the wrong person" in guard.summary_line(findings)


def test_the_hub_wide_tag_is_not_a_collision_but_is_reported(hub):
    """The shared default earns, so it is a note — not a "two people" alarm."""
    db.add_influencer("Ravi Loots", "mama086-21")
    db.add_influencer("Sita Deals", "mama086-21")

    findings = guard.scan()

    kinds = {item["kind"] for item in findings}
    assert guard.TAG_COLLISION not in kinds
    shared = next(item for item in findings if item["kind"] == guard.TAG_MISSING)
    assert shared["severity"] == guard.MEDIUM
    assert "2 creator(s)" in shared["detail"]


def test_an_unusable_saved_tag_is_reported_as_high(hub):
    db.add_influencer("Ravi Loots", "not a tag!")

    findings = guard.scan()

    assert any(item["kind"] == guard.TAG_INVALID for item in findings)
    assert guard.counts(findings)["high"] == 1


def test_one_channel_saved_on_two_creators_is_a_high_finding(hub):
    first = db.add_influencer("Ravi Loots", "ravi-21")
    second = db.add_influencer("Sita Deals", "sita-21")
    db.add_channel(first, "telegram", "@shared_loots", role="broadcast", status="ready")
    db.add_channel(second, "telegram", "@shared_loots", role="broadcast", status="ready")

    findings = guard.scan()
    collision = next(item for item in findings if item["kind"] == guard.CHANNEL_COLLISION)

    assert collision["severity"] == guard.HIGH
    assert "@shared_loots" in collision["detail"]
    assert len(collision["creators"]) == 2


def test_the_same_channel_written_differently_is_still_one_channel(hub):
    """@handle, t.me/handle, https://t.me/handle/ and a trailing query are the same."""
    first = db.add_influencer("Ravi Loots", "ravi-21")
    second = db.add_influencer("Sita Deals", "sita-21")
    db.add_channel(first, "telegram", "@shared_loots", role="broadcast", status="ready")
    db.add_channel(second, "telegram", "https://t.me/Shared_Loots/?utm=1", role="broadcast",
                   status="ready")

    assert guard.channel_key("telegram", "@shared_loots") == \
        guard.channel_key("telegram", "https://t.me/Shared_Loots/?utm=1")
    assert any(item["kind"] == guard.CHANNEL_COLLISION for item in guard.scan())


def test_a_whatsapp_channel_and_a_telegram_handle_do_not_collide(hub):
    first = db.add_influencer("Ravi Loots", "ravi-21")
    second = db.add_influencer("Sita Deals", "sita-21")
    db.add_channel(first, "whatsapp_channel", "120363012345678901@newsletter",
                   role="whatsapp", status="ready")
    db.add_channel(second, "telegram", "@loots", role="broadcast", status="ready")

    kinds = {item["kind"] for item in guard.scan()}
    assert guard.CHANNEL_COLLISION not in kinds


def test_the_same_channel_as_preview_and_main_is_a_tidy_up_note(hub):
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    db.add_channel(inf_id, "telegram", "@loots_one", role="broadcast", status="ready")
    db.add_channel(inf_id, "telegram", "@loots_one", role="approval", status="ready")

    findings = guard.scan()
    clash = next(item for item in findings if item["kind"] == guard.ROLE_CLASH)

    assert clash["severity"] == guard.MEDIUM
    assert guard.is_clear(findings) is True       # nothing pays the wrong person


def test_duplicate_names_are_reported_because_adds_match_by_name(hub):
    db.add_influencer("Ravi Loots", "ravi-21")
    db.add_influencer("ravi loots", "ravi2-21")

    findings = guard.scan()
    same_name = next(item for item in findings if item["kind"] == guard.NAME_COLLISION)

    assert same_name["severity"] == guard.MEDIUM
    assert "2 profiles" in same_name["detail"]
    assert "#1" in same_name["detail"]


def test_findings_are_ordered_worst_first_and_every_one_names_a_fix(hub):
    db.add_influencer("Ravi Loots", "ravi-21")
    db.add_influencer("Sita Deals", "ravi-21")
    db.add_influencer("Kiran Deals", "mama086-21")

    findings = guard.scan()

    assert findings[0]["severity"] == guard.HIGH
    assert all(item["fix"] for item in findings)
    assert all(item["creators"] for item in findings)
    assert all(item["url"].startswith("/") for item in findings)


def test_the_report_is_readable_in_a_terminal_and_in_json(hub):
    db.add_influencer("Ravi Loots", "ravi-21")
    db.add_influencer("Sita Deals", "ravi-21")

    text = guard.render_report()

    assert "⛔" in text and "fix:" in text
    assert json.loads(json.dumps(guard.scan()))[0]["kind"] == guard.TAG_COLLISION


# --------------------------------------------------------------------------
# 2. The door: a row that would mix is refused, by name
# --------------------------------------------------------------------------
def test_preflight_refuses_a_tag_that_belongs_to_somebody_else(hub):
    db.add_influencer("Ravi Loots", "ravi-21")
    index = guard.GuardIndex.from_db()

    taken = index.preflight(name="New Person", tag="ravi-21")

    assert taken["ok"] is False
    assert taken["errors"][0]["field"] == "amazon_tag"
    assert "Ravi Loots" in taken["errors"][0]["detail"]
    assert "Ravi Loots" in index.explain(taken)


def test_preflight_refuses_a_channel_that_belongs_to_somebody_else(hub):
    ravi = db.add_influencer("Ravi Loots", "ravi-21")
    db.add_channel(ravi, "telegram", "@ravi_loots", role="broadcast", status="ready")
    index = guard.GuardIndex.from_db()

    for written in ("@ravi_loots", "t.me/ravi_loots", "https://t.me/Ravi_Loots/"):
        taken = index.preflight(name="New Person", tag="new-21", main=written)
        assert taken["ok"] is False, written
        assert "Ravi Loots" in index.explain(taken)


def test_preflight_refuses_one_channel_as_preview_and_main(hub):
    db.add_influencer("Ravi Loots", "ravi-21")
    index = guard.GuardIndex.from_db()

    taken = index.preflight(name="New", tag="new-21", main="@loots_one", approval="@loots_one")

    assert taken["ok"] is False
    assert "same channel" in index.explain(taken)


def test_preflight_refuses_a_whatsapp_destination_already_saved(hub):
    ravi = db.add_influencer("Ravi Loots", "ravi-21")
    db.add_channel(ravi, "whatsapp_channel", "120363012345678901@newsletter",
                   role="whatsapp", status="ready")
    index = guard.GuardIndex.from_db()

    taken = index.preflight(name="New", tag="new-21", main="@new_main",
                            whatsapp="120363012345678901@newsletter")

    assert taken["ok"] is False
    assert "whatsapp_channel" == taken["errors"][0]["field"]


def test_preflight_warns_about_the_hub_wide_tag_and_duplicate_names(hub):
    db.add_influencer("Ravi Loots", "mama086-21")
    index = guard.GuardIndex.from_db()

    shared = index.preflight(name="New Person", tag="mama086-21", main="@new_main")
    assert shared["ok"] is True                    # it earns, so it is a warning
    assert any("hub-wide" in item["detail"] for item in shared["warnings"])

    missing = index.preflight(name="Nobody", tag="", main="@new_main")
    assert missing["ok"] is True
    assert any("fall back" in item["detail"] for item in missing["warnings"])

    duplicate = index.preflight(name="Ravi Loots", tag="another-21", main="@other_main")
    assert duplicate["ok"] is True
    assert any(item["field"] == "name" for item in duplicate["warnings"])


def test_a_creator_may_keep_its_own_tag_and_channel_when_it_is_updated(hub):
    ravi = db.add_influencer("Ravi Loots", "ravi-21")
    db.add_channel(ravi, "telegram", "@ravi_loots", role="broadcast", status="ready")
    index = guard.GuardIndex.from_db()

    kept = index.preflight(name="Ravi Loots", tag="ravi-21", ignore_id=ravi,
                           main="@ravi_loots")

    assert kept["ok"] is True
    assert not any(item["field"] == "amazon_tag" for item in kept["errors"])


def test_the_index_notices_two_rows_in_the_same_paste(hub):
    """The paste itself must not create the collision the scan would find later."""
    index = guard.GuardIndex.from_db()

    first = index.preflight(name="One", tag="twin-21", main="@one_main")
    assert first["ok"] is True
    index.note(1, "twin-21", name="One")

    second = index.preflight(name="Two", tag="twin-21", main="@two_main")
    assert second["ok"] is False
    assert "One" in index.explain(second)


def test_the_index_counts_credentials_written_in_different_shapes(hub):
    index = guard.GuardIndex.from_db()

    assert index.preflight(name="A", tag="shape-21",
                           main="https://t.me/shape_loots/")["ok"] is True
    index.note(1, "shape-21", name="A",
               channels=[{"platform": "telegram", "identifier": "https://t.me/shape_loots/",
                          "role": "broadcast"}])

    assert index.preflight(name="B", tag="shape2-21", main="@shape_loots")["ok"] is False


# --------------------------------------------------------------------------
# 3. The team page: refused at the door, with a reason, never a half-add
# --------------------------------------------------------------------------
def test_the_team_page_refuses_a_taken_tag_and_says_whose_it_is(hub, monkeypatch):
    db.add_influencer("Ravi Loots", "ravi-21")
    client = _client(monkeypatch)
    _login(client, "/team/login", {"name": "Mohan", "password": TEAM_PASSWORD})

    page = client.post("/team/add", follow_redirects=True, data={
        "_csrf_token": _csrf(client, "/team"),
        "name": "Copy Cat", "amazon_tag": "ravi-21", "main_channel": "@copycat_loots",
    }).get_data(as_text=True)

    assert "Ravi Loots" in page                       # the reason names the owner
    assert [inf["name"] for inf in db.list_influencers()] == ["Ravi Loots"]
    assert any(row["action"] == "add_blocked_duplicate" for row in db.list_audit(limit=10))


def test_the_team_page_refuses_a_channel_that_is_already_live(hub, monkeypatch):
    ravi = db.add_influencer("Ravi Loots", "ravi-21")
    db.add_channel(ravi, "telegram", "@ravi_loots", role="broadcast", status="ready")
    client = _client(monkeypatch)
    _login(client, "/team/login", {"name": "Mohan", "password": TEAM_PASSWORD})

    page = client.post("/team/add", follow_redirects=True, data={
        "_csrf_token": _csrf(client, "/team"),
        "name": "Copy Cat", "amazon_tag": "copycat-21", "main_channel": "@ravi_loots",
    }).get_data(as_text=True)

    assert "already the main channel" in page
    assert len(db.list_influencers()) == 1
    assert db.list_channels(1) and len(db.list_channels(1)) == 1


def test_a_clean_team_add_still_works_and_lands_in_the_money_map(hub, monkeypatch):
    db.add_influencer("Ravi Loots", "ravi-21")
    client = _client(monkeypatch)
    _login(client, "/team/login", {"name": "Mohan", "password": TEAM_PASSWORD})

    client.post("/team/add", data={
        "_csrf_token": _csrf(client, "/team"),
        "name": "Fresh Creator", "amazon_tag": "fresh-21",
        "main_channel": "@fresh_loots", "approval_channel": "@fresh_approval",
        "whatsapp_id": "120363099999999999@newsletter",
    })

    assert [inf["name"] for inf in db.list_influencers()] == ["Ravi Loots", "Fresh Creator"]
    assert guard.scan() == []                          # nothing mixed, nothing missing
    assert db.list_influencers(added_by="Mohan")[0]["amazon_tag"] == "fresh-21"


def test_a_team_add_saves_the_extras_the_line_carried(hub, monkeypatch):
    """A phone, a niche and the hours a creator asked for land on the profile."""
    client = _client(monkeypatch)
    _login(client, "/team/login", {"name": "Mohan", "password": TEAM_PASSWORD})

    client.post("/team/add", data={
        "_csrf_token": _csrf(client, "/team"),
        "bulk_lines": "Ramesh Rao, ramesh-21, @ramesh_loots, @ramesh_amazon, "
                      "120363012345678901@newsletter, 9876500000, electronics, 09-23, budget",
    })

    created = db.search_influencers("Ramesh Rao")[0]
    assert created["phone_number"] == "9876500000"
    assert created["categories"] == "electronics"
    assert created["posting_schedule"] == "09-23"
    assert created["price_filter"] == "budget"
    assert created["added_by"] == "Mohan"
    roles = {channel["role"]: channel for channel in db.list_channels(created["id"])}
    assert roles["broadcast"]["mirror_whatsapp"] == "1"
    assert roles["whatsapp"]["status"] == "pending"


def test_the_check_button_answers_without_writing_anything(hub, monkeypatch):
    db.add_influencer("Ravi Loots", "ravi-21")
    client = _client(monkeypatch)
    _login(client, "/team/login", {"name": "Mohan", "password": TEAM_PASSWORD})
    page = client.get("/team").get_data(as_text=True)
    csrf = page.split('name="_csrf_token" value="')[1].split('"')[0]

    response = client.post("/team/preview", data={
        "_csrf_token": csrf,
        "bulk_lines": "Ravi Loots ravi-21 @ravi_loots\nNew Person new-21 t.me/new_loots",
    })
    data = response.get_json()

    assert response.status_code == 200 and data["ok"] is True
    assert data["total"] == 2 and data["ready"] == 2
    assert data["rows"][0]["name"] == "Ravi Loots" and data["rows"][0]["tag"] == "ravi-21"
    assert data["rows"][0]["main"] == "@ravi_loots"
    assert data["rows"][1]["main"] == "@new_loots"
    assert data["defaults"].startswith("New creators start with")
    assert len(db.list_influencers()) == 1                 # read-only


def test_the_check_button_points_out_a_line_it_cannot_use(hub, monkeypatch):
    client = _client(monkeypatch)
    _login(client, "/team/login", {"name": "Mohan", "password": TEAM_PASSWORD})
    page = client.get("/team").get_data(as_text=True)
    csrf = page.split('name="_csrf_token" value="')[1].split('"')[0]

    data = client.post("/team/preview", data={
        "_csrf_token": csrf, "bulk_lines": "Broken Line\nGood, good-21, @good_loots",
    }).get_json()

    assert data["problems"] == 1
    broken = data["rows"][0]
    assert broken["line"] == 1 and broken["problems"]
    assert data["rows"][1]["ready"] is True


def test_the_team_page_shows_what_every_add_inherits(hub, monkeypatch):
    client = _client(monkeypatch)
    _login(client, "/team/login", {"name": "Mohan", "password": TEAM_PASSWORD})

    page = client.get("/team").get_data(as_text=True)

    assert "New creators start with:" in page
    assert "Check these lines first" in page


# --------------------------------------------------------------------------
# 4. The dashboard surface
# --------------------------------------------------------------------------
def test_the_api_reports_the_map_and_the_team_cannot_read_it(hub, monkeypatch):
    db.add_influencer("Ravi Loots", "ravi-21")
    db.add_influencer("Sita Deals", "ravi-21")
    client = _client(monkeypatch)
    _login(client, "/login", {"password": ADMIN_PASSWORD})

    data = client.get("/api/assignments").get_json()
    assert data["ok"] is False
    assert data["counts"]["high"] == 1
    assert data["findings"][0]["kind"] == "tag_collision"

    team = _client(monkeypatch)
    _login(team, "/team/login", {"name": "Mohan", "password": TEAM_PASSWORD})
    assert team.get("/api/assignments").status_code == 403


def test_the_setup_page_carries_the_money_map_and_the_defaults(hub, monkeypatch):
    db.add_influencer("Ravi Loots", "ravi-21")
    client = _client(monkeypatch)
    _login(client, "/login", {"password": ADMIN_PASSWORD})

    page = client.get("/setup").get_data(as_text=True)

    assert 'id="money-map"' in page and "Money map" in page
    assert 'id="add-defaults"' in page and "New-creator defaults" in page
    assert "nothing mixed" in page or "to fix" in page


def test_saving_the_defaults_changes_what_the_next_add_creates(hub, monkeypatch):
    client = _client(monkeypatch)
    _login(client, "/login", {"password": ADMIN_PASSWORD})
    saved = client.post("/setup/add-defaults", data={
        "_csrf_token": _csrf(client, "/setup"), "allow_amazon": "1", "allow_hypd": "1",
        "categories": "electronics", "posting_schedule": "09-23", "price_filter": "budget",
        "hypd_store_id": "93944",
    })
    assert saved.status_code == 302

    from influencer_hub import presets

    values = presets.resolved()
    assert values["allow_earnkaro"] is False              # unticked box = off
    assert values["mirror_whatsapp"] is False
    assert values["categories"] == "electronics"

    client.post("/easy-setup/bulk", data={
        "_csrf_token": _csrf(client, "/setup"),
        "bulk_lines": "Defaults Kid, defaults-21, @defaults_main, @defaults_approval",
    })
    created = db.search_influencers("Defaults Kid")[0]

    assert created["categories"] == "electronics"
    assert created["posting_schedule"] == "09-23"
    assert created["price_filter"] == "budget"
    assert int(created["allow_earnkaro"]) == 0
    assert int(created["only_amazon"]) == 0
    assert any(row["action"] == "add_defaults_changed" for row in db.list_audit(limit=10))


def test_the_self_check_names_the_map_and_its_fix(hub):
    from influencer_hub import selfcheck

    db.add_influencer("Ravi Loots", "ravi-21")
    db.add_influencer("Sita Deals", "ravi-21")

    report = selfcheck.run_checks(live=False)
    item = next(check for check in report["checks"] if check["id"] == "assignments")

    assert item["state"] == "warn"
    assert "wrong" in item["detail"]
    assert "Money map" in item["fix"]


def test_the_cli_reports_and_can_fix_the_map(hub, capsys):
    from influencer_hub import cli

    db.add_influencer("Ravi Loots", "ravi-21")
    db.add_influencer("Sita Deals", "ravi-21")

    assert cli.main(["assignments"]) == 1                  # exit 1 when money is mixed
    out = capsys.readouterr().out
    assert "⛔" in out and "ravi-21" in out

    assert cli.main(["assignments", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["counts"]["high"] == 1

    db.update_influencer(2, amazon_tag="sita-21")
    assert cli.main(["assignments"]) == 0
    assert "nothing mixes" in capsys.readouterr().out


def test_the_cli_sets_the_new_creator_defaults(hub, capsys):
    from influencer_hub import cli, presets

    assert cli.main(["add-defaults", "--only-amazon", "on", "--mirror-whatsapp", "off",
                     "--store", "93944", "--hours", "09-23"]) == 0
    values = presets.resolved()

    assert values["only_amazon"] is True
    assert values["mirror_whatsapp"] is False
    assert values["posting_schedule"] == "09-23"
    assert values["hypd_store_id"] == "93944"
    assert "Amazon-only" in capsys.readouterr().out

    assert cli.main(["add-defaults", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["defaults"]["only_amazon"] is True


def test_the_inventory_preview_warns_when_a_spreadsheet_mixes_two_tags(hub):
    from influencer_hub import inventory

    db.add_influencer("Ravi Loots", "ravi-21")
    db.add_influencer("Sita Deals", "sita-21")
    text = inventory.csv_text()
    rows = text.splitlines()
    rows[2] = rows[2].replace("sita-21", "ravi-21")        # Sita now wants Ravi's tag
    plan = inventory.plan_import("\n".join(rows) + "\n")

    assert plan["issues"] == []                            # it is applicable …
    assert plan["warnings"], "the plan must warn about the mixed tag"
    assert "Ravi Loots" in plan["warnings"][0]["detail"]
    assert plan["warnings"][0]["line"] == 3
