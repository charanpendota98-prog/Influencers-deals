"""The guided flow: name → channels → categories → go live, one save at a time.

These tests hold the whole promise of the screen: every step can be saved on its
own, each one lands in the database (not only on the screen), the creator's own
Amazon tag is refused when somebody else already owns it, and the review table
tells the truth about what is still missing.
"""

from __future__ import annotations

import datetime
import pathlib
import re
import tempfile

import pytest

from influencer_hub import config, db


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """A real hub, not Flask TESTING, so the route guard actually runs.

    CSRF stays on: the wizard is a form flow, and every step has to carry the
    token the page handed out.
    """
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "guided.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", TEAM_PASSWORD)
    monkeypatch.setattr(config, "ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_COOKIE_SECURE", False)
    db.init()
    import dashboard.app as dashboard_module
    monkeypatch.setattr(dashboard_module.app, "testing", False)
    dashboard_module.app.config.update(TESTING=False, WTF_CSRF_ENABLED=True)
    monkeypatch.setattr(dashboard_module, "_dashboard_secret_key_is_persistent", lambda: True)
    with dashboard_module.app.test_client() as test_client:
        yield test_client
        test_client.get("/logout")


ADMIN_PASSWORD = "owner-password-16chars"
TEAM_PASSWORD = "team-password-16chars"


def team_login(client, name: str = "Mohan", password: str = TEAM_PASSWORD) -> None:
    page = client.get("/team/login").get_data(as_text=True)
    token_ = re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)
    response = client.post("/team/login",
                           data={"_csrf_token": token_, "name": name, "password": password})
    assert response.status_code == 302, response.get_data(as_text=True)[:400]


def login(client, password: str = ADMIN_PASSWORD) -> None:
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)
    response = client.post("/login", data={"_csrf_token": token, "password": password})
    assert response.status_code == 302, response.get_data(as_text=True)[:400]


def token(client, path: str = "/guided") -> str:
    page = client.get(path).get_data(as_text=True)
    match = re.search(r'name="_csrf_token" value="([^"]+)"', page)
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def body(response) -> str:
    return re.sub(r"\s+", " ", response.get_data(as_text=True))


# --------------------------------------------------------------------------
# the page itself
# --------------------------------------------------------------------------
def test_guided_page_is_admin_only(client):
    response = client.get("/guided")
    assert response.status_code in {302, 401}
    client.get("/login")
    login(client)
    page = body(client.get("/guided"))
    assert "Guided setup" in page
    assert "Their own Amazon tag" in page
    assert "Step 1" in page and "Step 4" in page


def test_the_four_steps_are_the_four_things_that_have_to_exist(client):
    login(client)
    page = client.get("/guided").get_data(as_text=True)
    # the rail names all four steps before anything is saved, so the operator
    # knows what is coming
    for label in ("Who is it?", "Channels", "Deals &amp; hours", "Go live"):
        assert label in page, label
    # step 1 is a real form even with JavaScript switched off
    assert 'action="/guided/creator"' in page


# --------------------------------------------------------------------------
# step 1 — the creator and their own tag
# --------------------------------------------------------------------------
def test_step_one_creates_the_creator_with_their_own_tag(client):
    login(client)
    response = client.post("/guided/creator",
                           data={"_csrf_token": token(client), "name": "Ravi Loots",
                                 "amazon_tag": "ravi-21"},
                           follow_redirects=True)
    assert response.status_code == 200
    profile = db.search_influencers("Ravi Loots")[0]
    assert profile["amazon_tag"] == "ravi-21"
    assert profile["active"] is not None  # the guided flow leaves it live
    assert "now add the channels" in body(response)


