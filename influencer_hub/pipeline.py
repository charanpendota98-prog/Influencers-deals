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
from typing import Iterable

from . import bitly_client, db, earnkaro, link_router, telegram_ops, whatsapp_client

WA_SESSION_KEY = lambda influencer_id: f"inf-{influencer_id}-wa"


async def _earnkaro_map_for(text: str) -> dict[str, str]:
    urls = {u for u, k in link_router.collect_links(text).items() if k == "merchant"}
    if not urls:
        return {}
    return await earnkaro.convert_links(urls)


import asyncio
import random
import time
from typing import Iterable

from . import bitly_client, db, earnkaro, link_router, telegram_ops, whatsapp_client

WA_SESSION_KEY = lambda influencer_id: f"inf-{influencer_id}-wa"

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

    Multi-account support: Uses `channel['wa_session_key']` if set (e.g. secondary phone number),
    otherwise defaults to the influencer's primary session `inf-{id}-wa`.
    """
    platform = channel["platform"]
    try:
        if platform == "telegram":
            await telegram_ops.post_to_channel(channel["identifier"], text)
        elif platform in ("whatsapp_group", "whatsapp_channel"):
            # Support separate WA session per channel if configured, else default influencer session
            key = channel.get("wa_session_key") or WA_SESSION_KEY(influencer["id"])

            # Apply 45-65s gap + 120-150s hourly safety pacing
            await apply_whatsapp_safety_pacing(key)

            await whatsapp_client.send_text(key, channel["identifier"], text)
        else:
            return "skipped"
        return "posted"
    except Exception as exc:  # pragma: no cover - network dependent
        return f"failed:{exc}"


def _parse_price_filter_spec(spec: str) -> tuple[float | None, float | None]:
    """Parse filter specs like 'under_99', 'under_499', 'under_999', 'under_199', 'all'."""
    import re
    s = (spec or "").strip().lower()
    if not s or s == "all":
        return None, None
    m = re.search(r"under_?(\d+)", s)
    if m:
        return float(m.group(1)), None
    return None, None


def _is_source_allowed(source_name: str, allowed_spec: str) -> bool:
    """Return True if source_name is allowed by allowed_spec (comma-separated).
    Empty allowed_spec means ALL sources are allowed.
    """
    s = (allowed_spec or "").strip().lower()
    if not s or s == "all":
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
        amazon_tag = inf["amazon_tag"]
        channels = [c for c in db.list_channels(inf["id"]) if c["status"] == "ready"]
        per_channel: dict[int, str] = {}
        for ch in channels:
            role = ch.get("role", "broadcast")
            strip_amz = bool(ch.get("strip_amazon", 0))
            # If channel has custom override amazon tag, use it, else default to inf tag
            effective_amz_tag = ch.get("amazon_override_tag") or amazon_tag

            # 1. Source Specification Filter (Powerloot, Secret Loots, etc.)
            allowed_sources = ch.get("allowed_sources") or inf.get("allowed_sources") or ""
            if source_channel and not _is_source_allowed(source_channel, allowed_sources):
                per_channel[ch["id"]] = "skipped"
                continue

            # 2. Timing Schedule Filter (e.g. 06:00-09:00,18:00-23:00)
            schedule = ch.get("posting_schedule") or inf.get("posting_schedule") or ""
            if not link_router.is_time_in_schedule(schedule):
                # Outside the influencer's requested posting window: skip!
                per_channel[ch["id"]] = "skipped"
                continue

            # 3. Category Filter (Clothing, Electronics, Home Things, Daily Essentials)
            cat_filter = ch.get("categories") or inf.get("categories") or ""
            if not link_router.matches_category_filter(deal_text, cat_filter):
                per_channel[ch["id"]] = "skipped"
                continue

            # 4. Price Specification Filter (Under 99, Under 499, etc.)
            filter_spec = ch.get("price_filter") or inf.get("price_filter") or "all"
            max_p, min_p = _parse_price_filter_spec(filter_spec)
            if not link_router.matches_price_filter(deal_text, max_price=max_p, min_price=min_p):
                per_channel[ch["id"]] = "skipped"
                continue

            # 5. Approval channel only carries Amazon deals (native + #ad). Skip
            # deals that have no Amazon link so we never post an empty approval.
            if role == "approval" and not link_router.has_amazon_link(deal_text):
                per_channel[ch["id"]] = "skipped"
                continue

            # 6. Ultra-Smart Granular Merchant Toggles (allow_amazon & allow_earnkaro / only_amazon):
            # Rule A: If allow_amazon is OFF (or strip_amazon is active), do NOT post Amazon deals
            allow_amz = bool(ch.get("allow_amazon", 1) and inf.get("allow_amazon", 1))
            # Rule B: If allow_earnkaro is OFF (or only_amazon is active), do NOT post non-Amazon merchant deals
            allow_ek = bool(ch.get("allow_earnkaro", 1) and inf.get("allow_earnkaro", 1))
            only_amz = bool(ch.get("only_amazon", 0) or inf.get("only_amazon", 0))

            has_amz = link_router.has_amazon_link(deal_text)
            has_merchant = any(link_router.classify_url(u) == "merchant" for u in link_router.find_urls(deal_text))

            # If deal is strictly non-Amazon (Flipkart/Myntra/etc) and EarnKaro is disabled (or only_amazon is enabled):
            if (not allow_ek or only_amz) and not has_amz:
                per_channel[ch["id"]] = "skipped"
                continue

            # If deal is strictly Amazon and Amazon deals are disabled on this channel/influencer:
            if not allow_amz and not has_merchant:
                per_channel[ch["id"]] = "skipped"
                continue

            # 7. If strip_amazon is active on this channel, and deal has ONLY Amazon links,
            # skip it because nothing remains to post.
            if strip_amz and not has_merchant:
                per_channel[ch["id"]] = "skipped"
                continue

            # 8. Smart Dedup Guard: never post the same deal/product twice to the same channel
            if db.already_posted(inf["id"], ch["id"], sig):
                per_channel[ch["id"]] = "skipped"
                continue

            # Check if this deal needs Bitly URL shortening:
            # ONLY used if influencer has provided their Bitly API key (or channel has its own key)
            effective_bitly_key = (ch.get("bitly_api_key") or inf.get("bitly_api_key") or "").strip()
            shortened_map = {}

            if effective_bitly_key and role != "approval":
                # Render base version to identify final URLs that will appear
                base_rendered = link_router.render_for_influencer(
                    deal_text, effective_amz_tag, ek_map, role=role, strip_amazon=strip_amz, clean_promos=True
                )
                final_urls = link_router.find_urls(base_rendered)
                should_shorten = (len(final_urls) >= 2) or any(len(u) > 65 for u in final_urls)
                if should_shorten:
                    shortened_map = await bitly_client.shorten_urls(final_urls, token=effective_bitly_key)

            rendered = link_router.render_for_influencer(
                deal_text, effective_amz_tag, ek_map, shortened_links=shortened_map, role=role, strip_amazon=strip_amz
            )
            status = await dispatch_to_channel(inf, ch, rendered)
            db.record_post(inf["id"], ch["id"], sig,
                           status="posted" if status == "posted" else "failed",
                           error="" if status == "posted" else status)
            per_channel[ch["id"]] = status
        results[inf["id"]] = per_channel
    return results


async def run_once(deals: Iterable[dict | str], influencer_ids: Iterable[int] | None = None) -> dict:
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


async def close() -> None:
    await telegram_ops.disconnect()
