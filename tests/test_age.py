"""How long each creator has been set up — the wording the owner reads.

The age is a support answer (“when did we set this one up?”) and a sorting key
(“oldest first”), so the wording is pinned here word for word, and the two scales
— the short one used in lists and the long one used on a profile — are kept
distinct.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from influencer_hub import age


NOW = datetime(2026, 10, 13, 15, 30, 0, tzinfo=timezone.utc)


def moment(days: int, *, hours: int = 0) -> str:
    return (NOW - timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# reading a stored timestamp
# --------------------------------------------------------------------------
def test_parse_moment_accepts_the_shapes_the_database_returns():
    assert age.parse_moment("2026-10-13 15:30:00") == NOW
    assert age.parse_moment("2026-10-13T15:30:00") == NOW
    assert age.parse_moment("2026-10-13") == datetime(2026, 10, 13, tzinfo=timezone.utc)
    assert age.parse_moment(NOW) == NOW
    assert age.parse_moment("") is None
    assert age.parse_moment(None) is None
    assert age.parse_moment("not a date") is None


def test_days_since_counts_calendar_days():
    assert age.days_since(moment(0), now=NOW) == 0
    assert age.days_since(moment(1), now=NOW) == 1
    assert age.days_since(moment(0, hours=3), now=NOW) == 0  # a partial day is still today
    assert age.days_since(moment(59), now=NOW) == 59
    assert age.days_since("rubbish", now=NOW) is None


# --------------------------------------------------------------------------
# the short scale: one unit, what a list column can hold
# --------------------------------------------------------------------------
def test_human_age_uses_one_unit_at_a_time():
    cases = {
        0: "today",
        1: "1 day",
        6: "6 days",
        7: "1 week",
        13: "1 week",
        29: "4 weeks",
        31: "1 month",
        59: "1 month",
        200: "6 months",
        344: "11 months",
        365: "1 year",
        456: "1 year 3 months",
        800: "2 years 2 months",
        1500: "4 years 1 month",
    }
    for days, expected in cases.items():
        assert age.human_age(moment(days), now=NOW) == expected, days


def test_twelve_months_never_appears():
    """345 days is a year — “12 months” would look like a mistake in a list."""
    for days in range(345, 366):
        assert "month" not in age.human_age(moment(days), now=NOW), days


def test_human_age_of_an_unreadable_date_is_empty():
    assert age.human_age("") == ""
    assert age.human_age(None) == ""
    assert age.human_age("yesterday-ish") == ""


# --------------------------------------------------------------------------
# the long scale: two units, for a profile line
# --------------------------------------------------------------------------
def test_long_age_spells_out_two_units():
    assert age.long_age(moment(0), now=NOW) == "today"
    assert age.long_age(moment(6), now=NOW) == "6 days"
    assert age.long_age(moment(13), now=NOW) == "1 week 6 days"
    assert age.long_age(moment(45), now=NOW) == "1 month 15 days"
    assert age.long_age(moment(456), now=NOW) == "1 year 3 months"
    assert age.long_age(moment(800), now=NOW) == "2 years 2 months"


def test_long_age_is_never_shorter_than_the_short_one():
    for days in (0, 1, 6, 7, 29, 30, 59, 344, 345, 456, 900, 2000):
        short = age.human_age_from_days(days)
        long = age.long_age_from_days(days)
        assert len(long) >= len(short), days
        assert short.split()[0] == long.split()[0] or short == "today", days


# --------------------------------------------------------------------------
# the one-line label the admin actually reads
# --------------------------------------------------------------------------
def test_setup_label_names_the_date_and_who_added_it():
    label = age.setup_label(moment(456), now=NOW, member="Mohan")
    assert label == "Set up 1 year 3 months ago · 2025-07-14 · by Mohan"
    assert age.setup_label(moment(0), now=NOW) == "Set up today · 2026-10-13"
    assert age.setup_label("", now=NOW, member="Mohan") == "Set up date unknown"
    assert age.setup_label("nonsense", now=NOW) == "Set up date unknown"


def test_setup_label_of_today_does_not_say_ago():
    assert "ago" not in age.setup_label(moment(0), now=NOW)
    assert "ago" in age.setup_label(moment(2), now=NOW)


# --------------------------------------------------------------------------
# the whole set at a glance
# --------------------------------------------------------------------------
def test_summarise_reads_profiles_and_timestamps_alike():
    profiles = [{"name": "a", "created_at": moment(900)},
                {"name": "b", "created_at": moment(0)},
                {"name": "c", "created_at": moment(20)}]
    by_row = age.summarise(profiles, now=NOW)
    by_value = age.summarise([profile["created_at"] for profile in profiles], now=NOW)
    assert by_row == by_value
    assert by_row["count"] == 3
    assert by_row["oldest_days"] == 900
    assert by_row["newest_days"] == 0
    assert by_row["oldest"] == "2 years 6 months"
    assert by_row["newest"] == "today"
    assert by_row["this_month"] == 2


def test_summarise_ignores_unreadable_rows_and_survives_an_empty_hub():
    summary = age.summarise([{"created_at": ""}, {"created_at": "whenever"}], now=NOW)
    assert summary["count"] == 0
    assert summary["oldest"] == ""
    assert age.summarise([], now=NOW)["count"] == 0


def test_phrase_reads_naturally_for_today_and_for_older():
    assert age.phrase("today") == "today"
    assert age.phrase("") == ""
    assert age.phrase("2 months") == "2 months ago"
    assert age.phrase("1 year 3 months") == "1 year 3 months ago"


def test_summarise_counts_only_the_last_thirty_days():
    profiles = [{"created_at": moment(0)}, {"created_at": moment(29)},
                {"created_at": moment(30)}, {"created_at": moment(31)}]
    assert age.summarise(profiles, now=NOW)["this_month"] == 2