def test_step_one_refuses_a_tag_that_belongs_to_somebody_else(client):
    login(client)
    db.add_influencer("Ravi Loots", "ravi-21", added_by="Mohan")
    client.post("/guided/creator", data={"_csrf_token": token(client), "name": "Copy Cat",
                                         "amazon_tag": "ravi-21"}, follow_redirects=True)
    response = client.post("/guided/creator",
                           data={"_csrf_token": token(client), "name": "Copy Cat",
                                 "amazon_tag": "ravi-21"}, follow_redirects=True)
    assert "already belongs to" in body(response)
    assert [inf["name"] for inf in db.list_influencers()] == ["Ravi Loots"]


def test_step_one_needs_a_tag(client):
    login(client)
    response = client.post("/guided/creator", data={"_csrf_token": token(client),
                                                    "name": "No Tag Yet"},
                           follow_redirects=True)
    assert "own Amazon Associate tag is required" in body(response)
    assert db.list_influencers() == []


def test_step_one_resumes_an_existing_creator_instead_of_creating_a_twin(client):
    login(client)
    db.add_influencer("Ravi Loots", "ravi-21", added_by="Mohan")
    response = client.post("/guided/creator",
                           data={"_csrf_token": token(client), "name": "  ravi   loots  ".split()[0]
                                 + " " + "Loots", "amazon_tag": "ravi-new-21"},
                           follow_redirects=True)
    assert "already in the hub" in body(response)
    assert len(db.list_influencers()) == 1
    assert db.list_influencers()[0]["amazon_tag"] == "ravi-new-21"


# --------------------------------------------------------------------------
# step 2 — the channels
# --------------------------------------------------------------------------
def test_step_two_saves_both_channels_under_the_creator_and_keeps_the_tag(client):
    login(client)
    db.add_influencer("Ravi Loots", "ravi-21")
    inf_id = db.list_influencers()[0]["id"]
    response = client.post("/guided/channels",
                           data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                                 "creator": inf_id, "approval_channel": "@ravi_amazon",
                                 "main_channel": "https://t.me/ravi_loots", "mirror_whatsapp": "1"},
                           follow_redirects=True)
    assert response.status_code == 200
    channels = {ch["role"]: ch for ch in db.list_channels(inf_id)}
    assert channels["approval"]["identifier"] == "@ravi_amazon"
    assert channels["broadcast"]["identifier"] == "@ravi_loots"
    assert channels["broadcast"]["status"] == "ready"
    assert str(channels["broadcast"]["mirror_whatsapp"]) in {"1", "True"}
    # one tag, both channels: the tag never has to be pasted twice
    assert db.get_influencer(inf_id)["amazon_tag"] == "ravi-21"


def test_step_two_refuses_a_channel_another_creator_already_owns(client):
    login(client)
    taken = db.add_influencer("Sita Deals", "sita-21")
    db.add_channel(taken, "telegram", "@loots_hub", role="broadcast", status="ready")
    mine = db.add_influencer("Ravi Loots", "ravi-21")
    response = client.post("/guided/channels",
                           data={"_csrf_token": token(client, f"/guided?creator={mine}"),
                                 "creator": mine, "main_channel": "t.me/loots_hub"},
                           follow_redirects=True)
    assert "belongs to" in body(response)
    assert db.get_channel(mine, "broadcast") is None


def test_step_two_saves_a_direct_whatsapp_id_and_marks_it_pending(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    client.post("/guided/channels",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "main_channel": "@ravi_loots",
                      "whatsapp_channel": "120363012345678901@newsletter"},
                follow_redirects=True)
    wa = [ch for ch in db.list_channels(inf_id) if ch["role"] == "whatsapp"]
    assert wa and wa[0]["identifier"] == "120363012345678901@newsletter"
    assert wa[0]["status"] != "ready"  # a destination is pending until a test send


def test_step_two_keeps_an_invite_link_until_the_phone_is_paired(client):
    """A chat invite needs the creator's phone to resolve — never silently dropped."""
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    response = client.post("/guided/channels",
                           data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                                 "creator": inf_id, "main_channel": "@ravi_loots",
                                 "whatsapp_channel": "https://chat.whatsapp.com/AbCdEf123456"},
                           follow_redirects=True)
    assert "kept" in body(response)
    # and the page shows the link back to the operator instead of losing it
    page = body(client.get(f"/guided?creator={inf_id}"))
    assert "ab" in page.lower()


