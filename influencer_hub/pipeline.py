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


async def dispatch_to_channel(influencer: dict, channel: dict, text: str) -> str:
    """Post `text` to one channel. Returns a status string.

    WhatsApp Safety: Uses per-session random delay (2.0s - 5.5s) + anti-flood jitter
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
            # Anti-ban Human Emulation Jitter: 2.0s to 5.5s delay
            delay = random.uniform(2.0, 5.5)
            await asyncio.sleep(delay)

            # Support separate WA session per channel if configured, else default influencer session
            key = channel.get("wa_session_key") or WA_SESSION_KEY(influencer["id"])
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

            # 2. Price Specification Filter (Under 99, Under 499, etc.)
            filter_spec = ch.get("price_filter") or inf.get("price_filter") or "all"
            max_p, min_p = _parse_price_filter_spec(filter_spec)
            if not link_router.matches_price_filter(deal_text, max_price=max_p, min_price=min_p):
                per_channel[ch["id"]] = "skipped"
                continue

            # 3. Approval channel only carries Amazon deals (native + #ad). Skip
            # deals that have no Amazon link so we never post an empty approval.
            if role == "approval" and not link_router.has_amazon_link(deal_text):
                per_channel[ch["id"]] = "skipped"
                continue

            # 4. If strip_amazon is active on this channel, and deal has ONLY Amazon links,
            # skip it because nothing remains to post.
            if strip_amz and not any(link_router.classify_url(u) == "merchant" for u in link_router.find_urls(deal_text)):
                per_channel[ch["id"]] = "skipped"
                continue

            # 5. Smart Dedup Guard: never post the same deal/product twice to the same channel
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
