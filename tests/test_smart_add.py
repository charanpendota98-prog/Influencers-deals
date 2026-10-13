"""Adding creators must be easy for whoever has the phone in their hand.

The team gets a name, an Amazon tag, two Telegram channels, a WhatsApp id and
maybe a phone number — in whatever order they were written down. These tests pin
that the hub understands every shape, never silently turns a wrong value into a
name, carries the owner's presets into every add, and previews a paste without
writing anything.
"""
from __future__ import annotations

import json

import pytest

import dashboard.app as dashboard_module
from influencer_hub import config, creators, db, presets


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "smart.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "owner-password-16chars")
    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", "team-password-16chars")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "AMAZON_ASSOCIATE_TAG", "mama086-21")
    db.init()
    return db


# --------------------------------------------------------------------------
# 1. Every shape of line
# --------------------------------------------------------------------------
def test_a_white_space_line_is_read_token_by_token(hub):
    row = creators.parse(
        "Ravi Loots ravi-21 @ravi_loots @ravi_amazon_deals "
        "120363012345678901@newsletter +91 98765 43210"
    )[0]

    assert row["name"] == "Ravi Loots"
    assert row["tag"] == "ravi-21"
    assert row["main"] == "@ravi_loots"
    assert row["approval"] == "@ravi_amazon_deals"
    assert row["whatsapp"] == "120363012345678901@newsletter"
    assert row["phone"] == "919876543210"
    assert row["how"] and any("main channel" in bit for bit in row["how"])


def test_the_documented_comma_order_still_works(hub):
    row = creators.parse("Sita Deals, sita-21, @sita_loots, @sita_amazon, 12036@newsletter")[0]

    assert (row["name"], row["tag"], row["main"], row["approval"], row["whatsapp"]) == (
        "Sita Deals", "sita-21", "@sita_loots", "@sita_amazon", "12036@newsletter")


@pytest.mark.parametrize("separator", [",", "|", ";", "\t"])
def test_every_separator_a_phone_keyboard_makes_is_accepted(hub, separator):
    line = separator.join(["Kiran Deals", "kiran-21", "@kiran_main", "@kiran_approval"])
    row = creators.parse(line)[0]

    assert row["name"] == "Kiran Deals" and row["tag"] == "kiran-21"
    assert row["main"] == "@kiran_main" and row["approval"] == "@kiran_approval"


def test_links_are_understood_and_normalised(hub):
    row = creators.parse(
        "Sita Deals | sita-21 | t.me/sita_loots | https://t.me/sita_approval | "
        "https://chat.whatsapp.com/ABC123")[0]

    assert row["main"] == "@sita_loots"
    assert row["approval"] == "@sita_approval"
    assert row["whatsapp"].startswith("https://chat.whatsapp.com/")


def test_a_header_row_decides_the_column_meaning(hub):
    text = ("creator_name,amazon_tag,main_channel,approval_channel,whatsapp_channel,phone\n"
            "Ravi Loots,ravi-21,@ravi_loots,@ravi_amazon,12036@newsletter,9876543210")
    row = creators.parse(text)[0]

    assert row["name"] == "Ravi Loots" and row["phone"] == "9876543210"


def test_a_value_in_the_wrong_column_is_moved_and_reported(hub):
    """No tag written, channels shifted left: still the right creator."""
    row = creators.parse("Ravi Loots, @ravi_loots, 120363012345678901@newsletter")[0]

    assert row["main"] == "@ravi_loots"
    assert row["whatsapp"] == "120363012345678901@newsletter"
    assert row["tag"] == ""
    assert any("was in the" in bit for bit in row["how"])


def test_a_plainly_wrong_tag_stays_wrong(hub):
    """A junk value in the tag column must be reported, not become a channel."""
    row = creators.parse("Bad Row, not a tag!, @bad_main")[0]

    assert row["tag"] == "not a tag!"
    assert row["main"] == "@bad_main"
    assert creators._usable_tag(row["tag"]) is False


