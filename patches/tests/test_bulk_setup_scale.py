"""Scale: a hub with 1000+ creators must stay easy, fast and idempotent.

The command center, ``/api/readiness`` and the bulk paste are the three things
an operator uses once the hub is big, so these tests pin the properties that
make that possible:

* the readiness overview reads the database in a **bounded** number of queries
  (one for channels, one for sessions, one for mirror counts — not three per
  creator), which is what keeps a 1000-creator dashboard fast;
* the dashboard renders one filtered page, and the same filters/paging exist on
  the JSON API;
* pasting a thousand lines creates them, and pasting the same list again
  updates them instead of duplicating;
* bad lines are reported per line and never stop the batch.
"""
from __future__ import annotations

import re
import time

import pytest

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, readiness


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "scale.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    return app.test_client()


def _row_names(body: str) -> list[str]:
    """Creator names of the rendered readiness table rows, in page order."""
    rows = re.findall(r'<tr class="[^"]*" data-creator-row="\d+">(.*?)</tr>', body, re.S)
    names = []
    for row in rows:
        match = re.search(r"<strong>([^<]+)</strong>", row)
        if match:
            names.append(match.group(1).strip())
    return names


def _bulk(client, data: dict, **kwargs) -> str:
    """POST a bulk import and return the result page (following the token URL)."""
    response = client.post("/easy-setup/bulk", data=data, **kwargs)
    assert response.status_code == 302, response.status_code
    return client.get(response.headers["Location"]).get_data(as_text=True)


def _seed(count: int, *, prefix="Creator", tag_prefix="tag", with_wa=True) -> list[int]:
    """Create `count` creators quickly, each with a main channel (+ WhatsApp)."""
    ids = []
    for index in range(count):
        inf_id = db.add_influencer(f"{prefix} {index:04d}", f"{tag_prefix}{index}-21")
        db.add_channel(inf_id, "telegram", f"@{tag_prefix}{index}_main",
                       status="ready", role="broadcast")
        if with_wa:
            db.add_channel(inf_id, "whatsapp_channel", f"12036{index:08d}@newsletter",
                           status="pending", role="whatsapp")
        ids.append(inf_id)
    return ids


# --------------------------------------------------------------------------
# 1. The overview must not scale its query count with the hub size
# --------------------------------------------------------------------------
def test_a_thousand_creators_read_in_a_bounded_number_of_queries(client):
    _seed(400, prefix="Scale", tag_prefix="scale")

    connects = {"n": 0}
    real_connect = db._connect

    def counting_connect():
        connects["n"] += 1
        return real_connect()

    original = db._connect
    db._connect = counting_connect
    try:
        started = time.perf_counter()
        plan = readiness.overview()
        elapsed = time.perf_counter() - started
    finally:
        db._connect = original

    assert plan["summary"]["creators"] == 400
    # One query per table, not per creator: 400 creators would be >1200 reads
    # if this ever regressed to the per-creator helpers.
    assert connects["n"] < 40, f"readiness made {connects['n']} database connects"
    assert elapsed < 5, f"overview took {elapsed:.2f}s for 400 creators"


def test_the_dashboard_renders_one_filtered_page_for_a_big_hub(client):
    _seed(120, prefix="Page", tag_prefix="page")

    first = client.get("/").get_data(as_text=True)
    assert "120 creators in this hub" in first
    assert "Next →" in first
    # One page of rows only: 25 rows, starting at the first creator.
    assert len(_row_names(first)) == 25
    assert _row_names(first)[0] == "Page 0000"
    assert "Page 0119" not in _row_names(first)

    second = client.get("/?offset=25").get_data(as_text=True)
    assert _row_names(second)[0] == "Page 0025"
    assert "Page 0000" not in _row_names(second)
    assert "← Previous" in second
    assert "showing 26–50" in second

    # Search narrows to one row; the hub itself has nothing ready yet (no phone
    # is linked in this seed), which the state filter must say honestly.
    searched = client.get("/?q=Page 0007").get_data(as_text=True)
    assert "1 of 120 creators match" in searched
    assert _row_names(searched) == ["Page 0007"]

    ready = client.get("/?state=ready").get_data(as_text=True)
    assert _row_names(ready) == []
    assert "0 of 120 creators match" in ready

    assert client.get("/?state=nonsense").status_code == 200  # unknown filter = all


