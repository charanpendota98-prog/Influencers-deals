"""How long a creator has been set up — in the words an operator would use.

Every list in the hub can say *when* something happened (``10 Aug 2026``), but the
question the owner actually asks is *"how long has this one been running?"*. This
module turns a stored timestamp into that answer, in two shapes:

* :func:`human_age` — short, for a table cell: ``today``, ``3 days``, ``6 weeks``,
  ``2 months``, ``1 year 3 months``;
* :func:`long_age` — spelled out, for a card: ``1 year 3 months and 4 days``.

Pure functions over strings/``datetime``, no database, stdlib only.
"""
from __future__ import annotations

from datetime import datetime, timezone

def parse_moment(value) -> datetime | None:
    """A stored timestamp (or date) as an aware datetime, or ``None``."""
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace(" ", "T", 1) if (" " in text and "T" not in text) else text
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    moment = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
            else:
                return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def days_since(value, *, now: datetime | None = None) -> int | None:
    """Whole days since the moment (0 = today, negative = in the future)."""
    moment = parse_moment(value)
    if moment is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return int((reference - moment).total_seconds() // 86400)


def human_age(value, *, now: datetime | None = None) -> str:
    """``today`` / ``1 day`` / ``3 weeks`` / ``2 months`` / ``1 year 3 months``."""
    days = days_since(value, now=now)
    if days is None:
        return ""
    return human_age_from_days(days)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def years_and_months(days: int) -> tuple[int, int]:
    """A day count past one year as whole years and months.

    The month count is rounded, and a rounded 12 rolls into the next year, so
    ``729`` days reads "2 years" rather than "1 year 12 months".
    """
    years, rest = divmod(days, 365)
    months = round(rest / 30.44)
    if years == 0:  # 345–364 days: past "11 months", not yet a full year
        return 1, 0
    if months >= 12:
        return years + 1, 0
    return years, months


def human_age_from_days(days: int) -> str:
    """The same wording as :func:`human_age`, from a day count."""
    if days is None:
        return ""
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day"
    if days < 7:
        return f"{days} days"
    if days < 30:
        return _plural(days // 7, "week")
    if days < 345:
        return _plural(max(1, days // 30), "month")
    years, months = years_and_months(days)
    text = _plural(years, "year")
    if months:
        text += " " + _plural(months, "month")
    return text


def long_age(value, *, now: datetime | None = None) -> str:
    """``1 year 3 months`` / ``2 months 4 days`` / ``1 week 6 days``.

    Two units at most: exact about what matters and still short enough to read in
    a sentence ("set up 1 year 3 months ago").
    """
    days = days_since(value, now=now)
    if days is None:
        return ""
    return long_age_from_days(days)


def long_age_from_days(days: int) -> str:
    if days is None:
        return ""
    if days <= 0:
        return "today"
    if days < 7:
        return human_age_from_days(days)
    if days < 30:
        weeks, rest = divmod(days, 7)
        text = _plural(weeks, "week")
        return text + (f" {_plural(rest, 'day')}" if rest else "")
    if days < 345:
        months, rest = divmod(days, 30)
        text = _plural(max(1, months), "month")
        return text + (f" {_plural(rest, 'day')}" if rest else "")
    years, months = years_and_months(days)
    text = _plural(years, "year")
    return text + (f" {_plural(months, 'month')}" if months else "")


def setup_label(created_at, *, now: datetime | None = None, member: str = "") -> str:
    """One line for a list: ``Set up 2 months ago · 10 Aug 2026 · by Mohan``."""
    moment = parse_moment(created_at)
    if moment is None:
        return "Set up date unknown"
    age = human_age(created_at, now=now)
    date = moment.date().isoformat()
    text = f"Set up {age} ago · {date}" if age != "today" else f"Set up today · {date}"
    if member:
        text += f" · by {member}"
    return text


def phrase(human: str) -> str:
    """``today`` stays as it is; every other age reads "… ago" in a sentence."""
    text = str(human or "").strip()
    if not text or text == "today":
        return text
    return f"{text} ago"


def _moment(value):
    """Accept either a stored timestamp or a whole profile row.

    Callers hand this function whatever they already have to hand — a list of
    ``created_at`` strings, or the creator dicts themselves — so the call site
    never has to build a throwaway list.
    """
    if isinstance(value, dict):
        return value.get("created_at")
    return value


def summarise(values, *, now: datetime | None = None) -> dict:
    """How old the whole set is: oldest, newest, and how many joined this month."""
    days = [day for day in (days_since(_moment(value), now=now) for value in values)
            if day is not None]
    if not days:
        return {"count": 0, "oldest_days": None, "newest_days": None,
                "oldest": "", "newest": "", "this_month": 0}
    return {
        "count": len(days),
        "oldest_days": max(days),
        "newest_days": min(days),
        "oldest": human_age_from_days(max(days)),
        "newest": human_age_from_days(min(days)),
        "this_month": sum(1 for day in days if day < 30),
    }


