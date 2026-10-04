"""Pipeline: shared deal pool -> per-influencer rendered posts.

The deal pool is shared. For each eligible influencer/channel, the pipeline
renders Amazon links with the selected Associate tag and independently attempts
enabled EarnKaro/HYPD routes. The explicit ``only_amazon`` setting suppresses
other affiliate categories. It dispatches to ready/active destinations
supported by current integrations. Routing settings do not guarantee external
conversion or commission outcomes.

Dedup is per (influencer, channel, deal_signature) so a deal is not reposted
twice to the same place while its dedup record is retained.
"""
from __future__ import annotations

import asyncio
import random
from typing import Iterable

from . import (
    amazon_shortlinks,
    bitly_client,
    config,
    db,
    earnkaro,
    hypd_shortlinks,
    lehlah_shortlinks,
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


async def apply_whatsapp_safety_pacing(session_key: str) -> None:
    """Reserve a durable, conservative WhatsApp send slot for this session.

    SQLite serializes the slot across worker/dashboard processes and preserves
    it across restarts. Consecutive reservations are separated by a random
    45–65 second gap, with a 120–150 second rest at the hourly window boundary.
    These delays reduce send frequency; they cannot guarantee account safety
    or prevent platform enforcement. Failed sends also consume their reserved
    slot, intentionally favoring conservative pacing over throughput.
    """
    delay = db.reserve_whatsapp_send(
        session_key,
        gap_seconds=random.uniform(45.0, 65.0),
        rest_seconds=random.uniform(120.0, 150.0),
    )
    if delay > 0:
        await asyncio.sleep(delay)


async def dispatch_poll(channel: dict, question: str, options: list[str],
                        allow_multiple: bool = False) -> dict:
    """Send a native poll to a Telegram destination or WhatsApp group.

    WhatsApp Channels/newsletters are intentionally unsupported until native
    poll delivery there is verified in the pinned Baileys integration.
    """
    platform = str(channel.get("poll_platform") or channel.get("platform") or "").lower()
    identifier = str(channel.get("identifier") or "").strip()
    if platform == "telegram":
        await telegram_ops.post_poll_to_channel(
            identifier, question, options, allow_multiple=allow_multiple
        )
        return {"ok": True}
    if platform in {"whatsapp", "whatsapp_group"} and identifier.lower().endswith("@g.us"):
        key = channel.get("wa_session_key") or WA_SESSION_KEY(channel["influencer_id"])
        await apply_whatsapp_safety_pacing(str(key))
        return await whatsapp_client.send_poll(
            str(key), identifier, question, options, allow_multiple=allow_multiple
        )
    raise ValueError("Polls are supported only in Telegram channels and WhatsApp groups")


async def dispatch_to_channel(influencer: dict, channel: dict, text: str) -> str:
    """Post `text` to one channel. Returns a status string.

    WhatsApp sends use conservative per-session pacing, but pacing cannot
    guarantee account safety or prevent platform enforcement.

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
    # Lazily convert only when at least one destination explicitly allows
    # EarnKaro. The same deal mapping is then reused across those destinations.
    ek_map_cache: dict[str, str] | None = None

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
            present_affiliate_kinds = kinds.intersection(
                {"amazon", "merchant", "hypd", "meesho", "lehlah"}
            )

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
            # Network switches are independent: Amazon can post alongside
            # selected EarnKaro/HYPD links. only_amazon is the explicit
            # exclusive-mode override.
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
                # HYPD owns the Meesho route. Existing HYPD affiliate links
                # can be retagged; raw Meesho URLs stay intact until an official
                # HYPD generator contract is configured.
                allowed_kinds.update({"hypd", "meesho"})
            # LehLah-tagged Meesho links carry an existing publisher attribution.
            # Keep them (unless the channel is explicitly Amazon-only/approval)
            # and never route them through EarnKaro or strip their query params.
            if not only_amz and role != "approval":
                allowed_kinds.add("lehlah")

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
            # Fix amzn.to/amzn.in short links that encode old tags: if the deal also has a long
            # Amazon link with ASIN, replace the short with the canonical OUR link (heuristic, no network)
            # This ensures https://amzn.to/4dnF9lU?tag=mama086-21 (old code) becomes
            # https://www.amazon.in/dp/B0D9P2M1PB?th=1&tag=mama086-21 and then gets shortened correctly
            if "amzn.to" in render_text or "amzn.in" in render_text:
                render_text = link_router.expand_amazon_shorts_in_text(render_text, effective_amz_tag)

            # 8. Smart Dedup Guard: never post the same deal/product twice to the same channel
            if db.already_posted(inf["id"], ch["id"], sig):
                per_channel[ch["id"]] = "skipped"
                continue

            if allow_ek:
                if ek_map_cache is None:
                    ek_map_cache = await _earnkaro_map_for(render_text)
                channel_ek_map = ek_map_cache
            else:
                channel_ek_map = {}

            # Bitly token priority: channel -> influencer -> global key explicitly
            # allowlisted for this owned influencer profile. No other influencer
            # can consume the operator's global token by default.
            influencer_key = str(inf.get("bitly_api_key") or "").strip()
            channel_key = str(ch.get("bitly_api_key") or "").strip()
            global_key = (
                config.BITLY_API_KEY
                if str(inf.get("id") or "") in config.BITLY_GLOBAL_INFLUENCER_IDS
                else ""
            )
            effective_bitly_key = channel_key or influencer_key or global_key
            # Resolve HYPD Store ID priority: channel -> profile -> central setting -> configured default.
            global_hypd_store = db.get_global_setting("hypd_store_id", config.HYPD_STORE_ID)
            effective_hypd_store = (
                ch.get("hypd_store_id") or inf.get("hypd_store_id") or global_hypd_store or config.HYPD_STORE_ID
            ).strip()
            shortened_map = {}

            if effective_bitly_key and role != "approval":
                # Check if ADVANCED ONLY-OUR-LINKS mode is enabled (user requested "ONLY MANA LINK KI")
                # When enabled, generic merchant Bitly is DISABLED; only OUR Amazon/HYPD via advanced shortener will be shortened
                try:
                    _db_only_our = str(db.get_global_setting("advanced_only_our_links", "")).strip().lower()
                    if _db_only_our:
                        _only_our_enabled = _db_only_our in {"1", "true", "yes", "on"}
                    else:
                        _only_our_enabled = bool(config.ADVANCED_ONLY_OUR_LINKS)
                except Exception:
                    _only_our_enabled = bool(config.ADVANCED_ONLY_OUR_LINKS)

                if _only_our_enabled:
                    # ADVANCED MODE: Skip generic merchant Bitly, ONLY OUR links via advanced shortener later
                    shortened_map = {}
                else:
                    # Classic mode: Bitly for long merchant links (Flipkart etc. not yet converted)
                    # Render base version to identify final URLs that will appear
                    base_rendered = link_router.render_for_influencer(
                        render_text, effective_amz_tag, channel_ek_map, role=role, strip_amazon=strip_amz,
                        clean_promos=True, hypd_store_id=effective_hypd_store
                    )
                    # Amazon, HYPD, and existing LehLah affiliate URLs never go
                    # through generic Bitly. Their first-party routes are applied later.
                    # EarnKaro shorteners (ekaro.in, fktr.in, etc.) are already short and must stay as-is.
                    from urllib.parse import urlparse as _bt_urlparse
                    _ek_shortener_hosts = {"fktr.in", "ekaro.in", "ekaro.app", "clnk.in", "clnk.app", "myntr.it"}
                    def _is_earnkaro_short(url: str) -> bool:
                        try:
                            h = (_bt_urlparse(url).hostname or "").lower()
                            return h in _ek_shortener_hosts or (h.startswith("www.") and h[4:] in _ek_shortener_hosts)
                        except Exception:
                            return False
                    final_urls = [
                        url for url in link_router.find_urls(base_rendered)
                        if link_router.classify_url(url) not in {"amazon", "hypd", "meesho", "lehlah"}
                        and not _is_earnkaro_short(url)
                    ]
                    # PERFECT CHECK: Ensure Amazon product links are never Bitly-shortened via generic path
                    # Only shorten if multiple links or excessively long URL (>65 chars)
                    should_shorten = (len(final_urls) >= 2) or any(len(url) > 65 for url in final_urls)
                    if should_shorten:
                        shortened_map = await bitly_client.shorten_urls(final_urls, token=effective_bitly_key)

            rendered = link_router.render_for_influencer(
                render_text, effective_amz_tag, channel_ek_map, shortened_links=shortened_map,
                role=role, strip_amazon=strip_amz, hypd_store_id=effective_hypd_store
            )
            # ADVANCED SHORTENER: ONLY OUR affiliate links are shortened
            # - HYPD links with OUR store ID (93944) -> first-party /m/<code> or Bitly fallback
            # - Amazon links with OUR tag (e.g. mytag-21) -> first-party /amazon/<code>?tag= or Bitly fallback
            # Generic merchant links (Flipkart etc.) are already handled via EarnKaro's ekaro.in, no extra Bitly needed
            # This advanced system ensures ONLY MANA LINK KI MATHARME short avtundi, vere vallavi kaadu
            if role != "approval":
                from . import advanced_shortener
                rendered = await advanced_shortener.shorten_our_links_advanced(
                    rendered, effective_amz_tag, effective_hypd_store, bitly_token=effective_bitly_key
                )
                if config.LEHLAH_SHORTLINKS_ENABLED:
                    rendered = lehlah_shortlinks.shorten_lehlah_links(rendered)
                # Deduplicate identical short URLs (e.g., amzn.to + long link both became same bit.ly)
                rendered = link_router.deduplicate_urls_in_text(rendered)

                # === COMMISSION GUARD: perfect verification that every shortened/posted link is OUR affiliate ===
                # User: "SHORTEN LINK CHETHE PEREFCTGA CORRECT GA MANA AFFILAT ELINK RAVALI OOKAYNAA"
                # We audit FINAL rendered text before dispatch. Any non-OUR leak is logged and sanitized.
                try:
                    from . import commission_guard
                    expected_pubid = (db.get_global_setting("earnkaro_publisher_id") or config.EARNKARO_PUBLISHER_ID or "").strip()
                    audit = commission_guard.audit_rendered_text(
                        rendered, effective_amz_tag, effective_hypd_store, expected_pubid, bitly_map=shortened_map, allowed_kinds=allowed_kinds
                    )
                    if not audit["ok"]:
                        import logging
                        log = logging.getLogger(__name__)
                        log.warning(
                            "COMMISSION GUARD leak inf=%s ch=%s tag=%s store=%s pubid=%s allowed=%s issues=%s snippet=%.200s",
                            inf.get("id"), ch.get("id"), effective_amz_tag, effective_hypd_store, expected_pubid, allowed_kinds,
                            "; ".join(audit["issues"])[:500], rendered[:200]
                        )
                        # Sanitize: remove raw meesho / unconverted merchant / wrong-tag links
                        sanitized, _ = commission_guard.sanitize_rendered_text(
                            rendered, effective_amz_tag, effective_hypd_store, expected_pubid, bitly_map=shortened_map, allowed_kinds=allowed_kinds
                        )
                        # Check if sanitized still has at least one OUR affiliate (amazon/hypd/EarnKaro/first-party/bitly-OUR)
                        remaining_urls = link_router.find_urls(sanitized)
                        has_affiliate = False
                        for u in remaining_urls:
                            kind = link_router.classify_url(u)
                            host = (link_router._host_of(u) if hasattr(link_router, "_host_of") else "")
                            # Bitly OUR links (advanced_only_our mode ensures any bit.ly is OUR)
                            if host in {"bit.ly", "www.bit.ly", "bitly.com", "www.bitly.com"} or host.endswith(".bit.ly"):
                                has_affiliate = True
                                break
                            if kind == "amazon" and effective_amz_tag and advanced_shortener.is_our_amazon_link(u, effective_amz_tag):
                                has_affiliate = True
                                break
                            if kind == "hypd" and effective_hypd_store and advanced_shortener.is_our_hypd_link(u, effective_hypd_store):
                                has_affiliate = True
                                break
                            if commission_guard.is_earnkaro_short_link(u):
                                has_affiliate = True
                                break
                            if kind == "lehlah":
                                has_affiliate = True
                                break
                            if "/amazon/" in u and effective_amz_tag and f"tag={effective_amz_tag}" in u:
                                has_affiliate = True
                                break
                            if "/m/" in u:
                                has_affiliate = True
                                break
                        if has_affiliate:
                            rendered = sanitized
                        else:
                            # No OUR affiliate left — posting would earn zero. Skip this channel.
                            per_channel[ch["id"]] = "skipped:no_our_affiliate_after_guard"
                            continue
                except Exception as _guard_exc:
                    import logging
                    logging.getLogger(__name__).exception("commission_guard failed: %s", _guard_exc)
            status = await dispatch_to_channel(inf, ch, rendered)
            db.record_post(inf["id"], ch["id"], sig,
                           status="posted" if status == "posted" else "failed",
                           error="" if status == "posted" else status,
                           deal_text=rendered if status == "posted" else "")
            per_channel[ch["id"]] = status
        results[inf["id"]] = per_channel
    return results


async def run_once(deals: Iterable[dict | str], influencer_ids: Iterable[int] | None = None) -> dict:
    """Process a batch of deals (dicts with ``text``/``source`` or plain strings)."""
    # Periodically prune old post/stat rows to bound database growth.
    try:
        if random.random() < 0.05:  # ~5% of run cycles
            db.purge_old_posts_and_stats(days_to_keep=14)
    except Exception:
        pass
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
    """Select the highest-rated recent deal and send a highlight banner.

    The ranking is based on the application's available deal signals; it does
    not predict sales or guarantee conversion performance.
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
            # 2. Respect Only-Amazon and category/price filters even for hourly highlights
            # This ensures perfect targeting - hourly loot respects influencer's preferences
            try:
                only_amz = str(ch.get("only_amazon") or inf.get("only_amazon") or "0").strip().lower() in {"1", "true", "yes", "on"}
                cat_filter = ch.get("categories") or inf.get("categories") or ""
                price_filter = ch.get("price_filter") or inf.get("price_filter") or "all"
                max_p, min_p = _parse_price_filter_spec(price_filter)
            except Exception:
                only_amz = False
                cat_filter = ""
                max_p = min_p = None

            recent_posts = db.get_recent_posted_deals(ch["id"], hours=1)
            # Filter posts with deal text - exclude previous highlights
            candidates = [p for p in recent_posts if p.get("deal_text") and not p.get("deal_text").startswith("👑")]
            # Apply perfect filtering to candidates: Only Amazon, category, price
            filtered_candidates = []
            for p in candidates:
                txt = p.get("deal_text", "")
                # Only-Amazon: must have Amazon link
                if only_amz and not link_router.has_amazon_link(txt):
                    continue
                if cat_filter and not link_router.matches_category_filter(txt, cat_filter):
                    continue
                if not link_router.matches_price_filter(txt, max_price=max_p, min_price=min_p):
                    continue
                filtered_candidates.append(p)
            if not filtered_candidates:
                continue

            # Pick the highest loot score deal - most attractive loot
            best_post = max(filtered_candidates, key=lambda p: link_router.calculate_deal_loot_score(p["deal_text"]))
            # Only highlight if score is decent (avoid highlighting mediocre deals)
            best_score = link_router.calculate_deal_loot_score(best_post["deal_text"])
            if best_score < 20.0:  # Skip low-score highlights to keep channel neat
                continue
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
