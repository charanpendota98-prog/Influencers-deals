"""Proof that an Amazon link posted to a creator's channel pays *that* creator.

The owner's rule, in his words: *“akkada tag lo em tag isthe, valla channel aa
channels loney valla tag tho links cheyali — vallaki commission miss avvakunda
carefulga cheyu.”* Whatever tag a creator is given, the Amazon links on **their**
channels must carry it, and nobody may lose commission because a link was signed
with somebody else's (or the hub's) id.

This module is the one place that answers three questions:

* **Whose tag is this?** :func:`signing` — the channel override, the creator's own
  Associate id, or the hub-wide fallback. Only the first two are the creator's.
* **Is the link really theirs?** :func:`verify` — renders/reads a post and checks
  the ``tag=`` on every Amazon URL, so a copy-pasted link, an ``amzn.to`` wrapper
  or a stale channel override is caught before the post goes out.
* **Does the whole fleet hold?** :func:`fleet` — one batched pass over the hub
  (no network, no per-row query), so “1000+ influencers, every one on their own
  tag” is a number the owner can read, and :data:`HOLD_REASON` is the single
  reason string the pipeline uses when it refuses to post.

Pure functions over dicts — the pipeline, the dashboard, the CLI and the
self-check all read the same answer.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse

from . import accounts, config, db, link_router

#: The Amazon deal the pipeline uses to prove a creator's own signing link.
SAMPLE_ASIN = "B0D9P2M1PB"
SAMPLE_URL = f"https://www.amazon.in/dp/{SAMPLE_ASIN}?tag=someone-else-21"

#: Why a post was held instead of signed with the wrong tag (pipeline status +
#: ``db.defer_deal`` reason). The dashboard and the CLI print the same string.
HOLD_REASON = "no_own_amazon_tag"

_TAG_PARAM_RE = re.compile(r"(?:^|[?&])tag=([^&\s]+)", re.I)


# --------------------------------------------------------------------------
# 1. Whose tag is it?
# --------------------------------------------------------------------------
def signing(profile: dict | None = None, channel: dict | None = None) -> dict:
    """The tag that will sign this channel's Amazon links, and whose it is.

    ``source`` is ``channel`` (an id typed for this creator's channel),
    ``creator`` (their own Associate id) or ``fallback`` (the hub-wide tag —
    *not* theirs, which is exactly the case that loses them commission).
    """
    profile = profile or {}
    channel = channel or {}
    tag = accounts.creator_amazon_tag(profile, channel)
    source = accounts.amazon_tag_source(profile, channel)
    fallback_tag = link_router.effective_amazon_tag("")
    # The database fills a blank tag with the hub-wide default so a deal is never
    # dropped. That makes the *value* look like the creator's own while the money
    # would land on the shared id — the exact mix-up the owner asked to prevent —
    # so a tag equal to the default (or a channel override equal to it) is not
    # this creator's own.
    shared_default = bool(tag) and bool(fallback_tag) and tag == fallback_tag
    own = bool(tag) and accounts.amazon_tag_is_creators_own(profile, channel) \
        and not shared_default
    if own:
        reason = ""
    else:
        reason = HOLD_REASON
    return {
        "tag": tag,
        "source": source,
        "own": own,
        "shared_default": shared_default,
        "usable": link_router.amazon_tag_is_usable(tag),
        "fallback_tag": fallback_tag,
        "reason": reason,
        "creator": str(profile.get("name") or ""),
        "creator_id": int(profile.get("id") or 0),
        "channel_id": int(channel.get("id") or 0),
    }


def stored_tag(profile: dict | None = None) -> str:
    """The tag value saved on the creator row (may be the hub-wide default)."""
    return str((profile or {}).get("amazon_tag") or "").strip()


def own_tag(profile: dict | None = None) -> str:
    """The creator's **own** Associate id, or ``""`` when there is none.

    The database fills a blank tag with the hub-wide default, so a stored value
    that equals that default is not an own tag — it is the shared id the owner
    asked us never to pay a creator's commission to.
    """
    value = stored_tag(profile)
    if not link_router.amazon_tag_is_usable(value):
        return ""
    if value == link_router.effective_amazon_tag(""):
        return ""
    return value


def tagged_link(profile: dict | None = None, channel: dict | None = None,
                url: str = SAMPLE_URL) -> str:
    """The exact canonical Amazon link this creator's channel will publish."""
    info = signing(profile, channel)
    if not info["tag"]:
        return ""
    return link_router.render_for_influencer(url, info["tag"])


def amazon_tags_in(text: str) -> dict[str, str]:
    """Every Amazon URL in ``text`` → the ``tag`` it carries (``""`` when none)."""
    found: dict[str, str] = {}
    for url in link_router.find_urls(text or ""):
        kind = link_router.classify_url(url)
        if kind != "amazon" and not link_router.is_amazon_short_host(url):
            continue
        match = _TAG_PARAM_RE.search(url)
        found[url] = match.group(1) if match else ""
    return found


# --------------------------------------------------------------------------
# 2. Is the rendered post really signed by its creator?
# --------------------------------------------------------------------------
def verify(rendered: str, profile: dict | None = None, channel: dict | None = None,
           *, expect_own: bool = True) -> dict:
    """Does every Amazon link in ``rendered`` carry *this creator's* tag?

    ``expect_own=False`` accepts the hub-wide fallback (used by the CLI when the
    owner deliberately runs one shared account), but a link carrying **another
    creator's** tag is always wrong.

    A post with no Amazon links verifies: nothing about it can mispay Amazon.
    """
    info = signing(profile, channel)
    tags = amazon_tags_in(rendered)
    expected = info["tag"]
    issues: list[str] = []
    wrong: list[dict] = []

    for url, tag in tags.items():
        if not tag:
            wrong.append({"url": url, "tag": "", "why": "untagged"})
            issues.append(f"Amazon link without a tag: {url}")
            continue
        if expected and tag != expected:
            wrong.append({"url": url, "tag": tag, "why": "different_tag"})
            issues.append(
                f"Amazon link signed with {tag} but this channel posts as {expected}: {url}"
            )
    if tags and not expected:
        issues.append("No Amazon tag is configured for this creator or channel.")
    if expect_own and not info["own"] and tags:
        issues.append(
            f"{info['creator'] or 'This creator'} has no own Amazon tag — their Amazon "
            f"links would earn {info['fallback_tag'] or 'the hub tag'} instead of them."
        )

    return {
        "ok": not issues,
        "issues": issues,
        "wrong": wrong,
        "tag": expected,
        "own": info["own"],
        "source": info["source"],
        "amazon_links": len(tags),
        "reason": "" if not issues else (wrong[0]["why"] if wrong else HOLD_REASON),
    }


# --------------------------------------------------------------------------
# 3. Does the whole fleet hold — at 1000+ creators?
# --------------------------------------------------------------------------
def fleet(*, influencers: list[dict] | None = None,
          channels_by_creator: dict[int, list[dict]] | None = None,
          active_only: bool = True, sample: bool = True) -> dict:
    """Every active channel of every creator, checked in one pass.

    Returns the counts the owner reads (own / fallback / sample links verified),
    plus one finding per creator whose Amazon posts would pay the wrong account.
    Reads the database in two queries when the caller does not pass the rows, so
    a 1000-creator hub costs milliseconds rather than 1000 round-trips.
    """
    rows = influencers if influencers is not None else db.list_influencers(
        active_only=active_only)
    if active_only:
        rows = [row for row in rows if row.get("active")]
    if channels_by_creator is None:
        channels_by_creator = {}
        for channel in db.list_channels():
            channels_by_creator.setdefault(int(channel.get("influencer_id") or 0), []).append(channel)

    checked = 0
    own = 0
    fallback = 0
    broken_links: list[dict] = []
    blocked: list[dict] = []

    for profile in rows:
        inf_id = int(profile.get("id") or 0)
        channels = [ch for ch in channels_by_creator.get(inf_id, [])
                    if str(ch.get("status") or "").strip().lower() in _ACTIVE_STATUSES]
        if not channels:
            continue
        blocked_here: list[dict] = []
        for channel in channels:
            checked += 1
            info = signing(profile, channel)
            if info["own"]:
                own += 1
            else:
                fallback += 1
                blocked_here.append({
                    "channel_id": info["channel_id"],
                    "platform": str(channel.get("platform") or ""),
                    "identifier": str(channel.get("identifier") or ""),
                    "tag": info["tag"],
                    "source": info["source"],
                    "shared_default": info["shared_default"],
                })
            if sample and info["tag"]:
                rendered = link_router.render_for_influencer(SAMPLE_URL, info["tag"])
                proof = verify(rendered, profile, channel, expect_own=False)
                if not proof["ok"]:
                    broken_links.append({
                        "creator_id": inf_id,
                        "channel_id": info["channel_id"],
                        "issues": proof["issues"][:3],
                    })
        if blocked_here:
            blocked.append({
                "creator_id": inf_id,
                "name": str(profile.get("name") or f"Creator #{inf_id}"),
                "fallback_tag": link_router.effective_amazon_tag(""),
                "stored_tag": str(profile.get("amazon_tag") or ""),
                "shared_default": all(row["shared_default"] for row in blocked_here),
                "channels": blocked_here,
            })

    return {
        "creators": len(rows),
        "channels": checked,
        "own": own,
        "fallback": fallback,
        "broken_links": broken_links,
        "blocked": blocked,
        "ok": not blocked and not broken_links,
        "reason": HOLD_REASON if blocked else ("broken_tag" if broken_links else ""),
        "summary": summary_line(checked, own, fallback, blocked, broken_links),
        "require_own_tag": bool(getattr(config, "REQUIRE_OWN_AMAZON_TAG", True)),
    }


def summary_line(checked: int, own: int, fallback: int,
                 blocked: list[dict] | None = None,
                 broken: list[dict] | None = None) -> str:
    """One sentence for the dashboard, the CLI and the digest."""
    if not checked:
        return "No active channels yet — nothing can be posted with the wrong tag."
    if not fallback and not broken:
        return (f"✅ All {own} active channel(s) sign Amazon links with their own "
                "Associate tag.")
    parts = []
    if fallback:
        shared = sum(1 for row in (blocked or []) if row.get("shared_default"))
        what = ("still on the hub's shared tag" if shared == len(blocked or [])
                else "have no own tag")
        parts.append(f"⛔ {fallback} of {checked} active channel(s) {what}")
    if blocked:
        names = ", ".join(row["name"] for row in blocked[:4])
        more = f" and {len(blocked) - 4} more" if len(blocked) > 4 else ""
        parts.append(f"({names}{more})")
    if broken:
        parts.append(f"⚠ {len(broken)} rendered link(s) failed a second check")
    return " ".join(parts)


def describe_fix(blocked: list[dict]) -> str:
    """What the owner has to do, in one line."""
    if not blocked:
        return ""
    first = blocked[0]
    return (f"Paste each creator's own Associate tag (for example on "
            f"{first['name']}'s page) — until then their Amazon deals are held, "
            f"never signed with {first['fallback_tag'] or 'the hub tag'}.")


# --------------------------------------------------------------------------
# Keeping the fleet honest at post time
# --------------------------------------------------------------------------
def rendered_for(profile: dict, channel: dict, deal_text: str) -> str:
    """Render only the Amazon tagging part of ``deal_text`` (offline, no network).

    Used by the tests and by the CLI to prove the signing link without touching a
    source, an API or a network.
    """
    return link_router.render_for_influencer(deal_text, signing(profile, channel)["tag"])


def holds(profile: dict, channel: dict, deal_text: str, *, strip_amazon: bool = False) -> dict:
    """Would the pipeline have to hold this deal for this channel?

    Holding means: the post carries Amazon links, the channel would sign them with
    a tag that is not the creator's own, and the owner has asked for own-tag-only
    posting (``config.REQUIRE_OWN_AMAZON_TAG``, on by default).

    A channel that strips Amazon links, or a deal without any Amazon link, is
    never held — EarnKaro/HYPD money goes to the hub's central accounts by design.
    """
    if strip_amazon:
        return {"hold": False, "reason": "", "signing": signing(profile, channel)}
    if not getattr(config, "REQUIRE_OWN_AMAZON_TAG", True):
        return {"hold": False, "reason": "", "signing": signing(profile, channel)}
    info = signing(profile, channel)
    if info["own"]:
        return {"hold": False, "reason": "", "signing": info}
    if not deal_has_amazon(deal_text):
        return {"hold": False, "reason": "", "signing": info}
    return {"hold": True, "reason": HOLD_REASON, "signing": info}


def deal_has_amazon(text: str) -> bool:
    """Does this deal post carry an Amazon link (long, short or bare)?"""
    body = link_router.promote_scheme_less_links(text or "")
    if link_router.text_has_amazon_short(body):
        return True
    for url in link_router.find_urls(body):
        if link_router.classify_url(url) == "amazon":
            return True
    return False


def url_summary(url: str) -> str:
    """``www.amazon.in/dp/B0…?tag=ravi-21`` — a link an operator can eyeball."""
    try:
        parsed = urlparse(url)
    except Exception:  # pragma: no cover - defensive
        return url
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    tag = dict(parse_qsl(parsed.query)).get("tag", "")
    return f"{host}{path}" + (f"?tag={tag}" if tag else "")


# A channel that really posts. Kept here rather than imported from ``pipeline``
# because the pipeline calls this module (an import back would be a cycle);
# ``tests/test_tag_proof.py`` fails if the two lists ever drift apart.
ACTIVE_CHANNEL_STATUSES = frozenset({"ready", "active"})
_ACTIVE_STATUSES = ACTIVE_CHANNEL_STATUSES