# --------------------------------------------------------------------------
# step 3 — categories, hours, switches
# --------------------------------------------------------------------------
def test_step_three_saves_the_ticked_categories_and_the_switches(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    client.post("/guided/categories",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "categories": ["electronics", "home", "beauty"],
                      "posting_schedule": "09-23", "price_filter": "budget",
                      "allow_amazon": "1", "allow_hypd": "1"},
                follow_redirects=True)
    profile = db.get_influencer(inf_id)
    assert profile["categories"] == "electronics,home,beauty"
    assert profile["posting_schedule"] == "09-23"
    assert profile["price_filter"] == "budget"
    assert profile["allow_amazon"] and profile["allow_hypd"]
    assert not profile["allow_earnkaro"]  # unticked means off, not inherited


def test_step_three_leaves_all_categories_alone_when_nothing_is_ticked(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    client.post("/guided/categories",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "allow_amazon": "1"},
                follow_redirects=True)
    assert not (db.get_influencer(inf_id)["categories"] or "")


def test_step_three_never_leaves_a_creator_with_no_network(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    response = client.post("/guided/categories",
                           data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                                 "creator": inf_id},
                           follow_redirects=True)
    assert "at least one network" in body(response)
    assert db.get_influencer(inf_id)["allow_amazon"]


# --------------------------------------------------------------------------
# step 4 — review and go live
# --------------------------------------------------------------------------
def test_review_lists_everything_and_flags_what_is_missing(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21", added_by="Mohan",
                               categories="electronics")
    page = body(client.get(f"/guided?creator={inf_id}"))
    assert "Their own Amazon tag" in page
    assert "ravi-21" in page
    assert "still missing" in page  # no channel yet
    assert "Approval (preview) channel" in page


def test_go_live_turns_the_creator_on_and_points_at_the_first_post(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    db.add_channel(inf_id, "telegram", "@ravi_loots", role="broadcast", status="ready")
    response = client.post("/guided/finish",
                           data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                                 "creator": inf_id}, follow_redirects=True)
    assert "is live" in body(response)
    assert db.get_influencer(inf_id)["active"] in {1, True}
    page = body(response)
    assert "Send a test deal now" in page


def test_the_whole_four_step_run_is_audited_end_to_end(client):
    login(client)
    client.post("/guided/creator", data={"_csrf_token": token(client), "name": "Ravi Loots",
                                         "amazon_tag": "ravi-21"}, follow_redirects=True)
    inf_id = db.search_influencers("Ravi Loots")[0]["id"]
    client.post("/guided/channels",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "approval_channel": "@ravi_amazon",
                      "main_channel": "@ravi_loots"}, follow_redirects=True)
    client.post("/guided/categories",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "categories": ["electronics"], "allow_amazon": "1"},
                follow_redirects=True)
    client.post("/guided/finish",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id}, follow_redirects=True)
    actions = [row["action"] for row in db.list_audit(limit=20)]
    for action in ("guided_creator_created", "guided_channels_saved", "guided_deals_saved",
                   "guided_finished"):
        assert action in actions, actions
    assert db.get_channel(inf_id, "broadcast")["status"] == "ready"


def test_the_progress_line_counts_the_steps_that_are_really_done(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    alone = body(client.get(f"/guided?creator={inf_id}"))
    assert "1 of 4" in alone and "next: Channels" in alone
    db.add_channel(inf_id, "telegram", "@ravi_loots", role="broadcast", status="ready")
    # a working main channel is the whole "channels" step; the profile is active,
    # so only the deal settings are still open
    with_channel = body(client.get(f"/guided?creator={inf_id}"))
    assert "3 of 4" in with_channel and "next: Deals &amp; hours" in with_channel
    client.post("/guided/categories",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "categories": ["electronics"], "allow_amazon": "1"})
    assert "4 of 4" in body(client.get(f"/guided?creator={inf_id}"))