def test_the_json_api_serves_the_same_filters_and_paging(client):
    _seed(60, prefix="Api", tag_prefix="api")

    page = client.get("/api/readiness?limit=10&offset=20").get_json()
    assert page["total"] == 60
    assert page["matched"] == 60
    assert page["returned"] == 10
    assert page["offset"] == 20
    assert len(page["creators"]) == 10

    blocked = client.get("/api/readiness?state=blocked&limit=5").get_json()
    assert blocked["matched"] == 60           # every seeded creator still blocked
    assert blocked["returned"] == 5

    searched = client.get("/api/readiness?q=Api 0042").get_json()
    assert searched["matched"] == 1
    assert searched["creators"][0]["name"] == "Api 0042"

    nothing = client.get("/api/readiness?q=nobody-here").get_json()
    assert nothing["matched"] == 0
    assert nothing["creators"] == []


def test_the_easy_setup_creator_strip_pages_and_searches(client):
    _seed(60, prefix="Strip", tag_prefix="strip")

    first = client.get("/easy-setup").get_data(as_text=True)
    assert first.count("creator-pill") <= 45        # one page of pills
    assert "Showing 1–40 of 60" in first
    assert "Next 40 →" in first

    second = client.get("/easy-setup?pill_offset=40").get_data(as_text=True)
    strip = re.search(r'<div class="creator-strip">(.*?)</div>', second, re.S).group(1)
    assert "Strip 0040" in strip
    assert "Strip 0000" not in strip
    assert "← Previous" in second

    searched = client.get("/easy-setup?pill_q=Strip 0007").get_data(as_text=True)
    assert "Strip 0007" in searched
    assert searched.count("creator-pill") == 2      # the "New creator" pill + the match

    # Picking a creator that the current search would hide still shows its
    # readiness panel: the focused creator is never filtered out of their own page.
    focused = client.get("/easy-setup?pill_q=Strip 0059&creator=1").get_data(as_text=True)
    assert "Strip 0000" in focused
    assert "what is done, and what to do next" in focused


# --------------------------------------------------------------------------
# 2. Bulk paste: create thousands, then update them without duplicates
# --------------------------------------------------------------------------
def _bulk_lines(count: int, *, prefix="Bulk", tag_prefix="bulk") -> str:
    return "\n".join(
        f"{prefix} {index:04d}, {tag_prefix}{index:04d}-21, @{tag_prefix}{index}_main, "
        f"@{tag_prefix}{index}_approval, 12036{index:08d}@newsletter"
        for index in range(count)
    )


def test_bulk_paste_creates_hundreds_and_reports_every_line(client):
    lines = _bulk_lines(150)

    page = _bulk(client, {"bulk_lines": lines, "mirror_whatsapp": "1"})

    assert len(db.list_influencers()) == 150
    profiles = db.list_influencers(active_only=True)
    assert profiles[0]["amazon_tag"] == "bulk0000-21"
    channels = db.list_channels(profiles[0]["id"])
    roles = {channel["role"] for channel in channels}
    assert {"approval", "broadcast", "whatsapp"} <= roles
    assert db.get_channel(profiles[0]["id"], "whatsapp", "whatsapp_channel")["status"] == "pending"

    assert "Bulk setup — 150 line(s) processed" in page
    assert "Created" in page and ">150<" in page


def test_pasting_the_same_list_again_updates_instead_of_duplicating(client):
    lines = _bulk_lines(40, prefix="Again", tag_prefix="again")
    _bulk(client, {"bulk_lines": lines})
    assert len(db.list_influencers()) == 40

    # Same paste, one new tag: an idempotent upsert, not a second 40 profiles.
    edited = lines.replace("again0000-21", "again0000-updated-21")
    page = _bulk(client, {"bulk_lines": edited})

    assert len(db.list_influencers()) == 40
    assert db.search_influencers("Again 0000")[0]["amazon_tag"] == "again0000-updated-21"
    assert "Bulk setup — 40 line(s) processed" in page
    assert "Updated" in page


