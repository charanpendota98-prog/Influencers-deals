from __future__ import annotations

from io import BytesIO
import re

from openpyxl import Workbook

from influencer_hub import config, db, source_workbook, sources


def workbook_bytes(*, second_source="https://t.me/tech_deals") -> bytes:
    workbook = Workbook()
    first = workbook.active
    first.title = "Electronics"
    first.append(["Channel Name", "Telegram Link"])
    first.append(["Tech Deals", "https://t.me/tech_deals"])
    first.append(["Bad row", "not a Telegram channel"])
    second = workbook.create_sheet("Home and Kitchen")
    second.append(["@home_deals"])
    second.append([second_source])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_excel_parser_loads_all_tabs_and_reports_bad_rows():
    parsed = source_workbook.parse_workbook(workbook_bytes())

    assert parsed["source_count"] == 3
    assert [sheet["sheet_name"] for sheet in parsed["sheets"]] == [
        "Electronics", "Home and Kitchen",
    ]
    assert parsed["sheets"][0]["sources"] == [
        {"name": "Tech Deals", "spec": "https://t.me/tech_deals"},
    ]
    assert parsed["sheets"][0]["invalid_rows"] == [3]
    assert parsed["invalid_count"] == 1
    assert len(parsed["sheets"][1]["sources"]) == 2


def test_sheet_import_deduplicates_channels_and_replaces_tab_membership(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sources.xlsx.sqlite3")
    db.init()
    parsed = source_workbook.parse_workbook(workbook_bytes())
    first_import = db.import_source_sheets("Creator Sources.xlsx", parsed["sheets"])

    assert first_import["sheet_count"] == 2
    assert first_import["source_count"] == 3
    assert first_import["new_sources"] == 2
    sheets = db.list_source_sheets()
    assert {row["sheet_name"] for row in sheets} == {"Electronics", "Home and Kitchen"}
    source_count_before = len(db.list_sources(active_only=False))

    updated = db.import_source_sheets(
        "Creator Sources.xlsx",
        [{"sheet_name": "Electronics", "sources": [
            {"name": "Tech Deals", "spec": "@tech_deals"},
            {"name": "New Tech", "spec": "https://t.me/new_tech_deals"},
        ]}],
    )

    assert updated["new_sources"] == 1
    current_sheets = db.list_source_sheets()
    electronics = next(row for row in current_sheets if row["sheet_name"] == "Electronics")
    assert electronics["source_count"] == 2
    assert len(db.list_sources(active_only=False)) == source_count_before + 1
    members = db.list_source_sheet_sources([electronics["id"]])[electronics["id"]]
    tokens = {sources.token_from_source_row(row) for row in members}
    assert tokens == {"tech_deals", "new_tech_deals"}


def test_easy_setup_imports_workbook_and_assigns_multiple_selected_sheets(
    monkeypatch, tmp_path,
):
    from dashboard.app import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "easy-source-sheets.sqlite3")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    db.init()
    monkeypatch.setitem(app.config, "TESTING", True)
    client = app.test_client()
    response = client.post(
        "/easy-setup/source-sheets/import",
        data={"source_workbook": (BytesIO(workbook_bytes()), "Creator Sources.xlsx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Import note: 1 invalid row(s)" in response.data
    assert b"Electronics: 3" in response.data
    sheets = db.list_source_sheets()
    assert len(sheets) == 2

    response = client.post("/easy-setup", data={
        "name": "Ravi Deals",
        "phone_number": "+91 98765 43210",
        "amazon_tag": "raviowner-21",
        "main_channel": "@ravi_deals",
        "categories_present": "1",
        "categories": ["electronics"],
        "source_sheets_present": "1",
        "source_sheet_ids": [str(row["id"]) for row in sheets],
        "allow_amazon": "1",
        "allow_earnkaro": "1",
        "allow_hypd": "1",
    }, follow_redirects=True)
    assert response.status_code == 200
    profile = db.list_influencers()[0]
    assert profile["phone_number"] == "+91 98765 43210"
    assert set(sources.selection_tokens(profile["allowed_sources"])) == {
        "tech_deals", "home_deals",
    }
    assert profile["categories"] == "electronics"
    assert profile["allow_amazon"] and profile["allow_earnkaro"] and profile["allow_hypd"]
    assert db.get_influencer_source_sheets(profile["id"]) == sorted(
        int(row["id"]) for row in sheets
    )
    page = response.get_data(as_text=True)
    for sheet in sheets:
        assert re.search(
            rf'name="source_sheet_ids" value="{sheet["id"]}"\s+checked', page
        )
    from influencer_hub.category_preferences import evaluate, is_strict
    assert is_strict(profile)
    assert evaluate("Smartphone deal", profile)["passes"]
    assert not evaluate("Cotton shirt deal", profile)["passes"]
    assert len(db.list_channels(profile["id"])) == 1