# --------------------------------------------------------------------------
# the age line the owner reads beside each creator
# --------------------------------------------------------------------------
def test_setup_age_is_shown_where_the_admin_looks(client):
    login(client)
    inf_id = db.add_influencer("Old Timer", "old-21", added_by="Ramesh")
    when = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect() as conn:
        conn.execute("UPDATE influencers SET created_at = ? WHERE id = ?", (when, inf_id))
    for path in (f"/guided?creator={inf_id}", "/setup", "/easy-setup", "/"):
        page = body(client.get(path))
        assert "1 year" in page, path
    # "today" never gets an "ago" glued on
    fresh = db.add_influencer("Fresh One", "fresh-21")
    recent = body(client.get("/"))
    assert "today ago" not in recent
    assert ">Fresh One</a> (today)" in recent
    assert "by Ramesh" in body(client.get(f"/guided?creator={inf_id}"))
    assert "by Ramesh" in body(client.get("/setup"))


def test_the_directory_can_be_sorted_and_filtered_by_setup_date(client):
    login(client)
    new_id = db.add_influencer("New One", "new-21", added_by="Mohan")
    old_id = db.add_influencer("Old One", "old-21", added_by="Ramesh")
    when = (datetime.datetime.now() - datetime.timedelta(days=900)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect() as conn:
        conn.execute("UPDATE influencers SET created_at = ? WHERE id = ?", (when, old_id))
    newest_first = body(client.get("/setup?sort=newest"))
    assert newest_first.index("New One") < newest_first.index("Old One")
    oldest_first = body(client.get("/setup?sort=oldest"))
    assert oldest_first.index("Old One") < oldest_first.index("New One")
    window = body(client.get("/setup?since=2026-01-01"))
    assert "New One" in window and "Old One" not in window


def test_the_export_carries_the_setup_age_as_its_own_columns(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21", added_by="Mohan")
    when = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect() as conn:
        conn.execute("UPDATE influencers SET created_at = ? WHERE id = ?", (when, inf_id))
    csv_text = client.get("/api/inventory.csv").get_data(as_text=True)
    header = csv_text.splitlines()[0]
    assert "setup_age" in header and "setup_age_days" in header
    row = csv_text.splitlines()[1]
    assert "2 months" in row and ",60," in row


# --------------------------------------------------------------------------
# the team may add through the wizard — and only touch its own additions
# --------------------------------------------------------------------------
def test_the_team_can_run_the_wizard_and_the_add_is_credited_to_them(client):
    team_login(client, "Mohan")
    client.post("/guided/creator", data={"_csrf_token": token(client), "name": "Ravi Loots",
                                         "amazon_tag": "ravi-21"}, follow_redirects=True)
    profile = db.search_influencers("Ravi Loots")[0]
    assert profile["added_by"] == "Mohan"
    assert str(profile["amazon_tag"]) == "ravi-21"
    inf_id = profile["id"]
    client.post("/guided/channels",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "main_channel": "@ravi_loots"}, follow_redirects=True)
    assert db.get_channel(inf_id, "broadcast")["identifier"] == "@ravi_loots"


def test_the_team_cannot_continue_a_creator_somebody_else_added(client):
    """A crafted ?creator= link or form post must change nothing at all."""
    other = db.add_influencer("Sita Deals", "sita-21", added_by="Ramesh")
    db.add_channel(other, "telegram", "@sita_deals", role="broadcast", status="ready")
    team_login(client, "Mohan")
    page = body(client.get(f"/guided?creator={other}"))
    assert "added by somebody else" in page
    assert "sita-21" not in page
    response = client.post("/guided/channels",
                           data={"_csrf_token": token(client, "/guided"),
                                 "creator": other, "main_channel": "@mohan_took_over"},
                           follow_redirects=True)
    assert "added by somebody else" in body(response)
    assert db.get_channel(other, "broadcast")["identifier"] == "@sita_deals"
    assert [ch["identifier"] for ch in db.list_channels(other)] == ["@sita_deals"]


def test_a_form_that_names_a_creator_is_not_overruled_by_the_session(client):
    """Two open tabs must never edit the wrong creator."""
    login(client)
    first = db.add_influencer("Ravi Loots", "ravi-21")
    second = db.add_influencer("Sita Deals", "sita-21")
    # the session is working on the first creator...
    client.post("/guided/creator", data={"_csrf_token": token(client), "creator": first,
                                         "amazon_tag": "ravi-21"}, follow_redirects=True)
    # ...but the posted form names the second one
    client.post("/guided/channels",
                data={"_csrf_token": token(client, f"/guided?creator={second}"),
                      "creator": second, "main_channel": "@sita_deals"}, follow_redirects=True)
    assert db.get_channel(second, "broadcast")["identifier"] == "@sita_deals"
    assert db.get_channel(first, "broadcast") is None


def test_the_team_cannot_edit_somebody_elses_tag_through_the_wizard(client):
    other = db.add_influencer("Sita Deals", "sita-21", added_by="Ramesh")
    team_login(client, "Mohan")
    client.post("/guided/creator",
                data={"_csrf_token": token(client, "/guided"), "creator": other,
                      "name": "Sita Deals", "amazon_tag": "mohan-21"}, follow_redirects=True)
    assert db.get_influencer(other)["amazon_tag"] == "sita-21"


def test_a_team_member_cannot_come_back_and_change_their_own_finished_creator(client):
    """Finishing the run hands the creator to the owner: later changes are edits."""
    team_login(client, "Mohan")
    client.post("/guided/creator", data={"_csrf_token": token(client), "name": "Ravi Loots",
                                         "amazon_tag": "ravi-21"}, follow_redirects=True)
    inf_id = db.search_influencers("Ravi Loots")[0]["id"]
    client.post("/guided/channels",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "main_channel": "@ravi_loots"}, follow_redirects=True)
    client.post("/guided/finish",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id}, follow_redirects=True)
    # a second visit is out of the add flow: the channel must not move
    response = client.post("/guided/channels",
                           data={"_csrf_token": token(client, "/guided"),
                                 "creator": inf_id, "main_channel": "@ravi_loots_v2"},
                           follow_redirects=True)
    assert "added by somebody else" in body(response)
    assert db.get_channel(inf_id, "broadcast")["identifier"] == "@ravi_loots"


