"""Pipeline: shared deal pool -> per-influencer rendered posts.

The deal pool is SHARED. For every active influencer we render the same deal
text but with:
  * Amazon links  -> THEIR amazon associate tag
  * other merchants -> OUR EarnKaro link
and then push to each of that influencer's ready channels (Telegram + WhatsApp
group + WhatsApp Channel).

Dedup is per (influencer, channel, deal_signature) so a deal is never posted
twice to the same place.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Iterable

from . import (
    amazon_shortlinks,
    bitly_client,
    config,
    db,
    earnkaro,
    hypd_shortlinks,
    link_router,
    telegram_ops,
    whatsapp_client,
)

WA_SESSION_KEY = lambda influencer_id: f"inf-{influencer_id}-wa"
ACTIVE_CHANNEL_STATUSES = {"ready", "active"}


async def _earnkaro_map_for(text: str) -> dict[str, str]:
    urls = {u for u, k in link_router.collect_links(text).items() if k == "merchant"}
    if not urls:
        return {}
    # Conversion is best-effort: the original clean merchant URL is retained
    # if the affiliate API is unavailable, so a deal is not lost.
    return await earnkaro.convert_links(urls)


# WhatsApp Session Safety Tracking (Per Session Key):
# Maps session_key -> dict of {"last_post_ts": float, "hour_start_ts": float, "posts_this_hour": int}
WA_PACING_STATE: dict[str, dict] = {}


async def apply_whatsapp_safety_pacing(session_key: str) -> None:
    """Enforces strict, human-like pacing for WhatsApp accounts:
    1. Per-post random human gap: 45 to 65 seconds between consecutive posts on the same session.
    2. Hourly safety break: After an hour of active posting or high volume, takes an extended
       120 to 150 seconds safety cooling break so accounts never trip WhatsApp algorithmic filters.
    """
    now = time.time()
    state = WA_PACING_STATE.setdefault(session_key, {
        "last_post_ts": 0.0,
        "hour_start_ts": now,
        "posts_this_hour": 0,
    })

    # Check 1-hour window reset
    if now - state["hour_start_ts"] >= 3600.0:
        # 1 hour elapsed: take 120 - 150s extended rest before new hourly cycle
        long_break = random.uniform(120.0, 150.0)
        await asyncio.sleep(long_break)
        state["hour_start_ts"] = time.time()
        state["posts_this_hour"] = 0

    # Check gap since last post
    last_post = state["last_post_ts"]
    if last_post > 0:
        elapsed = now - last_post
        target_gap = random.uniform(45.0, 65.0)
        if elapsed < target_gap:
            wait_time = target_gap - elapsed
            await asyncio.sleep(wait_time)

    # Update state
    state["last_post_ts"] = time.time()
    state["posts_this_hour"] += 1


async def dispatch_to_channel(influencer: dict, channel: dict, text: str) -> str:
    """Post `text` to one channel. Returns a status string.

    WhatsApp Safety: Uses per-session random delay (45s - 65s) + hourly 120s - 150s safety break
    so WhatsApp accounts (which belong to the individual influencers) remain 100% safe
    and never get flagged for robotic spamming.

    Telegram Navigation Button: If custom_button_enabled is True on channel, influencer,
    OR globally in Settings, attaches an inline navigation button underneath the post
    directing users to our main channel (e.g. 'Join Shpsy Loots ❤️' -> link).
    """
    platform = channel["platform"]
    try:
        # Resolve button configuration with priority: Channel -> Influencer -> Global
        global_btn_en = db.get_global_setting("nav_button_enabled", "0") == "1"
        global_btn_text = db.get_global_setting("nav_button_text", "Join Shpsy Loots ❤️")
        global_btn_url = db.get_global_setting("nav_button_url", "")

        ch_btn_en = channel.get("custom_button_enabled")
        inf_btn_en = influencer.get("custom_button_enabled")

        # Enabled if specifically enabled on channel or influencer, or if global is enabled
        btn_enabled = bool(ch_btn_en or inf_btn_en or global_btn_en)
        btn_text = (
            channel.get("custom_button_text")
            or influencer.get("custom_button_text")
            or global_btn_text
            or "Join Shpsy Loots ❤️"
        ).strip()
        btn_url = (
            channel.get("custom_button_url")
            or influencer.get("custom_button_url")
            or global_btn_url
            or ""
        ).strip()

        if platform == "telegram":
            if btn_enabled and btn_url:
                await telegram_ops.post_to_channel(channel["identifier"], text,
                                                    button_text=btn_text, button_url=btn_url)
            else:
                await telegram_ops.post_to_channel(channel["identifier"], text)
        elif platform in ("whatsapp", "whatsapp_group", "whatsapp_channel"):
            # Support separate WA session per channel if configured, else default influencer session
            key = channel.get("wa_session_key") or WA_SESSION_KEY(influencer["id"])

            # If custom navigation link is enabled, append clean text footer to WhatsApp posts
            wa_text = text
            if btn_enabled and btn_url and btn_url not in wa_text:
                wa_text = f"{text}\n\n👉 {btn_text}: {btn_url}"

            # Apply 45-65s gap + 120-150s hourly safety pacing
            await apply_whatsapp_safety_pacing(key)

            await whatsapp_client.send_text(key, channel["identifier"], wa_text)
        else:
            return "skipped"
        return "posted"
    except Exception as exc:  # pragma: no cover - network dependent
        return f"failed:{exc}"


def _parse_price_filter_spec(spec: str) -> tuple[float | None, float | None]:
    """Parse a price cap; empty/all/unrestricted/unknown values are permissive."""
    import re
    s = (spec or "").strip().lower()
    if not s or s in {"all", "any", "unrestricted", "no_filter", "*", "default"}:
        return None, None
    m = re.search(r"(?:under|below|max)_?\s*(\d+)", s)
    if m:
        return float(m.group(1)), None
    # Invalid or future filter values must not silently discard a deal.
    return None, None


def _setting_enabled(value, default: bool, unrestricted: bool = True) -> bool:
    """Normalize DB/form values (notably strings like 'all') to a safe toggle."""
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "default"}:
            return default
        if normalized in {"all", "any", "unrestricted", "*", "no_filter"}:
            return unrestricted
        if normalized in {"1", "true", "yes", "on", "enabled", "active"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled", "none", "null"}:
            return False
    return bool(value)


def _is_source_allowed(source_name: str, allowed_spec: str) -> bool:
    """Return True if source_name is allowed by allowed_spec (comma-separated).
    Empty allowed_spec means ALL sources are allowed.
    """
    s = (allowed_spec or "").strip().lower()
    if not s or s in {"all", "any", "unrestricted", "*", "no_filter"}:
        return True
    allowed_list = [x.strip() for x in s.split(",") if x.strip()]
    src = (source_name or "").strip().lower()
    return any(a in src or src in a for a in allowed_list)


async def render_and_dispatch(deal_text: str, influencer_ids: Iterable[int] | None = None,
                              source_channel: str = "") -> dict:
    """Render one deal for every active influencer and dispatch to their channels.

    Returns {influencer_id: {channel_id: status}}.
    """
    sig = link_router.deal_signature(deal_text)
    ek_map = await _earnkaro_map_for(deal_text)

    influencers = db.list_influencers(active_only=True)
    if influencer_ids is not None:
        want = set(influencer_ids)
        influencers = [i for i in influencers if i["id"] in want]

    results: dict[int, dict[int, str]] = {}
    for inf in influencers:
        amazon_tag = inf.get("amazon_tag") or config.AMAZON_ASSOCIATE_TAG
        channels = [c for c in db.list_channels(inf["id"])
                    if str(c.get("status", "")).strip().lower() in ACTIVE_CHANNEL_STATUSES]
        per_channel: dict[int, str] = {}
        for ch in channels:
            role = (ch.get("role") or "broadcast").strip().lower()
            strip_amz = _setting_enabled(ch.get("strip_amazon"), default=False, unrestricted=False)
            effective_amz_tag = (
                ch.get("amazon_override_tag") or amazon_tag or config.AMAZON_ASSOCIATE_TAG
            )

            # 1. Source Specification Filter (an empty/all/unrestricted value means no restriction)
            allowed_sources = ch.get("allowed_sources") or inf.get("allowed_sources") or ""
            if source_channel and not _is_source_allowed(source_channel, allowed_sources):
                per_channel[ch["id"]] = "skipped"
                continue

            # 2. Posting window (empty/all/unrestricted remains 24/7)
            schedule = ch.get("posting_schedule") or inf.get("posting_schedule") or ""
            if not link_router.is_time_in_schedule(schedule):
                per_channel[ch["id"]] = "skipped"
                continue

            # 3. Category and price filters are no-ops for all/unrestricted and
            # intentionally pass deals with missing metadata/prices.
            cat_filter = ch.get("categories") or inf.get("categories") or ""
            if not link_router.matches_category_filter(deal_text, cat_filter):
                per_channel[ch["id"]] = "skipped"
                continue
            filter_spec = ch.get("price_filter") or inf.get("price_filter") or "all"
            max_p, min_p = _parse_price_filter_spec(filter_spec)
            if not link_router.matches_price_filter(deal_text, max_price=max_p, min_price=min_p):
                per_channel[ch["id"]] = "skipped"
                continue

            urls = link_router.find_urls(deal_text)
            kinds = {link_router.classify_url(url) for url in urls}
            has_amz = "amazon" in kinds
            has_merchant = "merchant" in kinds
            has_hypd = "hypd" in kinds
            present_affiliate_kinds = kinds.intersection({"amazon", "merchant", "hypd"})

            # 4. Settings are combined conservatively across the profile and
            # channel. Strings such as "all" and "unrestricted" mean enabled,
            # not truthy special states that accidentally turn on only-Amazon.
            allow_amz = (
                _setting_enabled(ch.get("allow_amazon"), True)
                and _setting_enabled(inf.get("allow_amazon"), True)
            )
            allow_ek = (
                _setting_enabled(ch.get("allow_earnkaro"), True)
                and _setting_enabled(inf.get("allow_earnkaro"), True)
            )
            allow_hypd = (
                _setting_enabled(ch.get("allow_hypd"), True)
                and _setting_enabled(inf.get("allow_hypd"), True)
            )
            only_amz = (
                _setting_enabled(ch.get("only_amazon"), False, unrestricted=False)
                or _setting_enabled(inf.get("only_amazon"), False, unrestricted=False)
            )
            if only_amz:
                allow_amz, allow_ek, allow_hypd = True, False, False
            if strip_amz:
                allow_amz = False
            if role == "approval":
                allow_ek = allow_hypd = False

            allowed_kinds = set()
            if allow_amz:
                allowed_kinds.add("amazon")
            if allow_ek:
                allowed_kinds.add("merchant")
            if allow_hypd:
                allowed_kinds.add("hypd")

            # Approval and only-Amazon channels need a native Amazon link. If
            # every supported merchant in a mixed post is disabled, skip it;
            # otherwise keep the deal and remove only the disabled URLs.
            if (role == "approval" or only_amz) and not has_amz:
                per_channel[ch["id"]] = "skipped"
                continue
            if present_affiliate_kinds and not present_affiliate_kinds.intersection(allowed_kinds):
                per_channel[ch["id"]] = "skipped"
                continue
            render_text = link_router.filter_disallowed_affiliate_links(deal_text, allowed_kinds)

            # 8. Smart Dedup Guard: never post the same deal/product twice to the same channel
            if db.already_posted(inf["id"], ch["id"], sig):
                per_channel[ch["id"]] = "skipped"
                continue

            # Check if this deal needs Bitly URL shortening:
            # ONLY used if influencer has provided their Bitly API key (or channel has its own key)
            effective_bitly_key = (ch.get("bitly_api_key") or inf.get("bitly_api_key") or "").strip()
            # Resolve HYPD Store ID priority: channel -> profile -> central setting -> configured default.
            global_hypd_store = db.get_global_setting("hypd_store_id", config.HYPD_STORE_ID)
            effective_hypd_store = (
                ch.get("hypd_store_id") or inf.get("hypd_store_id") or global_hypd_store or config.HYPD_STORE_ID
            ).strip()
            shortened_map = {}

            if effective_bitly_key and role != "approval":
                # Render base version to identify final URLs that will appear
                base_rendered = link_router.render_for_influencer(
                    render_text, effective_amz_tag, ek_map, role=role, strip_amazon=strip_amz,
                    clean_promos=True, hypd_store_id=effective_hypd_store
                )
                # Amazon and HYPD affiliate URLs never go through generic Bitly.
                # Optional first-party routes for both are applied after rendering.
                final_urls = [
                    url for url in link_router.find_urls(base_rendered)
                    if link_router.classify_url(url) not in {"amazon", "hypd"}
                ]
                should_shorten = (len(final_urls) >= 2) or any(len(url) > 65 for url in final_urls)
                if should_shorten:
                    shortened_map = await bitly_client.shorten_urls(final_urls, token=effective_bitly_key)

            rendered = link_router.render_for_influencer(
                render_text, effective_amz_tag, ek_map, shortened_links=shortened_map,
                role=role, strip_amazon=strip_amz, hypd_store_id=effective_hypd_store
            )
            # Optional first-party redirects require an operator-owned HTTPS
            # hostname. Approval channels remain on native Amazon URLs. HYPD
            # links are shortened only after conversion to the chosen store ID.
            if role != "approval":
                rendered = amazon_shortlinks.shorten_amazon_links(rendered)
                rendered = hypd_shortlinks.shorten_hypd_links(rendered)
            status = await dispatch_to_channel(inf, ch, rendered)
            db.record_post(inf["id"], ch["id"], sig,
                           status="posted" if status == "posted" else "failed",
                           error="" if status == "posted" else status,
                           deal_text=rendered if status == "posted" else "")
            per_channel[ch["id"]] = status
        results[inf["id"]] = per_channel
    return results


async def run_once(deals: Iterable[dict | str], influencer_ids: Iterable[int] | None = None) -> dict:
    # Auto-prune aged logs (older than 14 days) periodically to keep DB ultra-lean and zero-lag
    try:
        if random.random() < 0.05: # ~5% of run cycles
            db.purge_old_posts_and_stats(days_to_keep=14)
    except Exception:
        pass
    """Process a batch of deal items (either dict with {"text", "source"} or plain strings)."""
    all_results: dict[int, dict[int, str]] = {}
    for deal in deals:
        if isinstance(deal, dict):
            d_text = deal.get("text", "")
            d_src = deal.get("source", "")
        else:
            d_text = str(deal)
            d_src = ""
        res = await render_and_dispatch(d_text, influencer_ids, source_channel=d_src)
        for iid, chmap in res.items():
            all_results.setdefault(iid, {}).update(chmap)
    return all_results


async def run_hourly_loot_highlight(influencer_ids: Iterable[int] | None = None) -> dict[int, dict[int, str]]:
    """Analyzes all deals posted in the last 1 hour across active channels,
    finds the highest-rated 'Loot of the Hour' (biggest discount/best price),
    and sends an attractive, high-converting highlight banner post.
    """
    influencers = db.list_influencers(active_only=True)
    if influencer_ids is not None:
        want = set(influencer_ids)
        influencers = [i for i in influencers if i["id"] in want]

    from datetime import datetime
    import zoneinfo
    try:
        ist_now = datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))
        hour_label = ist_now.strftime("%I:%M %p").lstrip("0")
    except Exception:
        hour_label = ""

    results: dict[int, dict[int, str]] = {}
    for inf in influencers:
        channels = [
            c for c in db.list_channels(inf["id"])
            if str(c.get("status", "")).strip().lower() in ACTIVE_CHANNEL_STATUSES
            and c.get("role") != "approval"
        ]
        per_ch: dict[int, str] = {}
        for ch in channels:
            # 1. Check schedule
            sched = ch.get("posting_schedule") or inf.get("posting_schedule") or ""
            if not link_router.is_time_in_schedule(sched):
                continue

            recent_posts = db.get_recent_posted_deals(ch["id"], hours=1)
            # Filter posts with deal text
            candidates = [p for p in recent_posts if p.get("deal_text") and not p.get("deal_text").startswith("👑")]
            if not candidates:
                continue

            # Pick the highest score deal
            best_post = max(candidates, key=lambda p: link_router.calculate_deal_loot_score(p["deal_text"]))
            banner = link_router.format_loot_of_the_hour_post(best_post["deal_text"], hour_label=hour_label)
            # Dedicated signature for hourly highlight to distinguish it from the original raw post
            banner_sig = "highlight:" + link_router.deal_signature(best_post["deal_text"])

            if db.already_posted(inf["id"], ch["id"], banner_sig):
                per_ch[ch["id"]] = "already_highlighted"
                continue

            status = await dispatch_to_channel(inf, ch, banner)
            db.record_post(inf["id"], ch["id"], banner_sig,
                           status="posted" if status == "posted" else "failed",
                           error="" if status == "posted" else status,
                           deal_text=banner if status == "posted" else "")
            per_ch[ch["id"]] = status
        if per_ch:
            results[inf["id"]] = per_ch
    return results


async def close() -> None:
    await telegram_ops.disconnect()
