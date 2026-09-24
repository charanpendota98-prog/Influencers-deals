"""Influencer hub dashboard (Flask).

Single-origin app that:
  * lists / registers influencers
  * creates the Telegram channel for an influencer (via Telethon)
  * pairs the influencer's WhatsApp number (QR) through the wa_hub service,
    then creates a WA group feed + official Channel
  * shows per-influencer post stats and live VM/bot health

Run:  PYTHONPATH=.. FLASK_ENV=development python app.py
Binds 0.0.0.0 so it is reachable from the live preview.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for, session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from influencer_hub import config, db, whatsapp_client  # noqa: E402

app = Flask(__name__)
app.secret_key = "change-me-hub-dashboard"
WA_SESSION_KEY = lambda influencer_id: f"inf-{influencer_id}-wa"


def _run(coro):
    return asyncio.run(coro)


def clean_identifier(raw: str) -> str:
    """Normalize Telegram channel link/username or WhatsApp JID.
    e.g.:
      'https://t.me/ravi_loots'         -> '@ravi_loots'
      'https://t.me/+AbCdEf'            -> 'https://t.me/+AbCdEf' (Preserves invite links!)
      'https://chat.whatsapp.com/...'   -> WhatsApp invite link preserved
      '120363xxx@g.us'                  -> WhatsApp group JID preserved
      'ravi_loots'                      -> '@ravi_loots'
      '-100123456789'                   -> '-100123456789'
    """
    s = raw.strip()
    if not s:
        return ""

    # Preserve WhatsApp links and JIDs
    if "chat.whatsapp.com" in s or s.endswith("@g.us") or s.endswith("@newsletter") or s.startswith("120363"):
        return s

    # Preserve private Telegram invite links (+hash or joinchat/hash)
    if "t.me/+" in s or "telegram.me/+" in s or "joinchat/" in s or s.startswith("+"):
        if not s.startswith("http") and s.startswith("+"):
            return f"https://t.me/{s}"
        return s

    # Strip url prefix for standard public channels
    s = re.sub(r"^https?://(?:t\.me|telegram\.me)/", "", s, flags=re.I)
    s = re.sub(r"^t\.me/", "", s, flags=re.I)
    s = s.strip("/")

    if s.startswith("-100") or s.isdigit():
        return s
    if "@" in s:
        return s if s.startswith("@") else "@" + s.split("@")[-1]
    return f"@{s}" if not s.endswith(".net") and not s.endswith(".us") else s


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    influencers = db.search_influencers(q) if q else db.list_influencers()
    stats = db.post_stats()
    vm = db.latest_vm()
    settings = db.get_all_global_settings()
    sources = db.list_sources(active_only=False)
    # Master pool of deal sources (Meesho, Shopsy, Loot channels & network sources)
    default_sources = [
        ("Meesho Deals Official", "https://t.me/+6LA1ljXGlbNmMjA1"),
        ("Shopsy Loots Official", "https://t.me/+O3j4ghbtJzhjZjJl"),
        ("Premium Loot Deals", "https://t.me/+HUga1JTHwhBmNDE1"),
        ("Mega Loot Deals", "https://t.me/+8KzU3P58MJ9jN2M1"),
        ("Under 99 Special Loots", "https://t.me/+LP6MYEpCwi0zOGYx"),
        ("VIP Secret Loot Pool", "https://t.me/+qhlEwwkhb2hlNWZl"),
        ("Flash Lootzone Tricks", "https://t.me/+uV5wcTkUWJEwM2Y1"),
        ("Fast Deals Network", "https://t.me/+WvEWEYf7j3MyYzNl"),
        ("Instant Trick Alerts", "https://t.me/+t--iQ-QFeJZiNmVl"),
        ("Prime Lightning Deals", "https://t.me/+ky8g5O5KTr5mZmQ9"),
        ("Loot Matrix Network", "https://t.me/+LNRQ0Y1-9RkzZDRl"),
        ("Discount Express", "https://t.me/+-mv6ttVsltczNzFl"),
        ("OZ Mega Source Pool", "https://t.me/+vZKuuHCZcX44M2I1"),
        ("Fast Loot Tracker", "https://t.me/+FpXKV70NYNY0NzQ1"),
        ("PowerLoot Official", "@powerloot"),
        ("Deals Under 99", "@DealsUnder99_com"),
        ("Under 99 Loot Deals", "@under_99_loot_deals"),
        ("Loot Alerts Direct", "@loot_alerts"),
        ("Telugu Techworld Loots", "@TeluguTechworld"),
        ("Flipkarthiik Loots", "@Flipkarthiik"),
        ("SmartBuy Loots & Deals", "@SB_Loots_And_Deals"),
        ("IDOffers Prime", "@idoffers"),
        ("IDOffers 2", "@idoffers2"),
        ("Indian Online Offers", "@indian_online_offer"),
        ("TechGlare Deals", "@techglaredeals"),
        ("PriceHistory Deals", "@pricehistory"),
        ("DealDost Community", "@dealdost"),
        ("Magix Deals", "@Magixdeals_Magix"),
        ("Deals Velocity", "@dealsvelocity"),
        ("Rapid Deals Unlimited", "@rapiddeals_unlimited"),
        ("Meesho Shopsy Offers", "@msho_shpsy_offers"),
        ("Myntra Ajio Shopsy Deals", "@Myntra_Ajio_Deals_Shopsy"),
        ("GrabOn Deals India", "@GrabOnIndiaOfficial"),
        ("Hidden Deals Amazon", "@hidden_loot_deals_amazon"),
        ("Real Shopping Deals", "@RealShoppingDeals"),
        ("iCoolz Tricks & Loots", "@icoolzTricks"),
    ]
    existing_specs = {s["spec"] for s in sources}
    added_any = False
    for s_name, s_spec in default_sources:
        if s_spec not in existing_specs:
            db.add_source(s_name, s_spec)
            added_any = True
    if added_any:
        sources = db.list_sources(active_only=False)

    vault_err = request.args.get("vault_err")
    vault_success = request.args.get("vault_success")

    # Current effective affiliate credentials
    current_ek_key = settings.get("earnkaro_api_key") or config.EARNKARO_API_KEY or ""
    current_ek_pubid = settings.get("earnkaro_publisher_id") or config.EARNKARO_PUBLISHER_ID or "5478322"
    current_hypd_store = settings.get("hypd_store_id") or "93944"

    return render_template("index.html", influencers=influencers, stats=stats, vm=vm,
                           search_query=q, settings=settings, sources=sources,
                           vault_err=vault_err, vault_success=vault_success,
                           current_ek_key=current_ek_key, current_ek_pubid=current_ek_pubid,
                           current_hypd_store=current_hypd_store)


@app.route("/sources/add", methods=["POST"])
def add_deal_source():
    name = request.form.get("name", "").strip()
    spec = request.form.get("spec", "").strip()
    kind = request.form.get("kind", "production").strip()
    from_vault = request.form.get("from_vault") == "1"
    if spec:
        if not name:
            name = spec.split("/")[-1].replace("+", "").replace("@", "")
        db.add_source(name, spec, kind=kind)
    if from_vault:
        return redirect(url_for("secret_vault_tab"))
    return redirect(url_for("index"))


@app.route("/sources/<int:source_id>/delete", methods=["POST"])
def delete_deal_source(source_id):
    from_vault = request.form.get("from_vault") == "1"
    db.delete_source(source_id)
    if from_vault:
        return redirect(url_for("secret_vault_tab"))
    return redirect(url_for("index"))


@app.route("/sources/<int:source_id>/toggle", methods=["POST"])
def toggle_deal_source(source_id):
    from_vault = request.form.get("from_vault") == "1"
    db.toggle_source(source_id)
    if from_vault:
        return redirect(url_for("secret_vault_tab"))
    return redirect(url_for("index"))


@app.route("/global-settings/update", methods=["POST"])
def update_global_settings():
    nav_btn_en = "1" if request.form.get("nav_button_enabled") in ("1", "on", "true") else "0"
    nav_btn_text = request.form.get("nav_button_text", "Join Shpsy Loots ❤️").strip()
    nav_btn_url = request.form.get("nav_button_url", "").strip()

    db.set_global_setting("nav_button_enabled", nav_btn_en)
    db.set_global_setting("nav_button_text", nav_btn_text)
    db.set_global_setting("nav_button_url", nav_btn_url)

    return redirect(url_for("index"))


@app.route("/admin/affiliate-vault/update", methods=["POST"])
def update_affiliate_vault():
    pwd = request.form.get("admin_password", "").strip()

    if pwd != config.ADMIN_DELETE_PASSWORD:
        return redirect(url_for("secret_vault_tab", vault_err="invalid_password"))

    session["vault_unlocked"] = True
    ek_key = request.form.get("earnkaro_api_key", "").strip()
    ek_pubid = request.form.get("earnkaro_publisher_id", "").strip()
    hypd_store = request.form.get("hypd_store_id", "").strip()

    if ek_key:
        db.set_global_setting("earnkaro_api_key", ek_key)
    if ek_pubid:
        db.set_global_setting("earnkaro_publisher_id", ek_pubid)
    if hypd_store:
        db.set_global_setting("hypd_store_id", hypd_store)

    return redirect(url_for("secret_vault_tab", vault_success="1"))

@app.route("/admin/secret-vault", methods=["GET", "POST"])
def secret_vault_tab():
    """Separate password-protected tab for Central Vault & Deal Sources."""
    auth_err = False
    vault_success = request.args.get("vault_success") == "1"
    vault_err = request.args.get("vault_err")
    unlocked = session.get("vault_unlocked", False)

    if request.method == "POST":
        pwd = request.form.get("admin_password", "").strip()
        if pwd == config.ADMIN_DELETE_PASSWORD:
            session["vault_unlocked"] = True
            unlocked = True
        else:
            auth_err = True
    current_hypd_store = db.get_global_setting("hypd_store_id") or "93944"
    current_ek_pubid = db.get_global_setting("earnkaro_publisher_id") or "5478322"
    sources = db.list_sources()

    return render_template(
        "secret_vault.html",
        unlocked=unlocked,
        auth_err=auth_err,
        vault_success=vault_success,
        vault_err=vault_err,
        current_hypd_store=current_hypd_store,
        current_ek_pubid=current_ek_pubid,
        sources=sources
    )


@app.route("/admin/secret-vault/lock", methods=["POST"])
def lock_vault():
    session.pop("vault_unlocked", None)
    return redirect(url_for("secret_vault_tab"))


@app.route("/quick-add", methods=["POST"])
def quick_add():
    """Ultra-fast 1-click addition of influencer + their ready-made channels.
    No code deploy, no server restart needed!
    """
    name = request.form.get("name", "").strip()
    tag = request.form.get("tag", "").strip()
    phone = request.form.get("phone_number", "").strip()
    insta = request.form.get("insta_id", "").strip()
    handle = request.form.get("handle", "").strip()
    price_filt = request.form.get("price_filter", "all").strip()
    allowed_src = request.form.get("allowed_sources", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories_list = request.form.getlist("categories")
    categories = ",".join(c.strip() for c in categories_list if c.strip())
    schedule_list = request.form.getlist("posting_schedule")
    schedule = ",".join(s.strip() for s in schedule_list if s.strip()) or request.form.get("custom_schedule", "").strip()
    only_amazon = request.form.get("only_amazon") == "1"
    allow_amazon = request.form.get("allow_amazon", "1") == "1"
    allow_earnkaro = request.form.get("allow_earnkaro", "1") == "1"
    allow_hypd = request.form.get("allow_hypd", "1") == "1"
    hypd_store_id = request.form.get("hypd_store_id", "93944").strip() or "93944"
    if only_amazon:
        allow_earnkaro = False
        allow_hypd = False

    approval_tg = request.form.get("approval_tg", "").strip()
    broadcast_tg = request.form.get("broadcast_tg", "").strip()
    whatsapp_id = request.form.get("whatsapp_id", "").strip()
    strip_amz_all = request.form.get("strip_amazon_broadcast") == "1"

    # Custom button fields on quick add
    btn_enabled = request.form.get("custom_button_enabled") == "1"
    btn_text = request.form.get("custom_button_text", "Join Shpsy Loots ❤️").strip()
    btn_url = request.form.get("custom_button_url", "").strip()

    # Fallback to defaults so form never fails silently if minor details missed
    if not name:
        name = "Influencer-" + phone[-4:] if phone else "New Partner"
    if not tag:
        tag = "amzdeal-21"

    iid = db.add_influencer(name, tag, handle=handle, insta_id=insta,
                            phone_number=phone, price_filter=price_filt,
                            allowed_sources=allowed_src, bitly_api_key=bitly_key,
                            categories=categories, posting_schedule=schedule,
                            only_amazon=only_amazon, allow_amazon=allow_amazon,
                            allow_earnkaro=allow_earnkaro, allow_hypd=allow_hypd,
                            hypd_store_id=hypd_store_id)

    if btn_enabled or btn_url:
        db.update_influencer(iid, custom_button_enabled=btn_enabled,
                             custom_button_text=btn_text, custom_button_url=btn_url)

    # 1. Approval Channel
    if approval_tg:
        ident = clean_identifier(approval_tg)
        db.add_channel(iid, "telegram", ident, role="approval", status="ready",
                       allowed_sources=allowed_src, categories=categories, posting_schedule=schedule,
                       only_amazon=True, allow_amazon=True, allow_earnkaro=False,
                       allow_hypd=False, hypd_store_id=hypd_store_id)

    # 2. Broadcast Channel
    if broadcast_tg:
        ident = clean_identifier(broadcast_tg)
        db.add_channel(iid, "telegram", ident, role="broadcast", status="ready",
                       strip_amazon=strip_amz_all,
                       price_filter=price_filt if price_filt != "all" else "",
                       allowed_sources=allowed_src, bitly_api_key=bitly_key,
                       categories=categories, posting_schedule=schedule,
                       only_amazon=only_amazon, allow_amazon=allow_amazon,
                       allow_earnkaro=allow_earnkaro, allow_hypd=allow_hypd,
                       hypd_store_id=hypd_store_id)

    # 3. WhatsApp Channel/Group
    if whatsapp_id:
        db.add_channel(iid, "whatsapp_group", whatsapp_id, role="whatsapp", status="ready",
                       strip_amazon=strip_amz_all,
                       price_filter=price_filt if price_filt != "all" else "",
                       allowed_sources=allowed_src, bitly_api_key=bitly_key,
                       categories=categories, posting_schedule=schedule,
                       only_amazon=only_amazon, allow_amazon=allow_amazon,
                       allow_earnkaro=allow_earnkaro, allow_hypd=allow_hypd,
                       hypd_store_id=hypd_store_id)

    return redirect(url_for("influencer_detail", inf_id=iid))


@app.route("/influencer/<int:inf_id>/update-profile", methods=["POST"])
def update_profile(inf_id):
    name = request.form.get("name", "").strip()
    tag = request.form.get("amazon_tag", "").strip()
    phone = request.form.get("phone_number", "").strip()
    handle = request.form.get("handle", "").strip()
    insta = request.form.get("insta_id", "").strip()
    price_filt = request.form.get("price_filter", "").strip()
    allowed_src = request.form.get("allowed_sources", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories_list = request.form.getlist("categories")
    categories = ",".join(c.strip() for c in categories_list if c.strip()) if categories_list else request.form.get("categories", "").strip()
    schedule_list = request.form.getlist("posting_schedule")
    schedule = ",".join(s.strip() for s in schedule_list if s.strip()) or request.form.get("custom_schedule", "").strip()
    only_amazon = request.form.get("only_amazon") == "1"
    allow_amazon = request.form.get("allow_amazon", "1") == "1"
    allow_earnkaro = request.form.get("allow_earnkaro", "1") == "1"
    allow_hypd = request.form.get("allow_hypd", "1") == "1"
    hypd_store_id = request.form.get("hypd_store_id", "93944").strip() or "93944"
    notes = request.form.get("notes", "").strip()
    active_str = request.form.get("active")
    active = (active_str == "1") if active_str is not None else None

    # Custom navigation button fields
    btn_enabled = request.form.get("custom_button_enabled") == "1"
    btn_text = request.form.get("custom_button_text", "").strip()
    btn_url = request.form.get("custom_button_url", "").strip()

    # If user explicitly selected only_amazon=1, sync allow_earnkaro=0, allow_hypd=0
    if only_amazon:
        allow_earnkaro = False
        allow_hypd = False

    db.update_influencer(
        inf_id,
        name=name if name else None,
        amazon_tag=tag if tag else None,
        phone_number=phone if phone else None,
        handle=handle if handle else None,
        insta_id=insta if insta else None,
        price_filter=price_filt if price_filt else None,
        allowed_sources=allowed_src if allowed_src is not None else None,
        bitly_api_key=bitly_key if bitly_key is not None else None,
        categories=categories if categories is not None else None,
        posting_schedule=schedule if schedule is not None else None,
        only_amazon=only_amazon,
        allow_amazon=allow_amazon,
        allow_earnkaro=allow_earnkaro,
        allow_hypd=allow_hypd,
        hypd_store_id=hypd_store_id,
        custom_button_enabled=btn_enabled,
        custom_button_text=btn_text,
        custom_button_url=btn_url,
        notes=notes if notes else None,
        active=active,
    )
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/channel/<int:channel_id>/update", methods=["POST"])
def update_channel_route(channel_id):
    inf_id = request.form.get("inf_id")
    ident = request.form.get("identifier", "").strip()
    role = request.form.get("role", "").strip()
    status = request.form.get("status", "").strip()
    override_tag = request.form.get("amazon_override_tag", "").strip()
    price_filt = request.form.get("price_filter", "").strip()
    allowed_src = request.form.get("allowed_sources", "").strip()
    wa_key = request.form.get("wa_session_key", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories = request.form.get("categories", "").strip()
    schedule = request.form.get("posting_schedule", "").strip()
    only_amz = request.form.get("only_amazon") == "1"
    strip_amz = request.form.get("strip_amazon") == "1"
    allow_amz_str = request.form.get("allow_amazon")
    allow_amz = (allow_amz_str == "1") if allow_amz_str is not None else None
    allow_ek_str = request.form.get("allow_earnkaro")
    allow_ek = (allow_ek_str == "1") if allow_ek_str is not None else None
    allow_hypd_str = request.form.get("allow_hypd")
    allow_hypd = (allow_hypd_str == "1") if allow_hypd_str is not None else None
    hypd_store_id = request.form.get("hypd_store_id", "").strip()

    # Custom button fields per channel
    btn_en_str = request.form.get("custom_button_enabled")
    btn_en = (btn_en_str == "1") if btn_en_str is not None else None
    btn_text = request.form.get("custom_button_text")
    btn_url = request.form.get("custom_button_url")

    # If only_amazon is explicitly toggled on, sync allow_earnkaro=False, allow_hypd=False
    if only_amz:
        allow_ek = False
        allow_hypd = False

    if ident:
        ident = clean_identifier(ident) if not ident.startswith("120") else ident

    db.update_channel_details(
        channel_id,
        identifier=ident if ident else None,
        role=role if role else None,
        status=status if status else None,
        amazon_override_tag=override_tag,
        strip_amazon=strip_amz,
        price_filter=price_filt,
        allowed_sources=allowed_src,
        wa_session_key=wa_key,
        bitly_api_key=bitly_key,
        categories=categories,
        posting_schedule=schedule,
        only_amazon=only_amz,
        allow_amazon=allow_amz,
        allow_earnkaro=allow_ek,
        allow_hypd=allow_hypd,
        hypd_store_id=hypd_store_id if hypd_store_id else None,
        custom_button_enabled=btn_en,
        custom_button_text=btn_text,
        custom_button_url=btn_url,
    )
    if inf_id:
        return redirect(url_for("influencer_detail", inf_id=int(inf_id)))
    return redirect(url_for("index"))


@app.route("/influencer/<int:inf_id>/add-manual-channel", methods=["POST"])
def add_manual_channel(inf_id):
    platform = request.form.get("platform", "telegram").strip()
    raw_ident = request.form.get("identifier", "").strip()
    role = request.form.get("role", "broadcast").strip()
    invite = request.form.get("invite", "").strip()
    override_tag = request.form.get("amazon_override_tag", "").strip()
    price_filt = request.form.get("price_filter", "").strip()
    allowed_src = request.form.get("allowed_sources", "").strip()
    wa_key = request.form.get("wa_session_key", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories = request.form.get("categories", "").strip()
    schedule = request.form.get("posting_schedule", "").strip()
    only_amz = request.form.get("only_amazon") == "1"
    strip_amz = request.form.get("strip_amazon") == "1"
    allow_amz = request.form.get("allow_amazon", "1") == "1"
    allow_ek = request.form.get("allow_earnkaro", "1") == "1"
    allow_hypd = request.form.get("allow_hypd", "1") == "1"
    hypd_store_id = request.form.get("hypd_store_id", "93944").strip() or "93944"
    if only_amz:
        allow_ek = False
        allow_hypd = False

    if raw_ident:
        ident = clean_identifier(raw_ident) if platform == "telegram" else raw_ident
        db.add_channel(
            inf_id,
            platform,
            ident,
            invite_link=invite,
            status="ready",
            role=role,
            amazon_override_tag=override_tag,
            strip_amazon=strip_amz,
            price_filter=price_filt,
            allowed_sources=allowed_src,
            wa_session_key=wa_key,
            bitly_api_key=bitly_key,
            categories=categories,
            posting_schedule=schedule,
            only_amazon=only_amz,
            allow_amazon=allow_amazon,
            allow_earnkaro=allow_ek,
            allow_hypd=allow_hypd,
            hypd_store_id=hypd_store_id,
        )

    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/bulk-import", methods=["POST"])
def bulk_import():
    """Bulk import hundreds/thousands of influencers via CSV file upload or raw CSV text.
    Columns: name, amazon_tag, phone, insta, approval_tg, broadcast_tg, whatsapp_id, price_filter, allowed_sources
    """
    import csv
    import io

    records = []
    file = request.files.get("csv_file")
    raw_text = request.form.get("csv_text", "").strip()

    if file and file.filename:
        stream = io.StringIO(file.stream.read().decode("utf-8", errors="ignore"))
        reader = csv.DictReader(stream)
        records = list(reader)
    elif raw_text:
        stream = io.StringIO(raw_text)
        reader = csv.DictReader(stream)
        records = list(reader)

    if records:
        cleaned_records = []
        for r in records:
            name = (r.get("name") or "").strip()
            tag = (r.get("amazon_tag") or r.get("tag") or "").strip()
            if not name or not tag:
                continue
            rec = {
                "name": name,
                "amazon_tag": tag,
                "phone_number": (r.get("phone_number") or r.get("phone") or "").strip(),
                "insta_id": (r.get("insta_id") or r.get("insta") or "").strip(),
                "price_filter": (r.get("price_filter") or "all").strip(),
                "allowed_sources": (r.get("allowed_sources") or "").strip(),
                "bitly_api_key": (r.get("bitly_api_key") or r.get("bitly_key") or "").strip(),
                "categories": (r.get("categories") or "").strip(),
                "posting_schedule": (r.get("posting_schedule") or r.get("schedule") or "").strip(),
                "wa_session_key": (r.get("wa_session_key") or "").strip(),
                "approval_tg": clean_identifier(r.get("approval_tg", "")),
                "broadcast_tg": clean_identifier(r.get("broadcast_tg", "")),
                "whatsapp_id": r.get("whatsapp_id", "").strip(),
                "strip_amazon": (r.get("strip_amazon", "0").lower() in ("1", "true", "yes")),
            }
            cleaned_records.append(rec)

        added = db.add_bulk_influencers(cleaned_records)
        return redirect(url_for("index", imported=added))

    return redirect(url_for("index"))


@app.route("/channel/<int:channel_id>/delete", methods=["POST"])
def delete_channel(channel_id):
    inf_id = request.form.get("inf_id")
    db.delete_channel(channel_id)
    if inf_id:
        return redirect(url_for("influencer_detail", inf_id=int(inf_id)))
    return redirect(url_for("index"))


@app.route("/influencer/<int:inf_id>/toggle-active", methods=["POST"])
def toggle_influencer_active(inf_id):
    """Instant 1-click master switch to turn posting ON or totally OFF for this creator."""
    inf = db.get_influencer(inf_id)
    if inf:
        new_active = not bool(inf.get("active", 1))
        db.set_influencer_active(inf_id, new_active)
    ref = request.referrer or url_for("index")
    return redirect(ref)


@app.route("/channel/<int:channel_id>/toggle-status", methods=["POST"])
def toggle_channel_status(channel_id):
    """Instant 1-click switch to turn posting ON (ready) or totally OFF (paused) for this specific channel."""
    inf_id = request.form.get("inf_id")
    channels = db.list_channels(int(inf_id)) if inf_id else []
    ch = next((c for c in channels if c["id"] == channel_id), None)
    if ch:
        new_status = "paused" if ch.get("status") == "ready" else "ready"
        db.update_channel_details(channel_id, status=new_status)
    if inf_id:
        return redirect(url_for("influencer_detail", inf_id=int(inf_id)))
    return redirect(url_for("index"))


@app.route("/influencer/<int:inf_id>/delete", methods=["POST"])
def delete_influencer(inf_id):
    pwd = request.form.get("admin_password", "").strip()
    if pwd != config.ADMIN_DELETE_PASSWORD:
        return redirect(url_for("influencer_detail", inf_id=inf_id, err="invalid_password"))
    db.delete_influencer(inf_id)
    return redirect(url_for("index"))


@app.route("/influencer/<int:inf_id>/test-post", methods=["POST"])
def test_post_demo(inf_id):
    """Instant preview demo of how deals will render for all 3 channels."""
    return redirect(url_for("influencer_detail", inf_id=inf_id, demo=1))


@app.route("/onboard", methods=["GET", "POST"])
def onboard():
    if request.method == "GET":
        return render_template("onboard.html", result=None)
    name = request.form.get("name", "").strip()
    tag = request.form.get("tag", "").strip()
    phone = request.form.get("whatsapp", "").strip()
    handle = request.form.get("handle", "").strip()
    dummy = request.form.get("dummy") == "on"
    tg = request.form.get("tg") in ("on", "1", "true")
    wa = request.form.get("wa") in ("on", "1", "true")
    allow_amz = request.form.get("allow_amazon") in ("1", "on", "true")
    allow_ek = request.form.get("allow_earnkaro") in ("1", "on", "true")
    allow_hypd = request.form.get("allow_hypd") in ("1", "on", "true")
    hypd_store_id = request.form.get("hypd_store_id", "93944").strip() or "93944"

    if not name:
        name = "Influencer-" + phone[-4:] if phone else "New Partner"
    if not tag:
        tag = "amzdeal-21"

    iid = db.add_influencer(name, tag, handle=handle, phone_number=phone,
                            use_dummy_sources=dummy, telegram_enabled=tg,
                            whatsapp_enabled=wa, allow_amazon=allow_amz,
                            allow_earnkaro=allow_ek, allow_hypd=allow_hypd,
                            hypd_store_id=hypd_store_id)
    msgs = []
    # 3-channel auto setup for each influencer:
    # Channel 1: Amazon Approval Telegram channel (pure amazon.in + #ad disclosure)
    # Channel 2: Real / Broadcast Telegram channel (Amazon to their tag + others to EarnKaro)
    # Channel 3: WhatsApp session & group/channel
    if tg:
        # Channel 1: Amazon Approval
        try:
            appr = _run(telegram_ops_create(iid, f"{name} Amazon Deals", "Amazon Official Deals", role="approval"))
            msgs.append(f"Channel 1 (Amazon Approval TG): {appr.get('identifier')} [{appr.get('role')}]")
        except Exception as e:  # pragma: no cover
            msgs.append(f"Channel 1 (Approval TG) error: {e}")
        # Channel 2: Real Broadcast
        try:
            bcast = _run(telegram_ops_create(iid, f"{name} Loots", "Best Daily Deals", role="broadcast"))
            msgs.append(f"Channel 2 (Real Broadcast TG): {bcast.get('identifier')} [{bcast.get('role')}]")
        except Exception as e:  # pragma: no cover
            msgs.append(f"Channel 2 (Broadcast TG) error: {e}")
    if wa:
        # Channel 3: WhatsApp
        try:
            _run(whatsapp_client.create_session(iid, "wa"))
            db.upsert_wa_session(iid, WA_SESSION_KEY(iid), phone="")
            db.set_wa_session_status(WA_SESSION_KEY(iid), "qr")
            msgs.append("Channel 3 (WhatsApp): Session started — scan QR below to complete.")
        except Exception as e:  # pragma: no cover
            msgs.append(f"Channel 3 (WhatsApp) error: {e}")
    return render_template("onboard.html", result={"inf_id": iid, "name": name, "msgs": msgs})


@app.route("/influencer/<int:inf_id>/flags", methods=["POST"])
def set_flags(inf_id):
    tg = request.form.get("tg") in ("1", "true", "on", "yes")
    wa = request.form.get("wa") in ("1", "true", "on", "yes")
    db.set_channel_flags(inf_id, telegram_enabled=tg, whatsapp_enabled=wa)
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/onboard-tg", methods=["POST"])
def onboard_tg(inf_id):
    name = db.get_influencer(inf_id)['name']
    title_appr = request.form.get("title_appr") or f"{name} Amazon Deals"
    title_bcast = request.form.get("title_bcast") or f"{name} Loots"
    try:
        _run(telegram_ops_create(inf_id, title_appr, "Amazon Official Deals", role="approval"))
        _run(telegram_ops_create(inf_id, title_bcast, "Best Daily Deals", role="broadcast"))
    except Exception as e:  # pragma: no cover
        print("onboard-tg failed:", e)
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/onboard-wa", methods=["POST"])
def onboard_wa(inf_id):
    try:
        _run(whatsapp_client.create_session(inf_id, "wa"))
        db.upsert_wa_session(inf_id, WA_SESSION_KEY(inf_id), phone="")
        db.set_wa_session_status(WA_SESSION_KEY(inf_id), "qr")
    except Exception as e:  # pragma: no cover
        print("onboard-wa failed:", e)
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>")
def influencer_detail(inf_id):
    inf = db.get_influencer(inf_id)
    if not inf:
        return redirect(url_for("index"))
    channels = db.list_channels(inf_id)
    wa_sessions = db.list_wa_sessions(inf_id)
    stats = db.post_stats(inf_id)
    wa_key = WA_SESSION_KEY(inf_id)

    # Check live WhatsApp session status & fetch groups/channels
    wa_status_info = {"connected": False, "phone": "", "chats": []}
    try:
        status_res = _run(whatsapp_client.session_status(wa_key))
        if status_res.get("status") == "connected":
            wa_status_info["connected"] = True
            wa_status_info["phone"] = status_res.get("phone", "")
            chats = _run(whatsapp_client.list_chats(wa_key))
            wa_status_info["chats"] = [c for c in chats if c.get("isGroup") or c.get("isChannel")]
    except Exception:
        pass
    
    # Generate live preview demo if requested or for display
    demo_sample = (
        "🔥 Portronics Handheld Mini Fan, at Rs.799.\n"
        "https://www.amazon.in/dp/B0H5PTMXV1?tag=old-21\n"
        "Flipkart loot: https://www.flipkart.com/sony-headphones/p/itmABC123\n"
        "Myntra loot: https://www.myntra.com/bags/p/1234567"
    )
    from influencer_hub import link_router
    demo_approval = link_router.render_for_influencer(demo_sample, inf["amazon_tag"], role="approval")
    demo_broadcast = link_router.render_for_influencer(demo_sample, inf["amazon_tag"], role="broadcast")

    return render_template("influencer.html", inf=inf, channels=channels,
                           wa_sessions=wa_sessions, stats=stats, wa_key=wa_key,
                           wa_hub_url=config.WA_HUB_URL, wa_status_info=wa_status_info,
                           demo_approval=demo_approval, demo_broadcast=demo_broadcast)


@app.route("/influencer/<int:inf_id>/create-tg", methods=["POST"])
def create_tg(inf_id):
    title = request.form.get("title") or f"{db.get_influencer(inf_id)['name']} Loots"
    about = request.form.get("about", "")
    role = request.form.get("role", "broadcast")
    try:
        out = _run(telegram_ops_create(inf_id, title, about, role=role))
    except Exception as e:  # pragma: no cover
        out = {"error": str(e)}
    return redirect(url_for("influencer_detail", inf_id=inf_id))


async def telegram_ops_create(inf_id, title, about, role="broadcast"):
    from influencer_hub import telegram_ops
    return await telegram_ops.create_channel_for_influencer(inf_id, title, about, role=role)


@app.route("/influencer/<int:inf_id>/pair-wa", methods=["POST"])
def pair_wa(inf_id):
    key = WA_SESSION_KEY(inf_id)
    try:
        res = _run(whatsapp_client.create_session(inf_id, "wa"))
        db.upsert_wa_session(inf_id, key, phone="")
        db.set_wa_session_status(key, "qr")
    except Exception as e:  # pragma: no cover
        res = {"error": str(e)}
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/create-group", methods=["POST"])
def create_group(inf_id):
    key = WA_SESSION_KEY(inf_id)
    participant = request.form.get("participant", "").strip()  # e.g. 9198...@s.whatsapp.net
    subject = request.form.get("subject") or f"{db.get_influencer(inf_id)['name']} Deals"
    try:
        res = _run(whatsapp_client.create_group(key, subject, participant))
        jid = res.get("jid")
        if jid:
            db.add_channel(inf_id, "whatsapp_group", jid, status="ready")
    except Exception as e:  # pragma: no cover
        res = {"error": str(e)}
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/wa-connect-chat", methods=["POST"])
def wa_connect_chat(inf_id):
    """Directly connect an existing WhatsApp Group/Channel either by selecting from dropdown OR entering link."""
    jid = request.form.get("chat_jid", "").strip()
    invite_link = request.form.get("invite_link", "").strip()
    role = request.form.get("role", "whatsapp").strip()
    wa_key = WA_SESSION_KEY(inf_id)

    # If user provided invite link (e.g. https://chat.whatsapp.com/ABC123xyz), resolve it via Baileys socket!
    resolved_jid = jid
    allowed_src = request.form.get("allowed_sources", "").strip()
    if invite_link:
        try:
            res = _run(whatsapp_client.resolve_invite(wa_key, invite_link))
            if res.get("ok") and res.get("jid"):
                resolved_jid = res.get("jid")
            else:
                # Store invite link directly
                resolved_jid = clean_identifier(invite_link)
        except Exception:
            resolved_jid = clean_identifier(invite_link)

    if resolved_jid:
        db.add_channel(inf_id, "whatsapp_group" if resolved_jid.endswith("@g.us") else "whatsapp_channel",
                       resolved_jid, invite_link=invite_link, status="ready", role=role,
                       allowed_sources=allowed_src)

    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/api/test-render-deal", methods=["POST"])
def api_test_render_deal():
    """Live interactive simulator: tests link rewriting for Amazon, HYPD, and EarnKaro
    with instant real-time visual output in the dashboard!
    """
    sample_text = request.form.get("sample_text", "").strip()
    amz_tag = request.form.get("amazon_tag", "demo-21").strip()
    hypd_store = request.form.get("hypd_store_id", "93944").strip()
    role = request.form.get("role", "broadcast").strip()

    if not sample_text:
        sample_text = (
            "🔥 Loot Deals Today!\n"
            "1. Meesho Kurti: https://hypd.store/12345/afflink/daol5bac45l0tc0oo5rg\n"
            "2. Amazon Earbuds: https://www.amazon.in/dp/B08XYZ1234?tag=oldcreator-21\n"
            "3. Flipkart Shoes: https://www.flipkart.com/shoes/p/itm123456?affid=lehlah&affextparam1=test"
        )

    from influencer_hub import link_router
    rendered = link_router.render_for_influencer(
        sample_text,
        amazon_tag=amz_tag,
        role=role,
        hypd_store_id=hypd_store,
    )
    return jsonify({
        "ok": True,
        "input": sample_text,
        "rendered": rendered,
        "detected_links": link_router.collect_links(sample_text),
    })


@app.route("/trigger-all-hourly-loot", methods=["POST"])
def trigger_all_hourly_loot():
    """Trigger 'Loot of the Hour' highlight across all active channels."""
    try:
        from influencer_hub import pipeline
        res = _run(pipeline.run_hourly_loot_highlight())
        print(f"All channels hourly loot highlight triggered: {res}")
    except Exception as e:
        print(f"All hourly loot highlight failed: {e}")
    return redirect(url_for("index"))


@app.route("/influencer/<int:inf_id>/trigger-hourly-loot", methods=["POST"])
def trigger_hourly_loot(inf_id):
    """Manually or cron-triggered 'Loot of the Hour' highlight for this influencer."""
    try:
        from influencer_hub import pipeline
        res = _run(pipeline.run_hourly_loot_highlight(influencer_ids=[inf_id]))
        print(f"Hourly loot highlight triggered for inf_id {inf_id}: {res}")
    except Exception as e:
        print(f"Hourly loot highlight failed: {e}")
    return redirect(url_for("influencer_detail", inf_id=inf_id, hourly_loot=1))


@app.route("/channel/<int:channel_id>/send-test", methods=["POST"])
def send_test_message(channel_id):
    """Instant test message dispatcher to verify channel connectivity."""
    inf_id = int(request.form.get("inf_id", 0))
    inf = db.get_influencer(inf_id)
    channels = db.list_channels(inf_id)
    ch = next((c for c in channels if c["id"] == channel_id), None)

    status = "ok"
    err_msg = ""
    if ch and inf:
        test_payload = (
            f"✅ Test Alert: {inf['name']} Channel Connected Successfully!\n"
            f"Role: {ch['role'].upper()}\n"
            f"Channel: {ch['identifier']}\n"
            f"Timestamp: Auto-verification test."
        )
        try:
            from influencer_hub import pipeline
            res = _run(pipeline.dispatch_to_channel(inf, ch, test_payload))
            if res.startswith("failed:"):
                status = "failed"
                err_msg = res[len("failed:"):]
            else:
                status = "posted"
        except Exception as e:
            status = "failed"
            err_msg = str(e)
            print(f"Test dispatch failed: {e}")

    return redirect(url_for("influencer_detail", inf_id=inf_id, test_status=status, test_err=err_msg, ch_name=ch['identifier'] if ch else ''))


@app.route("/influencer/<int:inf_id>/create-newsletter", methods=["POST"])
def create_newsletter(inf_id):
    key = WA_SESSION_KEY(inf_id)
    name = request.form.get("name") or f"{db.get_influencer(inf_id)['name']} Channel"
    desc = request.form.get("description", "")
    try:
        res = _run(whatsapp_client.create_channel(key, name, desc))
        jid = res.get("jid")
        if jid:
            db.add_channel(inf_id, "whatsapp_channel", jid, status="ready")
    except Exception as e:  # pragma: no cover
        res = {"error": str(e)}
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/api/wa/<key>/qr")
def wa_qr(key):
    url = f"{config.WA_HUB_URL.rstrip('/')}/sessions/{key}/qr"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return jsonify(json.loads(r.read()))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "qr": None})


@app.route("/api/wa/<key>/status")
def wa_status(key):
    url = f"{config.WA_HUB_URL.rstrip('/')}/sessions/{key}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return jsonify(json.loads(r.read()))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/vm")
def vm():
    vm = db.latest_vm()
    return render_template("vm.html", vm=vm)


@app.route("/api/vm")
def api_vm():
    return jsonify(db.latest_vm() or {})


if __name__ == "__main__":
    db.init()
    app.run(host="0.0.0.0", port=5000, debug=False)