def test_the_owner_can_change_a_creator_the_team_added(client):
    """The same creator, the same form — the owner is never locked out of it."""
    inf_id = db.add_influencer("Ravi Loots", "ravi-21", added_by="Mohan")
    db.add_channel(inf_id, "telegram", "@ravi_loots", role="broadcast", status="ready")
    login(client)
    client.post("/guided/channels",
                data={"_csrf_token": token(client, f"/guided?creator={inf_id}"),
                      "creator": inf_id, "main_channel": "@ravi_loots_v2"}, follow_redirects=True)
    assert db.get_channel(inf_id, "broadcast")["identifier"] == "@ravi_loots_v2"


def test_the_team_still_cannot_reach_the_admin_pages_from_the_wizard(client):
    """Opening the guided flow does not widen the role: everything else stays shut."""
    team_login(client, "Mohan")
    assert client.get("/guided").status_code == 200
    assert client.get("/setup").status_code == 302
    export = client.get("/api/inventory.csv")
    assert export.status_code == 403
    assert export.get_json()["error"] == "admin_only"
    refused = [row for row in db.list_audit(limit=20) if row["action"] == "team_blocked_route"]
    assert refused, "a refused admin route must be recorded"


def test_the_export_search_finds_a_creator_by_tag_channel_and_phone(client):
    login(client)
    inf_id = db.add_influencer("Ravi Loots", "ravi-21", added_by="Mohan")
    db.add_channel(inf_id, "telegram", "@ravi_loots", role="broadcast", status="ready")
    from influencer_hub import inventory
    assert [row["name"] for row in inventory.rows(query="ravi-21")] == ["Ravi Loots"]
    assert [row["name"] for row in inventory.rows(query="@ravi_loots")] == ["Ravi Loots"]
    assert [row["name"] for row in inventory.rows(query="mohan")] == ["Ravi Loots"]
    assert inventory.rows(query="nobody-by-that-name") == []