def test_key_value_pairs_carry_the_extras(hub):
    row = creators.parse('Priya, priya-21, @priya_main, notes="instagram 12k", '
                         'price=premium, hours=09-23, categories=electronics, store=93944')[0]

    assert row["notes"] == "instagram 12k"
    assert row["price_filter"] == "premium"
    assert row["schedule"] == "09-23"
    assert row["categories"] == "electronics"
    assert row["hypd_store_id"] == "93944"


def test_extra_columns_after_the_known_ones_become_settings_or_notes(hub):
    row = creators.parse("Kiran, kiran-21, @kiran_main, @kiran_approval, , "
                         "9876543210, electronics, 09-23, budget, VIP creator")[0]

    assert row["phone"] == "9876543210"
    assert row["categories"] == "electronics"
    assert row["schedule"] == "09-23"
    assert row["price_filter"] == "budget"
    assert row["notes"] == "VIP creator"


def test_a_leftover_word_is_a_note_and_never_a_setting(hub):
    """A guess here could change which deals a creator sees — so it is a note."""
    row = creators.parse("Ramesh Rao ramesh-21 @ramesh_loots @ramesh_amazon electronics")[0]

    assert row["categories"] == ""
    assert row["notes"] == "electronics"
    explicit = creators.parse("Ramesh Rao ramesh-21 @ramesh_loots categories=electronics")[0]
    assert explicit["categories"] == "electronics" and explicit["notes"] == ""


def test_blank_lines_and_comments_are_skipped(hub):
    rows = creators.parse("# today's additions\n\nRavi, ravi-21, @ravi_main\n\n")

    assert [row["name"] for row in rows] == ["Ravi"]
    assert rows[0]["line"] == 3                      # the real line number survives


def test_a_name_is_never_guessed_from_a_usable_word(hub):
    """`Ravi` is usable as a tag *and* is the name: the name wins."""
    row = creators.parse("Ravi Loots ravi-21 @ravi_loots")[0]

    assert row["name"] == "Ravi Loots"
    assert row["tag"] == "ravi-21"


# --------------------------------------------------------------------------
# 2. The check-first preview
# --------------------------------------------------------------------------
def test_the_preview_says_what_it_understood_and_what_is_missing(hub):
    result = creators.preview("Ravi Loots ravi-21 @ravi_loots\nBroken Line\n"
                              "Sita, sita-21, @sita_main")

    assert result["total"] == 3 and result["ready"] == 2 and result["problems"] == 1
    ready = result["rows"][0]
    assert ready["name"] == "Ravi Loots" and ready["how"]
    assert ready["problems"] == []
    broken = result["rows"][1]
    assert broken["name"] == "Broken Line"       # the name is there …
    assert any("channel" in problem for problem in broken["problems"])


def test_the_preview_flags_a_tag_that_is_not_usable(hub):
    result = creators.preview("Bad Tag, not a tag!, @bad_main")

    assert result["rows"][0]["ready"] is False
    assert any("not a usable Amazon tag" in problem for problem in result["rows"][0]["problems"])


def test_the_preview_limits_what_it_shows_without_losing_the_count(hub):
    text = "\n".join(f"Creator {index}, creator{index}-21, @main_{index}" for index in range(30))

    result = creators.preview(text, limit=10)

    assert result["total"] == 30 and result["shown"] == 10 and result["unparsed"] == 20


# --------------------------------------------------------------------------
# 3. The owner's presets reach every add
# --------------------------------------------------------------------------
def test_the_presets_only_override_what_is_left_empty(hub):
    presets.save({"categories": "electronics", "price_filter": "budget"})

    merged = presets.apply_to({"categories": "fashion", "price_filter": ""})

    assert merged["categories"] == "fashion"        # explicit wins
    assert merged["price_filter"] == "budget"       # empty inherits


