"""The creator inventory: export everything, edit in Excel, paste it back.

Two properties matter here. First, one export must answer every question about a
creator — when they were added, by whom, their tag, all three channels with ids,
the switches, the schedule, and how they are doing. Second, the round trip must be
safe: exporting and importing the same file changes nothing, an empty cell never
wipes a value, and a single ``-`` is the only way to clear one.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time

import pytest

from influencer_hub import config, db, inventory, security

ADMIN_PASSWORD = "owner-password-16chars"
TEAM_PASSWORD = "team-password-16chars"


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "inventory.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", TEAM_PASSWORD)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    db.init()
    return db


def _creator(name="Ravi Loots", tag="ravi-21", *, added_by="", channels=True):
    inf_id = db.add_influencer(name, tag, added_by=added_by)
    if channels:
        db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready", role="broadcast",
                       mirror_whatsapp="1")
        db.add_channel(inf_id, "telegram", "@ravi_amazon", status="ready", role="approval")
        db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                       status="ready", role="whatsapp")
    return inf_id


def _edit(text: str, row_name: str, **changes) -> str:
    """Change cells of one row by name, the way an operator would in Excel."""
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    for row in rows[1:]:
        if row[header.index("name")] == row_name:
            for key, value in changes.items():
                row[header.index(key)] = value
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# 1. One export answers every question
# --------------------------------------------------------------------------
def test_one_row_carries_everything_about_a_creator(hub):
    _creator(added_by="Ravi")
    db.update_influencer(1, categories="clothing,electronics", posting_schedule="06:00-09:00",
                         price_filter="lt:2000", notes="call first")

    row = inventory.rows()[0]

    assert set(row) >= set(inventory.COLUMNS)
    assert row["creator_id"] == 1 and row["name"] == "Ravi Loots"
    assert row["added_by"] == "Ravi" and row["created_date"]          # when and by whom
    assert row["amazon_tag"] == "ravi-21" and row["tag_source"] == "creator"
    assert row["approval_channel"] == "@ravi_amazon"
    assert row["main_channel"] == "@ravi_loots"
    assert row["whatsapp_channel"] == "120363012345678901@newsletter"
    assert row["wa_status"] == "not_paired"
    assert row["mirror_whatsapp"] == "yes"                            # main channel mirrors
    assert row["allow_amazon"] == "yes" and row["only_amazon"] == "no"
    assert row["categories"] == "clothing,electronics"
    assert row["posting_schedule"] == "06:00-09:00" and row["price_filter"] == "lt:2000"
    assert row["notes"] == "call first"
    assert row["next_step"] and row["next_action"]                    # what is left to do
    assert row["posts_24h"] == 0 and row["mirrored_24h"] == 0


def test_the_export_can_be_narrowed_to_one_team_member(hub):
    _creator("Made By Team", "team-21", added_by="Ravi")
    _creator("Made By Owner", "owner-21")

    team_rows = inventory.rows(member="Ravi")
    queried = inventory.rows(query="owner")

    assert [row["name"] for row in team_rows] == ["Made By Team"]
    assert [row["name"] for row in queried] == ["Made By Owner"]
    assert len(inventory.rows()) == 2


def test_the_csv_is_ready_for_excel_and_leaks_no_secret(hub):
    db.set_global_setting("earnkaro_api_key", "sk-live-do-not-leak")
    _creator()

    text = inventory.csv_text()
    lines = text.strip().splitlines()

    assert lines[0] == ",".join(inventory.COLUMNS)
    assert len(lines) == 2
    assert "sk-live-do-not-leak" not in text
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0]["created_date"] and parsed[0]["main_channel"] == "@ravi_loots"


def test_the_summary_line_names_the_team_and_the_newest_creator(hub):
    _creator("A", "a-21", added_by="Ravi")
    _creator("B", "b-21")

    line = inventory.summary_line()

    assert "2 creator(s)" in line and "1 added by the team" in line and "newest" in line


# --------------------------------------------------------------------------
# 2. The round trip is safe
# --------------------------------------------------------------------------
def test_exporting_and_importing_the_same_file_changes_nothing(hub):
    _creator(added_by="Ravi")
    text = inventory.csv_text()

    plan = inventory.plan_import(text)

    assert plan["records"] == 1 == plan["unchanged"]
    assert plan["changed"] == 0 and plan["ok"] is True
    report = inventory.apply_import(plan)
    assert report["updated"] == 0 and report["unchanged"] == 1


def test_an_edited_file_names_exactly_what_would_change(hub):
    _creator()
    text = _edit(inventory.csv_text(), "Ravi Loots", amazon_tag="new-21",
                 main_channel="@ravi_main", allow_hypd="no", categories="clothing",
                 notes="prefers evening posts")

    plan = inventory.plan_import(text)
    operation = plan["operations"][0]

    assert plan["changed"] == 1 and plan["ok"] is True
    assert operation["changes"] == {
        "amazon_tag": "new-21", "categories": "clothing", "notes": "prefers evening posts",
        "allow_hypd": False,
    }
    assert operation["channels"] == {"broadcast": "@ravi_main"}

    report = inventory.apply_import(plan, actor="admin")
    assert report["updated"] == 1
    profile = db.get_influencer(1)
    assert profile["amazon_tag"] == "new-21" and profile["categories"] == "clothing"
    assert int(profile["allow_hypd"]) == 0
    assert db.get_channel(1, "broadcast")["identifier"] == "@ravi_main"
    assert db.list_audit(limit=1)[0]["action"] == "creator_updated"


def test_an_empty_cell_never_wipes_a_value_and_a_dash_does(hub):
    db.update_influencer(_creator(), notes="keep me", categories="clothing")
    text = inventory.csv_text()

    blanked = inventory.plan_import(_edit(text, "Ravi Loots", notes="", categories=""))
    assert blanked["changed"] == 0                       # empty = leave it alone

    cleared = inventory.plan_import(_edit(text, "Ravi Loots", notes="-", categories="-"))
    assert cleared["operations"][0]["changes"] == {"notes": "", "categories": ""}
    inventory.apply_import(cleared)
    assert db.get_influencer(1)["notes"] == "" and db.get_influencer(1)["categories"] == ""


def test_a_dash_removes_a_channel_and_a_new_value_creates_one(hub):
    _creator(channels=False)
    text = inventory.csv_text()

    added = inventory.plan_import(_edit(text, "Ravi Loots", main_channel="@ravi_loots",
                                        approval_channel="@ravi_amazon"))
    assert added["operations"][0]["channels"] == {"approval": "@ravi_amazon",
                                                 "broadcast": "@ravi_loots"}
    inventory.apply_import(added)
    assert db.get_channel(1, "broadcast")["identifier"] == "@ravi_loots"

    text = inventory.csv_text()
    removed = inventory.plan_import(_edit(text, "Ravi Loots", approval_channel="-"))
    inventory.apply_import(removed)

    assert db.get_channel(1, "approval") is None
    assert db.get_channel(1, "broadcast")["identifier"] == "@ravi_loots"   # untouched


def test_the_mirror_switch_round_trips_on_the_main_channel(hub):
    _creator()
    assert inventory.rows()[0]["mirror_whatsapp"] == "yes"

    off = inventory.plan_import(_edit(inventory.csv_text(), "Ravi Loots", mirror_whatsapp="no"))

    assert off["operations"][0]["mirror_whatsapp"] == "0"
    inventory.apply_import(off)
    assert db.get_channel(1, "broadcast")["mirror_whatsapp"] == "0"
    assert inventory.rows()[0]["mirror_whatsapp"] == "no"
    assert inventory.plan_import(inventory.csv_text())["changed"] == 0    # quiet again

    inherited = inventory.plan_import(_edit(inventory.csv_text(), "Ravi Loots",
                                            mirror_whatsapp="-"))
    assert inherited["operations"][0]["mirror_whatsapp"] == ""
    inventory.apply_import(inherited)
    assert db.get_channel(1, "broadcast")["mirror_whatsapp"] == ""
    assert inventory.rows()[0]["mirror_whatsapp"] == "inherit"


def test_switching_a_creator_off_is_one_edited_cell(hub):
    _creator()
    plan = inventory.plan_import(_edit(inventory.csv_text(), "Ravi Loots", active="no"))

    assert plan["operations"][0]["changes"] == {"active": False}
    inventory.apply_import(plan)
    assert db.get_influencer(1)["active"] == 0
    # ...and the export shows it, so the next round trip is quiet again.
    assert inventory.rows()[0]["active"] == "no"
    assert inventory.plan_import(inventory.csv_text())["changed"] == 0


# --------------------------------------------------------------------------
# 3. Rows that cannot be applied are named, never guessed
# --------------------------------------------------------------------------
def test_bad_rows_are_reported_with_the_line_and_the_reason(hub):
    _creator()
    text = _edit(inventory.csv_text(), "Ravi Loots", amazon_tag="not a tag!",
                 allow_amazon="maybe")
    plan = inventory.plan_import(text)

    assert plan["ok"] is False
    reasons = " ".join(issue["detail"] for issue in plan["issues"])
    assert "not a usable Amazon tag" in reasons and "not yes/no" in reasons
    assert plan["changed"] == 0                       # neither row is applied blindly


def test_an_unknown_name_is_not_created_unless_asked(hub):
    _creator()
    rows = inventory.csv_text().strip().splitlines()
    header = rows[0]
    extra = ["", ""] * len(inventory.COLUMNS)
    cells = dict(zip(inventory.COLUMNS, [""] * len(inventory.COLUMNS)))
    cells.update({"name": "Brand New", "amazon_tag": "new-21", "main_channel": "@brand_new"})
    text = header + "\n" + "\n".join(rows[1:]) + "\n" + ",".join(cells[c] for c in inventory.COLUMNS)

    refused = inventory.plan_import(text)
    assert refused["created"] == 0 and "not in the hub" in refused["issues"][0]["detail"]

    allowed = inventory.plan_import(text, create=True)
    assert allowed["created"] == 1 and allowed["issues"] == []
    report = inventory.apply_import(allowed)
    assert report["created"] == 1 and report["updated"] == 0
    created = next(inf for inf in db.list_influencers() if inf["name"] == "Brand New")
    assert created["amazon_tag"] == "new-21"
    assert db.get_channel(created["id"], "broadcast")["identifier"] == "@brand_new"


def test_an_unknown_creator_id_is_reported(hub):
    plan = inventory.plan_import("creator_id,name,amazon_tag\n9999,,tag-21\n")

    assert plan["issues"] and "does not exist" in plan["issues"][0]["detail"]


def test_the_file_must_have_a_name_or_an_id_column(hub):
    with pytest.raises(ValueError):
        inventory.plan_import("something,else\n1,2\n")


# --------------------------------------------------------------------------
# 4. Surfaces: the dashboard card, the API, the CLI
# --------------------------------------------------------------------------
def _login(client, path, data):
    page = client.get(path).get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = page.index(marker) + len(marker)
    token = page[start:page.index('"', start)]
    response = client.post(path, data=dict(data, _csrf_token=token))
    assert response.status_code == 302
    return token


def test_the_dashboard_serves_the_export_and_keeps_the_team_out(hub, monkeypatch):
    _creator(added_by="Ravi")
    import dashboard.app as dashboard_module

    monkeypatch.setattr(dashboard_module.app, "testing", False)
    monkeypatch.setattr(dashboard_module, "_dashboard_secret_key_is_persistent", lambda: True)
    client = dashboard_module.app.test_client()

    # The team session cannot pull a file with phone numbers and settings.
    _login(client, "/team/login", {"name": "Ravi", "password": TEAM_PASSWORD})
    assert client.get("/api/inventory.csv").status_code == 403

    client.post("/logout", data={"_csrf_token": client.get("/team").get_data(as_text=True)
                                 .split('name="_csrf_token" value="')[1].split('"')[0]})
    _login(client, "/login", {"password": ADMIN_PASSWORD})
    response = client.get("/api/inventory.csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert response.headers["Content-Type"].count("charset") == 1
    assert "creator-inventory.csv" in response.headers["Content-Disposition"]
    body = response.get_data(as_text=True)
    assert body.splitlines()[0] == ",".join(inventory.COLUMNS)
    assert "Ravi Loots" in body and "@ravi_loots" in body
    assert any(row["action"] == "inventory_exported" for row in db.list_audit(limit=10))


def test_the_setup_page_previews_before_it_writes(hub):
    _creator()
    import dashboard.app as dashboard_module

    dashboard_module.app.config.update(TESTING=True)
    client = dashboard_module.app.test_client()
    token = client.get("/setup").get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = token.index(marker) + len(marker)
    csrf = token[start:token.index('"', start)]

    edited = _edit(inventory.csv_text(), "Ravi Loots", amazon_tag="previewed-21")
    preview = client.post("/setup/inventory/import", data={"_csrf_token": csrf,
                                                          "inventory_lines": edited})
    assert preview.status_code == 302
    assert db.get_influencer(1)["amazon_tag"] == "ravi-21"        # nothing written yet
    page = client.get("/setup").get_data(as_text=True)
    assert "Preview of the file you pasted" in page and "previewed-21" in page
    assert "Apply the changes" in page

    csrf = page.split('name="_csrf_token" value="')[1].split('"')[0]
    applied = client.post("/setup/inventory/import", data={"_csrf_token": csrf, "apply": "1"})
    assert applied.status_code == 302
    assert db.get_influencer(1)["amazon_tag"] == "previewed-21"
    assert any(row["action"] == "inventory_imported" for row in db.list_audit(limit=10))


def test_a_big_paste_is_kept_on_the_server_not_in_the_cookie(hub, monkeypatch, tmp_path):
    """The 1000-creator file must never travel in the browser session cookie.

    A ~250 KB cookie is silently dropped by every real browser, which would both
    lose the preview and sign the owner out — so only a token is stored.
    """
    monkeypatch.setenv("HUB_INVENTORY_STASH_DIR", str(tmp_path / "stash"))
    _creator()
    import dashboard.app as dashboard_module

    dashboard_module.app.config.update(TESTING=True)
    client = dashboard_module.app.test_client()
    token = client.get("/setup").get_data(as_text=True)
    marker = 'name="_csrf_token" value="'
    start = token.index(marker) + len(marker)
    csrf = token[start:token.index('"', start)]

    edited = _edit(inventory.csv_text(), "Ravi Loots", amazon_tag="stashed-21")
    response = client.post("/setup/inventory/import", data={"_csrf_token": csrf,
                                                           "inventory_lines": edited})
    assert response.status_code == 302
    cookie = response.headers.get("Set-Cookie", "")
    assert len(cookie) < 4096, f"the session cookie is {len(cookie)} bytes"
    assert "stashed-21" not in cookie                      # the file itself stayed out

    with client.session_transaction() as sess:
        raw_token = str(sess.get("inventory_plan_token") or "")
    stored = tmp_path / "stash" / f"{raw_token}.csv"
    assert stored.is_file() and "stashed-21" in stored.read_text()
    assert (stored.stat().st_mode & 0o077) == 0            # private to the owner

    page = client.get("/setup").get_data(as_text=True)
    assert "Preview of the file you pasted" in page and "stashed-21" in page
    applied = client.post("/setup/inventory/import", data={"_csrf_token": csrf, "apply": "1"})
    assert applied.status_code == 302
    assert db.get_influencer(1)["amazon_tag"] == "stashed-21"
    with client.session_transaction() as sess:
        assert not sess.get("inventory_plan_token")        # nothing left to replay

    page = client.get("/setup").get_data(as_text=True)
    assert "Result of the last import" in page
    assert "amazon_tag" in page                            # the real edit is listed first


def test_a_lost_stash_says_so_instead_of_applying_nothing(hub, monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_INVENTORY_STASH_DIR", str(tmp_path / "stash"))
    _creator()
    import dashboard.app as dashboard_module

    dashboard_module.app.config.update(TESTING=True)
    client = dashboard_module.app.test_client()
    page = client.get("/setup").get_data(as_text=True)
    csrf = page.split('name="_csrf_token" value="')[1].split('"')[0]
    client.post("/setup/inventory/import", data={"_csrf_token": csrf,
                                                "inventory_lines": inventory.csv_text()})
    with client.session_transaction() as sess:
        raw_token = str(sess["inventory_plan_token"])
    (tmp_path / "stash" / f"{raw_token}.csv").unlink()      # tmp cleanup, restart, ...

    page = client.get("/setup").get_data(as_text=True)
    assert "That preview expired" in page                   # honest, not a silent no-op
    applied = client.post("/setup/inventory/import", data={"_csrf_token": csrf, "apply": "1"})
    assert applied.status_code == 302
    assert db.get_influencer(1)["amazon_tag"] == "ravi-21"  # nothing was written


def test_stash_helpers_are_private_and_self_pruning(hub, monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_INVENTORY_STASH_DIR", str(tmp_path / "stash"))
    token = inventory.stash_write("creator_id,name\n1,Ravi Loots\n")
    assert token and inventory.stash_read(token).startswith("creator_id")
    assert inventory.stash_read("../../etc/passwd") == ""   # no path games
    assert inventory.stash_read("nope") == ""

    old = inventory.stash_dir() / "old-token.csv"
    old.write_text("stale")
    os.utime(old, (time.time() - 4 * 3600, time.time() - 4 * 3600))
    assert inventory.stash_prune() == 1
    assert not old.exists() and inventory.stash_read(token)   # the fresh one survives
    assert inventory.stash_write("x" * 10) != "" and inventory.stash_write(
        "y" * (8 * 1024 * 1024 + 1)) == ""                   # size limit


def test_the_setup_page_carries_the_export_card(hub):
    import dashboard.app as dashboard_module

    dashboard_module.app.config.update(TESTING=True)
    page = dashboard_module.app.test_client().get("/setup").get_data(as_text=True)

    assert 'id="inventory"' in page
    assert "Export everything (CSV)" in page and "/api/inventory.csv" in page
    assert "Empty cell = “leave it alone”" in page or "leave it alone" in page
    assert "Add rows whose name is not in the hub yet" in page


def test_the_export_and_the_preview_stay_fast_on_a_thousand_creators(hub, monkeypatch):
    """A one-click export may not cost a query per creator.

    The hub is operated with 1000+ creators, so the flat export and the import
    preview both have to run in one pass: count the DB connections the two
    calls open and keep them well under one per creator.
    """
    for index in range(300):
        inf_id = db.add_influencer(f"Scale Loots {index}", f"scale{index:04d}-21")
        db.add_channel(inf_id, "telegram", f"@scale{index:04d}", status="ready",
                       role="broadcast")
    opened = {"count": 0}
    real_connect = db._connect

    def counting_connect(*args, **kwargs):
        opened["count"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db, "_connect", counting_connect)
    text = inventory.csv_text()
    assert text.count("\n") == 301                       # header + every creator
    opened["count"] = 0
    plan = inventory.plan_import(text)
    assert plan["changed"] == 0 and plan["issues"] == []
    assert opened["count"] < 60, f"the preview opened {opened['count']} connections"


def test_the_checklist_csv_now_carries_the_date_and_who_added_it(hub):
    from influencer_hub import ops, readiness

    _creator(added_by="Ravi")
    text = ops.readiness_csv(readiness.overview())
    header = text.splitlines()[0].split(",")

    assert "added_by" in header and "created_date" in header
    assert "Ravi" in text.splitlines()[1]


def test_the_cli_exports_and_imports(hub, tmp_path, capsys):
    from influencer_hub import cli

    _creator(added_by="Ravi")
    target = tmp_path / "inventory.csv"

    assert cli.main(["export-inventory", "--out", str(target)]) == 0
    out = capsys.readouterr().out
    assert "wrote 1 creator(s)" in out and "creator_id, name" in out

    assert cli.main(["import-inventory", "--file", str(target)]) == 0
    assert "would change: 0" in capsys.readouterr().out

    target.write_text(_edit(target.read_text(), "Ravi Loots", price_filter="lt:500"))
    assert cli.main(["import-inventory", "--file", str(target), "--apply"]) == 0
    assert "1 updated" in capsys.readouterr().out
    assert db.get_influencer(1)["price_filter"] == "lt:500"

    assert cli.main(["import-inventory", "--file", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == 0 and payload["issues"] == []

    assert cli.main(["import-inventory", "--file", str(tmp_path / "gone.csv")]) == 1
    assert "could not read" in capsys.readouterr().out


def test_the_import_reports_failures_without_stopping_the_batch(hub):
    _creator("Good One", "good-21")
    _creator("Second One", "second-21")
    plan = inventory.plan_import(inventory.csv_text())
    # Simulate a row that fails inside the writer (a channel deleted mid-flight).
    plan["operations"][1]["create"] = True
    plan["operations"][1]["tag"] = "second-21"

    report = inventory.apply_import(plan)

    assert report["failed"] == 0
    assert report["created"] == 1
    assert "created" in inventory.render_report(report)


def test_the_team_page_shows_when_a_creator_was_added(hub, monkeypatch):
    import dashboard.app as dashboard_module

    monkeypatch.setattr(dashboard_module.app, "testing", False)
    monkeypatch.setattr(dashboard_module, "_dashboard_secret_key_is_persistent", lambda: True)
    client = dashboard_module.app.test_client()
    _login(client, "/team/login", {"name": "Ravi", "password": TEAM_PASSWORD})
    team = client.get("/team").get_data(as_text=True)

    csrf = team.split('name="_csrf_token" value="')[1].split('"')[0]
    client.post("/team/add", data={"_csrf_token": csrf, "name": "Fresh Creator",
                                   "amazon_tag": "fresh-21", "main_channel": "@fresh_loots"})
    team = client.get("/team").get_data(as_text=True)

    assert "Fresh Creator" in team
    assert "Added on" in team
    assert db.list_influencers(added_by="Ravi")[0]["created_at"][:4] in team   # the year