def test_bad_lines_are_reported_and_never_stop_the_batch(client):
    lines = "\n".join([
        "Good One, good1-21, @good1_main, @good1_approval",
        ",, ,",                                            # nothing usable
        "No Channels, nochannels-21",                      # no main/approval
        "Bad Tag, not a tag!, @bad_main",                  # invalid tag
        "Same Channel, samech-21, @same, @same",           # approval == main
        "Good Two, good2-21, @good2_main",
    ])

    page = _bulk(client, {"bulk_lines": lines})

    names = {inf["name"] for inf in db.list_influencers()}
    assert names == {"Good One", "Good Two"}

    assert "Same Channel" in page
    assert "approval and main channels are the same" in page
    assert "no main or approval channel" in page
    # Six pasted lines: the fully blank one is dropped, the other five are
    # processed and reported individually.
    assert "5 line(s) processed" in page


def test_a_csv_upload_with_headers_works_and_a_link_is_not_guessed(client):
    csv = "\n".join([
        "name,amazon_tag,broadcast_tg,approval_tg,whatsapp_id",
        "Csv One,csv1-21,@csv1_main,@csv1_approval,120369999999999999@newsletter",
        "Csv Two,csv2-21,@csv2_main,@csv2_approval,https://whatsapp.com/channel/ABC123",
    ])
    import io

    page = _bulk(
        client,
        {"bulk_file": (io.BytesIO(csv.encode("utf-8")), "creators.csv")},
        content_type="multipart/form-data",
    )

    rows = {row["name"]: row for row in db.list_influencers()}
    assert set(rows) == {"Csv One", "Csv Two"}
    one = db.get_channel(rows["Csv One"]["id"], "whatsapp", "whatsapp_channel")
    assert one and one["status"] == "pending"
    # The invite link needs a paired phone: reported, not stored as an identifier.
    two = [c for c in db.list_channels(rows["Csv Two"]["id"]) if c["platform"].startswith("whatsapp")]
    assert two == []

    assert "needs the creator&#39;s paired phone" in page or "needs the creator's paired phone" in page


def test_a_thousand_lines_land_in_one_submit(client):
    """The whole point: 1000+ creators in one paste, still fast."""
    started = time.perf_counter()
    _bulk(client, {"bulk_lines": _bulk_lines(1000)})
    elapsed = time.perf_counter() - started

    assert len(db.list_influencers()) == 1000
    assert elapsed < 60, f"1000-line import took {elapsed:.1f}s"

    # And every operator screen still answers quickly for that hub.
    started = time.perf_counter()
    assert client.get("/").status_code == 200
    dashboard_seconds = time.perf_counter() - started

    started = time.perf_counter()
    easy = client.get("/easy-setup").get_data(as_text=True)
    easy_seconds = time.perf_counter() - started

    assert dashboard_seconds < 10, f"dashboard took {dashboard_seconds:.1f}s"
    assert easy_seconds < 10, f"easy setup took {easy_seconds:.1f}s"
    # The creator strip is one page, not a thousand pills.
    assert easy.count("creator-pill") <= 45
    assert "of 1000 creators" in easy or "of 1000 match" in easy


def test_the_operator_pages_do_not_shrink_the_hub_to_one_page(client):
    """A thousand creators must not mean a thousand-card page anywhere."""
    _seed(300, prefix="Pages", tag_prefix="pages")

    setup = client.get("/setup").get_data(as_text=True)
    assert setup.count("creator-directory-card") <= 61      # one directory page
    # The creator dropdowns are capped at one page (200) plus the fixed controls
    # that do not grow with the hub: the sort selector and the state filter.
    assert setup.count("<option value=") <= 215
    assert "300 in this hub" in setup

    api = client.get("/api/readiness?limit=25").get_json()
    assert api["total"] == 300
    assert api["warnings_total"] >= 300                      # counted, not all shipped
    assert len(api["warnings"]) <= 50
    # 300 creators + the two hub-level steps (sources, worker) are waiting.
    assert api["actions_total"] >= 300
    assert len(api["actions"]) <= 200