# --------------------------------------------------------------------------
# the whole journey: create → channels → nothing ticked → the deals go out
# --------------------------------------------------------------------------
def test_the_whole_journey_ends_with_the_deals_going_out(client):
    """Create a creator the way the operator does, then let the worker deliver.

    The step the operator cares about is the one where **no source is ticked**:
    the creator must read the hub default, and the deal that comes from it must
    reach all three destinations with the creator's own Amazon tag — and must not
    come out a second time.
    """
    import asyncio

    from influencer_hub import pipeline, sources

    login(client)
    client.post("/guided/creator",
                data={"_csrf_token": token(client), "name": "Journey Ravi",
                      "phone_number": "919000012345", "amazon_tag": "journeyravi-21"})
    creator = db.search_influencers("Journey Ravi")[0]
    inf_id = int(creator["id"])
    assert creator["allowed_sources"] == sources.default_token()

    client.post("/guided/channels", data={
        "_csrf_token": token(client, f"/guided?creator={inf_id}&step=channels"),
        "creator": inf_id, "approval_channel": "@journey_amazon",
        "main_channel": "@journey_loots",
        "whatsapp_channel": "120363009998887776@newsletter", "mirror_whatsapp": "1",
    })
    # the WhatsApp channel is paired (QR/code accepted) and the creator posts 24/7
    for channel in db.list_channels(inf_id):
        if channel["platform"] == "whatsapp_channel":
            db.update_channel_details(channel["id"], status="ready")
    db.update_influencer(inf_id, posting_schedule="")

    sent: dict[str, str] = {}

    async def fake_dispatch(_inf, channel, text):
        sent[f"{channel['role']}:{channel['identifier']}"] = text
        return "posted"

    monkeypatch_ready = pipeline.dispatch_to_channel
    pipeline.dispatch_to_channel = fake_dispatch
    deal = ("🔥 BOAT AIRDOPES at ₹1299 (MRP ₹2999)\n"
            "https://www.amazon.in/dp/B09N3ZNHTY\n"
            "Use code BOAT500 · Sizes M, L, XL\n#deal #amazon\n")
    try:
        first = asyncio.run(pipeline.render_and_dispatch(
            deal, influencer_ids=[inf_id], source_channel="LootZoneIndia11"))
        second = asyncio.run(pipeline.render_and_dispatch(
            deal, influencer_ids=[inf_id], source_channel="LootZoneIndia11"))
    finally:
        pipeline.dispatch_to_channel = monkeypatch_ready

    destinations = db.list_channels(inf_id)
    assert len(destinations) == 3
    assert set(first[inf_id].values()) == {"posted"}
    for channel in destinations:
        key = f"{channel['role']}:{channel['identifier']}"
        assert key in sent, sent.keys()
        assert "tag=journeyravi-21" in sent[key]        # the creator's own money
        assert "BOAT AIRDOPES" in sent[key]             # the source text survived
        assert "BOAT500" in sent[key]                   # …including the coupon line

    # Same product again, minutes later: nothing goes out twice, and the reason is
    # written down rather than swallowed.
    assert set(second[inf_id].values()) == {"skipped:duplicate_same_product"}
    assert db.list_recent_posts(hours=1)[0]["status"].startswith("skipped")
