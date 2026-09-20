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
from typing import Iterable

from . import db, earnkaro, link_router, telegram_ops, whatsapp_client

WA_SESSION_KEY = lambda influencer_id: f"inf-{influencer_id}-wa"


async def _earnkaro_map_for(text: str) -> dict[str, str]:
    urls = {u for u, k in link_router.collect_links(text).items() if k == "merchant"}
    if not urls:
        return {}
    return await earnkaro.convert_links(urls)


async def dispatch_to_channel(influencer: dict, channel: dict, text: str) -> str:
    """Post `text` to one channel. Returns a status string."""
    platform = channel["platform"]
    try:
        if platform == "telegram":
            await telegram_ops.post_to_channel(channel["identifier"], text)
        elif platform in ("whatsapp_group", "whatsapp_channel"):
            key = WA_SESSION_KEY(influencer["id"])
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


async def render_and_dispatch(deal_text: str, influencer_ids: Iterable[int] | None = None) -> dict:
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

            # Price filter hierarchy: channel price_filter overrides influencer price_filter
            filter_spec = ch.get("price_filter") or inf.get("price_filter") or "all"
            max_p, min_p = _parse_price_filter_spec(filter_spec)

            # 1. Price Specification Filter (Under 99, Under 499, etc.)
            if not link_router.matches_price_filter(deal_text, max_price=max_p, min_price=min_p):
                per_channel[ch["id"]] = "skipped"
                continue

            # 2. Approval channel only carries Amazon deals (native + #ad). Skip
            # deals that have no Amazon link so we never post an empty approval.
            if role == "approval" and not link_router.has_amazon_link(deal_text):
                per_channel[ch["id"]] = "skipped"
                continue

            # 3. If strip_amazon is active on this channel, and deal has ONLY Amazon links,
            # skip it because nothing remains to post.
            if strip_amz and not any(link_router.classify_url(u) == "merchant" for u in link_router.find_urls(deal_text)):
                per_channel[ch["id"]] = "skipped"
                continue

            if db.already_posted(inf["id"], ch["id"], sig):
                per_channel[ch["id"]] = "skipped"
                continue
            rendered = link_router.render_for_influencer(
                deal_text, effective_amz_tag, ek_map, role=role, strip_amazon=strip_amz
            )
            status = await dispatch_to_channel(inf, ch, rendered)
            db.record_post(inf["id"], ch["id"], sig,
                           status="posted" if status == "posted" else "failed",
                           error="" if status == "posted" else status)
            per_channel[ch["id"]] = status
        results[inf["id"]] = per_channel
    return results


async def run_once(deals: Iterable[str], influencer_ids: Iterable[int] | None = None) -> dict:
    """Process a batch of deal texts."""
    all_results: dict[int, dict[int, str]] = {}
    for deal in deals:
        res = await render_and_dispatch(deal, influencer_ids)
        for iid, chmap in res.items():
            all_results.setdefault(iid, {}).update(chmap)
    return all_results


async def close() -> None:
    await telegram_ops.disconnect()