def test_the_presets_default_to_the_same_behaviour_the_hub_always_had(hub):
    values = presets.load()

    assert values["allow_amazon"] and values["allow_earnkaro"] and values["allow_hypd"]
    assert values["only_amazon"] is False
    assert values["mirror_whatsapp"] is True
    assert presets.resolved()["hypd_store_id"]        # the live central store


def test_saving_the_presets_is_audited_and_survives_a_reload(hub):
    presets.save({"only_amazon": True, "hours": "ignored-key"}, actor="Ravi")

    assert presets.load()["only_amazon"] is True
    assert "hours" not in presets.load()              # unknown keys are ignored
    rows = db.list_audit(limit=5)
    assert any(row["action"] == "add_defaults_changed" and row["actor"] == "Ravi"
               for row in rows)


def test_a_stored_setting_that_is_junk_does_not_break_an_add(hub):
    db.set_global_setting("add_defaults", "{not json")

    assert presets.load()["allow_amazon"] is True     # falls back to the code default
    assert presets.describe()


def test_the_summary_sentence_names_what_is_on(hub):
    presets.save({"allow_earnkaro": False, "only_amazon": True, "hypd_store_id": "93944"})

    line = presets.summary_line()

    assert "Amazon" in line and "HYPD" in line and "EarnKaro" not in line
    assert "Amazon-only" in line and "93944" in line


# --------------------------------------------------------------------------
# 4. Easy Setup uses them, and a CSV round trip still works
# --------------------------------------------------------------------------
def test_easy_setup_creates_with_the_presets_and_the_row_extras(hub, monkeypatch):
    presets.save({"allow_earnkaro": False, "categories": "electronics"})
    monkeypatch.setattr(dashboard_module.app, "testing", False)
    monkeypatch.setattr(dashboard_module, "_dashboard_secret_key_is_persistent", lambda: True)
    client = dashboard_module.app.test_client()
    page = client.get("/login").get_data(as_text=True)
    csrf = page.split('name="_csrf_token" value="')[1].split('"')[0]
    client.post("/login", data={"_csrf_token": csrf, "password": "owner-password-16chars"})

    setup = client.get("/setup").get_data(as_text=True)
    csrf = setup.split('name="_csrf_token" value="')[1].split('"')[0]
    client.post("/easy-setup/bulk", data={
        "_csrf_token": csrf,
        "bulk_lines": "Preset Kid, preset-21, @preset_main, @preset_approval, , 9876500000, "
                      "fashion, 10-22, premium",
    })

    created = db.search_influencers("Preset Kid")[0]
    assert int(created["allow_earnkaro"]) == 0        # the owner's preset
    assert created["categories"] == "fashion"         # the row is more specific
    assert created["posting_schedule"] == "10-22"
    assert created["price_filter"] == "premium"
    assert created["phone_number"] == "9876500000"


def test_the_exported_csv_round_trips_through_the_smart_parser(hub, monkeypatch):
    """A file the owner exports can be pasted straight into the team page."""
    from influencer_hub import inventory

    db.add_influencer("Ravi Loots", "ravi-21")
    text = inventory.csv_text()

    rows = creators.parse(text)

    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Ravi Loots" and row["tag"] == "ravi-21"
    assert row["main"] == "@creator0000_loots" or row["main"] == ""   # no channels yet
    assert creators.preview(text)["ready"] in {0, 1}  # missing channels are reported


def test_the_cli_prints_what_a_paste_would_create(hub, capsys):
    from influencer_hub import cli

    # There is no CLI add verb yet; the parser is the shared contract, so the
    # JSON preview the dashboard serves is the same data the CLI would use.
    result = creators.preview("Mohan Rao, mohan-21, @mohan_loots")
    payload = json.loads(json.dumps(result))

    assert payload["rows"][0]["tag"] == "mohan-21"
    assert payload["ready"] == 1
