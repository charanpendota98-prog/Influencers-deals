"""Amazon watcher: what the sources carry, and what each creator would post.

The hub's promise to a creator is simple — *every Amazon link in a deal goes out
signed with that creator's own Associates tag*. This module is the read-only
proof of that promise, in three questions:

* :func:`scan` — what Amazon links does this source post actually contain?
  (product pages, search/store pages, opaque ``amzn.to``/``a.co`` short codes,
  links written without ``http://``), whose tag is on them right now, and what
  will the hub do with each one (retag, carry, resolve-then-mint).
* :func:`plan` / :func:`fleet_plan` — what would one creator's post (or all
  thousand of them) look like, and does each one carry exactly that creator's
  tag? Rendering is pure text work, so a thousand creators take a fraction of a
  second and nothing is written to the database.
* :func:`attribution_report` — the same check on the posts that were *really
  sent*: every Amazon link in the last N hours, grouped into "the creator's own
  tag", "someone else's tag" (a leak worth failing a check over) and "no tag at
  all". It reads ``posts.deal_text``, so it audits the message the subscriber
  received, not the template we meant to send.

Nothing here posts, converts, shortens or writes. It only looks.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

from . import config, db, link_router

PRODUCT, SHORT, SEARCH, STORE, OTHER = "product", "short", "search", "store", "other"

# What the hub will do with each shape of Amazon link, in words an operator can
# repeat to a creator.
_ACTION_TEXT = {
    "retag": "canonicalise to /dp/<ASIN> and sign with this creator's tag",
    "carry": "keep the route and sign it with this creator's tag",
    "resolve": "resolve the short code, then mint /dp/<ASIN> with this creator's tag",
    "keep": "leave it exactly as the source wrote it",
}


def _kind(host: str, parsed) -> str:
    if link_router.is_amazon_short_host(f"https://{host}/"):
        return SHORT
    path = (parsed.path or "").lower()
    if link_router.amazon_marketplace_host(f"https://{host}/") is None:
        return OTHER
    if "/dp/" in path or "/gp/product" in path or "/product/" in path:
        return PRODUCT
    if path.startswith("/s") or "/s?" in path or "field-keywords" in path:
        return SEARCH
    return STORE


def _tag_in(url: str) -> str:
    try:
        return next(
            (value for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True)
             if key.casefold() == "tag" and value),
            "",
        )
    except (TypeError, ValueError):
        return ""


def scan(text: str) -> dict:
    """Every Amazon link in ``text`` with its shape, tag and planned action.

    Scheme-less links (``amazon.in/dp/...``) are promoted first, exactly like the
    pipeline does before it renders, so a link the source wrote without
    ``https://`` is counted as a link — never missed.
    """
    promoted = link_router.promote_scheme_less_links(str(text or ""))
    links: list[dict] = []
    for url in link_router.find_urls(promoted):
        if link_router.classify_url(url) != "amazon":
            continue
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        kind = _kind(host, parsed)
        asin = link_router._amazon_asin(parsed) if kind == PRODUCT else None
        source_tag = _tag_in(url)
        links.append({
            "url": url,
            "host": host,
            "marketplace": link_router.amazon_marketplace_host(url) or host,
            "kind": kind,
            "asin": asin,
            "source_tag": source_tag,
            # The source's own tag is never reused: it belongs to whoever wrote
            # the post, and paying them for a creator's subscriber would be
            # both wrong and visible in the reports.
            "action": "resolve" if kind == SHORT else ("retag" if kind == PRODUCT else "carry"),
            "action_text": _ACTION_TEXT["resolve" if kind == SHORT else ("retag" if kind == PRODUCT else "carry")],
        })
    counts: dict[str, int] = {}
    for link in links:
        counts[link["kind"]] = counts.get(link["kind"], 0) + 1
    return {
        "has_amazon": bool(links),
        "links": links,
        "counts": counts,
        "foreign_source_tags": sorted({
            link["source_tag"] for link in links
            if link["source_tag"] and link["source_tag"] != str(config.AMAZON_ASSOCIATE_TAG or "").strip()
        }),
        "unresolved_shorts": [link["url"] for link in links if link["kind"] == SHORT],
    }


def plan(text: str, *, tag: str = "", role: str = "broadcast") -> dict:
    """What this creator's post would be, link by link, before anything is sent."""
    creator_tag = str(tag or "").strip()
    usable = link_router.amazon_tag_is_usable(creator_tag)
    effective = link_router.effective_amazon_tag(creator_tag)
    promoted = link_router.promote_scheme_less_links(str(text or ""))
    rendered = link_router.render_for_influencer(promoted, effective, {}, role=role)
    after = scan(rendered)

    issues: list[str] = []
    for link in after["links"]:
        got = _tag_in(link["url"])
        if link["kind"] == SHORT:
            issues.append(
                f"{link['url']} is a short code: the hub resolves it before posting and "
                "mints the tagged product link, so attribution is verified at send time."
            )
        elif not got:
            issues.append(f"{link['url']} carries no Associate tag — check the source link shape.")
        elif got != effective:
            issues.append(f"{link['url']} carries '{got}' instead of '{effective}'.")
    if not usable:
        issues.append(
            f"'{creator_tag or '(blank)'}' is not a usable Associates tag, so the hub falls "
            f"back to {effective} for this creator (and the dashboard flags them)."
        )
    return {
        "tag": creator_tag,
        "effective_tag": effective,
        "usable_tag": usable,
        "role": role,
        "rendered": rendered,
        "links": after["links"],
        "counts": after["counts"],
        "issues": issues,
        "ok": bool(after["links"]) and not [i for i in issues if "instead of" in i or "no Associate tag" in i],
    }


def fleet_plan(text: str, influencers: list[dict], *, role: str = "broadcast",
               limit: int = 1000) -> dict:
    """Render one deal for every creator and check whose tag each post carries.

    Pure text work on purpose: a thousand creators are checked without a single
    database write, which is what makes it safe to run as a preflight before a
    big paste is switched on.
    """
    import time

    started = time.perf_counter()
    creators = list(influencers or [])[: max(1, int(limit or 1000))]
    details: list[dict] = []
    own = fallback = missing = 0
    for inf in creators:
        tag = str(inf.get("amazon_tag") or "").strip()
        effective = link_router.effective_amazon_tag(tag)
        rendered = link_router.render_for_influencer(
            link_router.promote_scheme_less_links(str(text or "")), effective, {}, role=role,
        )
        links = scan(rendered)["links"]
        tags = [_tag_in(link["url"]) for link in links]
        bad = [value for value in tags if value and value != effective]
        if not links:
            missing += 1
        elif bad:
            fallback += 1
        elif effective != tag:
            fallback += 1
        else:
            own += 1
        if len(details) < 12 and (bad or not links or effective != tag):
            details.append({
                "creator_id": inf.get("id"),
                "creator": inf.get("name"),
                "tag": tag,
                "effective_tag": effective,
                "links": len(links),
                "tags_found": tags,
            })
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "creators": len(creators),
        "own_tag": own,
        "fallback_or_missing": fallback + missing,
        "missing_amazon_link": missing,
        "details": details,
        "ms": elapsed_ms,
        "all_own_tags": bool(creators) and own == len(creators),
    }


def attribution_report(*, hours: int = 24, limit: int = 500,
                       influencers: list[dict] | None = None) -> dict:
    """Audit the posts we really sent: whose tag is on each Amazon link?

    ``own`` counts links carrying the tag of the creator that channel belongs to,
    ``foreign`` counts links carrying any other tag (nobody else may be paid for
    a creator's subscribers), and ``untagged`` counts Amazon links with no tag at
    all — informational, because a post may legitimately mention a product page.
    """
    hours = max(1, min(24 * 30, int(hours or 24)))
    # limit=0 (the CLI default) means "use the default cap", never "read nothing".
    cap = max(1, min(5000, int(limit or 500)))
    rows = db.recent_posted_texts(days=max(1, (hours + 23) // 24), limit=cap)
    creators = influencers if influencers is not None else db.list_influencers()
    tags_by_id = {inf.get("id"): str(inf.get("amazon_tag") or "").strip() for inf in creators}
    names_by_id = {inf.get("id"): inf.get("name") for inf in creators}

    posts_with_amazon = 0
    own = foreign = untagged = 0
    shorts = 0
    examples: list[dict] = []
    problems: list[dict] = []
    for row in rows:
        found = scan(row.get("deal_text") or "")
        if not found["links"]:
            continue
        posts_with_amazon += 1
        expected = link_router.effective_amazon_tag(tags_by_id.get(row.get("influencer_id")))
        for link in found["links"]:
            got = _tag_in(link["url"])
            if link["kind"] == SHORT:
                shorts += 1
                continue
            if not got:
                untagged += 1
                continue
            if got == expected:
                own += 1
            else:
                foreign += 1
                if len(problems) < 10:
                    problems.append({
                        "creator_id": row.get("influencer_id"),
                        "creator": names_by_id.get(row.get("influencer_id")),
                        "expected_tag": expected,
                        "found_tag": got,
                        "url": link["url"],
                    })
        if len(examples) < 8:
            examples.append({
                "creator": names_by_id.get(row.get("influencer_id")),
                "expected_tag": expected,
                "links": [link["url"] for link in found["links"]],
            })
    return {
        "hours": hours,
        "posts_scanned": len(rows),
        "posts_with_amazon": posts_with_amazon,
        "links": own + foreign + untagged + shorts,
        "own_tag": own,
        "foreign_tag": foreign,
        "untagged": untagged,
        "short_links": shorts,
        "problems": problems,
        "examples": examples,
        "ok": foreign == 0,
    }


def render_report(data: dict) -> str:
    """One plain line for the ops card / CLI (never a secret)."""
    if not data.get("posts_with_amazon"):
        return (f"Amazon: no Amazon links in the last {data.get('hours', 24)}h "
                f"({data.get('posts_scanned', 0)} post(s) scanned).")
    line = (f"Amazon: {data['own_tag']} link(s) with the creator's own tag in "
            f"{data['posts_with_amazon']} post(s)")
    if data.get("foreign_tag"):
        line += f" · ⛔ {data['foreign_tag']} with someone else's tag"
    if data.get("untagged"):
        line += f" · {data['untagged']} untagged"
    if data.get("short_links"):
        line += f" · {data['short_links']} short code(s) resolved at send time"
    return line
