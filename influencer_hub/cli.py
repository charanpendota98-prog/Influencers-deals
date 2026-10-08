"""Command line management for the influencer hub.

Examples
--------
  python -m influencer_hub.cli init
  python -m influencer_hub.cli add-influencer "Ravi" --tag ravi099-21 --handle @ravi
  python -m influencer_hub.cli list
  python -m influencer_hub.cli create-tg 1 --title "Ravi Loots"
  python -m influencer_hub.cli pair-wa 1
  python -m influencer_hub.cli status
  python -m influencer_hub.cli render-demo 1
  python -m influencer_hub.cli audit-links --text "Deal ... bit.ly/abc"
  python -m influencer_hub.cli audit-links --file /tmp/post.txt --json
  python -m influencer_hub.cli vm-watch
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from . import db, link_router, vm_watch
from . import config


def _cmd_init(args):  # pragma: no cover - side effect
    db.init()
    print("DB initialised at", config.DB_PATH)


def _cmd_add(args):  # pragma: no cover - side effect
    db.init()
    iid = db.add_influencer(args.name, args.tag, handle=args.handle or "",
                            notes=args.notes or "", use_dummy_sources=args.dummy,
                            telegram_enabled=not args.no_tg,
                            whatsapp_enabled=not args.no_wa)
    print(f"Created influencer id={iid} name={args.name} tag={args.tag} "
          f"tg={not args.no_tg} wa={not args.no_wa}")


def _cmd_list(args):  # pragma: no cover - side effect
    db.init()
    influencers = db.list_influencers()
    if not influencers:
        print("No influencers yet.")
        return
    for inf in influencers:
        chs = db.list_channels(inf["id"])
        print(f"#{inf['id']} {inf['name']} (@{inf['handle'] or '-'}) "
              f"tag={inf['amazon_tag']} active={bool(inf['active'])} "
              f"dummy_src={bool(inf['use_dummy_sources'])}")
        for ch in chs:
            print(f"    - {ch['platform']:16} {ch['identifier']:24} "
                  f"[{ch['status']}] {ch['invite_link']}")


def _cmd_create_tg(args):  # pragma: no cover - network
    db.init()
    inf = db.get_influencer(args.id)
    if not inf:
        print("No such influencer", file=sys.stderr); return 1
    from . import telegram_ops

    async def _create():
        try:
            return await telegram_ops.create_channel_for_influencer(
                args.id, args.title or f"{inf['name']} Loots", args.about or ""
            )
        finally:
            await telegram_ops.disconnect()

    out = asyncio.run(_create())
    print("Telegram channel:", out)


def _cmd_pair_wa(args):  # pragma: no cover - network
    db.init()
    from . import whatsapp_client
    label = "wa"
    res = asyncio.run(whatsapp_client.create_session(args.id, label))
    print("WA session created. Open the dashboard or poll QR:")
    print(res)


def _cmd_status(args):  # pragma: no cover - side effect
    db.init()
    print("== Influencers ==")
    for inf in db.list_influencers():
        print(f"  #{inf['id']} {inf['name']} tag={inf['amazon_tag']} "
              f"active={bool(inf['active'])}")
    print("== Channels ==")
    for ch in db.list_channels():
        print(f"  inf={ch['influencer_id']} {ch['platform']:16} {ch['identifier']:24} [{ch['status']}]")
    print("== WA sessions ==")
    for s in db.list_wa_sessions():
        print(f"  inf={s['influencer_id']} key={s['session_key']} status={s['status']} phone={s['phone']}")
    print("== Post stats ==")
    print(" ", db.post_stats())
    print("== VM ==")
    print(" ", db.latest_vm())


def _cmd_render_demo(args):  # pragma: no cover - demo
    db.init()
    inf = db.get_influencer(args.id)
    if not inf:
        print("No such influencer", file=sys.stderr); return 1
    sample = (
        "🔥 Portronics Handheld Mini Fan, at Rs.799.\n"
        "https://www.amazon.in/dp/B0H5PTMXV1?tag=old-21\n"
        "Flipkart loot: https://www.flipkart.com/sony-headphones/p/itmABC123\n"
        "Myntra loot: https://www.myntra.com/bags/p/1234567"
    )
    # Build EarnKaro map (network); falls back to passthrough when no API key.
    from . import earnkaro
    ek = asyncio.run(earnkaro.convert_links(
        {u for u, k in link_router.collect_links(sample).items() if k == "merchant"}))

    print(f"=== [DEMO] INFLUENCER #{inf['id']} {inf['name']} (Amazon tag={inf['amazon_tag']}) ===")
    print("\n-----------------------------------------------------------")
    print("🛡️ CHANNEL 1: AMAZON APPROVAL CHANNEL (Native Amazon + #ad)")
    print("-----------------------------------------------------------")
    print(link_router.render_for_influencer(sample, inf["amazon_tag"], ek, role="approval"))

    print("\n-----------------------------------------------------------")
    print("📢 CHANNEL 2: REAL / BROADCAST TG CHANNEL (Full deals)")
    print("-----------------------------------------------------------")
    print(link_router.render_for_influencer(sample, inf["amazon_tag"], ek, role="broadcast"))

    print("\n-----------------------------------------------------------")
    print("💬 CHANNEL 3: WHATSAPP CHANNEL / GROUP (Full deals)")
    print("-----------------------------------------------------------")
    print(link_router.render_for_influencer(sample, inf["amazon_tag"], ek, role="whatsapp"))


def _cmd_audit_links(args):  # pragma: no cover - operator tool
    """Show exactly what a source post becomes — read-only, nothing is posted.

    Prints every link in the post, how it is classified, where a wrapper really
    points, the text that would be published and the commission verdict. Exit
    code 0 means the post would carry at least one of OUR affiliate links; 1
    means it would carry none (the worker would hold it for a retry when a
    conversion/wrapper can still be resolved).
    """
    import json
    import pathlib

    from . import commission_guard, earnkaro

    text = args.text or ""
    if getattr(args, "file", None):
        text = pathlib.Path(args.file).read_text(encoding="utf-8")
    if not text.strip():
        print("Give a post with --text \"...\" or --file path", file=sys.stderr)
        return 2

    tag = (args.tag or config.AMAZON_ASSOCIATE_TAG or "").strip()
    store = (args.store or config.HYPD_STORE_ID or "").strip()
    pubid = (args.publisher_id or config.EARNKARO_PUBLISHER_ID or "").strip()
    resolve = not args.offline
    converted: dict[str, str] = {}
    unresolved: list[str] = []

    try:
        db.init()
    except Exception:
        pass
    try:
        if db.get_global_setting("earnkaro_publisher_id"):
            pubid = (db.get_global_setting("earnkaro_publisher_id") or pubid).strip()
    except Exception:
        pass

    if resolve:
        text, unresolved = asyncio.run(
            link_router.resolve_opaque_links_in_text_async(text, tag)
        )
        merchant_urls = {
            url for url, kind in link_router.collect_links(text).items() if kind == "merchant"
        }
        if merchant_urls:
            converted = asyncio.run(earnkaro.convert_links(merchant_urls))

    rendered = link_router.render_for_influencer(text, tag, converted)
    rendered = link_router.deduplicate_urls_in_text(rendered)
    audit = commission_guard.audit_rendered_text(rendered, tag, store, pubid, source_text=text)
    ours = commission_guard.our_affiliate_urls(rendered, tag, store, pubid, source_text=text)

    if args.json:
        print(json.dumps({
            "amazon_tag": tag,
            "hypd_store": store,
            "earnkaro_publisher_id": pubid,
            "links": [
                {
                    "url": url,
                    "kind": link_router.classify_url(url),
                    "ours": url in set(ours),
                    "note": next(
                        (d.get("reason", "") for d in audit["details"] if d.get("url") == url), ""
                    ),
                }
                for url in link_router.find_urls(rendered)
            ],
            "unresolved_wrappers": unresolved,
            "our_links": list(ours),
            "rendered": rendered,
            "verdict": "our_link_present" if ours else "no_our_link",
        }, indent=2, ensure_ascii=False))
        return 0 if ours else 1

    print(f"=== LINK AUDIT (tag={tag} store={store} publisher={pubid or 'unset'}) ===")
    print(f"resolve wrappers over the network: {'yes' if resolve else 'no (--offline)'}")
    print("\n-- links in the republished post --")
    for url in link_router.find_urls(rendered):
        kind = link_router.classify_url(url)
        mark = "OUR" if url in set(ours) else "   "
        note = next((d.get("reason", "") for d in audit["details"] if d.get("url") == url), "")
        print(f"  [{mark}] {kind:9s} {url}")
        if note:
            print(f"        {note}")
    if unresolved:
        print("\n-- wrappers we could not resolve (attribution unknown) --")
        for url in unresolved:
            print(f"  {url}")
    print("\n-- text that would be posted --")
    print(rendered)
    print("\n-- verdict --")
    if ours:
        print(f"POST: {len(ours)} of OUR affiliate link(s) present")
        return 0
    print("HOLD: no link in this post pays us (worker parks it for a retry)")
    return 1


def _cmd_vm_watch(args):  # pragma: no cover - side effect
    db.init()
    if args.loop:
        asyncio.run(vm_watch.watch_loop())
    else:
        snap = asyncio.run(vm_watch.snapshot_and_report())
        print(snap)


def _cmd_run_pipeline(args):  # pragma: no cover - network
    db.init()
    from . import pipeline, puller
    selected_influencer = db.get_influencer(args.id) if args.id else None
    if args.id and not selected_influencer:
        print("No such influencer", file=sys.stderr)
        return 1
    use_dummy = selected_influencer.get("use_dummy_sources", False) if selected_influencer else False

    async def _pull_and_dispatch():
        try:
            deals = await puller.pull_recent_deals(
                limit=args.limit, use_dummy=bool(use_dummy), include_source=True
            )
            return deals, await pipeline.run_once(deals, influencer_ids=[args.id] if args.id else None)
        finally:
            await pipeline.close()

    deals, results = asyncio.run(_pull_and_dispatch())
    print(f"Pulled {len(deals)} deals from the shared pool.")
    for iid, chmap in results.items():
        posted = sum(1 for s in chmap.values() if s == "posted")
        print(f"  influencer #{iid}: {posted} posts dispatched")
    return 0


def _cmd_onboard_tg(args):  # pragma: no cover - network
    db.init()
    inf = db.get_influencer(args.id)
    if not inf:
        print("No such influencer", file=sys.stderr); return 1
    if not inf["telegram_enabled"]:
        print("Telegram disabled for this influencer (use --no-tg to enable)."); return 1
    from . import telegram_ops
    name = inf["name"]

    async def _create_channels():
        try:
            approval = await telegram_ops.create_channel_for_influencer(
                args.id, args.title or f"{name} Amazon", args.about or "Amazon deals (approval)",
                role="approval",
            )
            broadcast = await telegram_ops.create_channel_for_influencer(
                args.id, args.title2 or f"{name} Loots", args.about or "", role="broadcast",
            )
            return approval, broadcast
        finally:
            await telegram_ops.disconnect()

    approval, broadcast = asyncio.run(_create_channels())
    print("Amazon approval channel:", approval)
    print("Real / broadcast channel:", broadcast)


def _cmd_onboard_wa(args):  # pragma: no cover - network
    db.init()
    inf = db.get_influencer(args.id)
    if not inf:
        print("No such influencer", file=sys.stderr); return 1
    if not inf["whatsapp_enabled"]:
        print("WhatsApp disabled for this influencer (use --no-wa to enable)."); return 1
    from . import whatsapp_client
    key = whatsapp_client.WA_SESSION_KEY(args.id)
    res = asyncio.run(whatsapp_client.create_session(args.id, "wa"))
    db.upsert_wa_session(args.id, key, phone="")
    db.set_wa_session_status(key, "qr")
    print("WhatsApp session started. Scan the QR in the dashboard (/influencer/<id>).")
    print(res)


def _cmd_onboard(args):  # pragma: no cover - network
    """One-shot onboarding: Telegram + WhatsApp started together (if enabled)."""
    db.init()
    inf = db.get_influencer(args.id)
    if not inf:
        print("No such influencer", file=sys.stderr); return 1
    if inf["telegram_enabled"]:
        _cmd_onboard_tg(args)
    else:
        print("(telegram disabled for this influencer — skipping TG)")
    if inf["whatsapp_enabled"]:
        _cmd_onboard_wa(args)
    else:
        print("(whatsapp disabled for this influencer — skipping WA)")
    print(f"Onboarding started for #{args.id} {inf['name']}. "
          "Scan the WA QR in the dashboard, then create the group + channel.")


def _cmd_add_bulk(args):  # pragma: no cover - side effect
    """Import many influencers from a CSV (name,tag,handle,dummy)."""
    import csv
    db.init()
    created = 0
    with open(args.csv, newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            tag = (row.get("tag") or row.get("amazon_tag") or config.AMAZON_ASSOCIATE_TAG).strip()
            if not name:
                continue
            db.add_influencer(name, tag, handle=(row.get("handle") or "").strip(),
                              use_dummy_sources=(row.get("dummy", "").lower() in ("1", "true", "yes")),
                              telegram_enabled=(row.get("telegram", "1").lower() != "0"),
                              whatsapp_enabled=(row.get("whatsapp", "1").lower() != "0"))
            created += 1
    print(f"Imported {created} influencers from {args.csv}")


def _cmd_set_flags(args):  # pragma: no cover - side effect
    db.init()
    db.set_channel_flags(args.id, telegram_enabled=args.tg, whatsapp_enabled=args.wa)
    print(f"Updated channel flags for #{args.id}: tg={args.tg} wa={args.wa}")


def _cmd_verify_earnkaro(args):  # pragma: no cover - network
    db.init()
    from . import earnkaro
    res = asyncio.run(earnkaro.verify_earnkaro(args.url))
    print(res)


def _cmd_doctor(args):  # pragma: no cover - side effect
    """Check selected local configuration and service health signals.

    Passing these checks is not an end-to-end delivery or affiliate-commission
    verification.
    """
    db.init()
    import base64
    import json
    import urllib.request

    checks = []

    # Dashboard vault settings take precedence, matching the dispatch path.
    ek_key = db.get_global_setting("earnkaro_api_key") or config.EARNKARO_API_KEY
    ek_pubid = db.get_global_setting("earnkaro_publisher_id") or config.EARNKARO_PUBLISHER_ID
    checks.append(("EarnKaro API credential configured", bool(ek_key)))
    checks.append(("EarnKaro publisher ID configured", bool(ek_pubid)))

    # If the credential is a JWT with an EarnKaro claim, compare it to the
    # selected publisher ID without exposing the credential itself.
    jwt_pub = None
    try:
        parts = (ek_key or "").split(".")
        if len(parts) >= 2:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            claim = json.loads(base64.urlsafe_b64decode(pad))
            if claim.get("earnkaro") is not None:
                jwt_pub = str(claim["earnkaro"])
    except Exception:
        pass
    if jwt_pub is not None:
        pub_ok = jwt_pub == str(ek_pubid)
        checks.append((f"Publisher ID matches credential claim ({ek_pubid})", pub_ok))

    # 3) WhatsApp hub reachable
    try:
        with urllib.request.urlopen(config.WA_HUB_URL + "/health", timeout=5) as r:
            wa_ok = r.status == 200
    except Exception:
        wa_ok = False
    checks.append((f"WhatsApp hub reachable @ {config.WA_HUB_URL}", wa_ok))

    # 4) Telegram creds present (needed to create channels)
    tg_ok = bool(config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH and config.TELEGRAM_SESSION)
    checks.append(("Telegram creds set", tg_ok))

    # 5) Database readable
    try:
        db.list_influencers()
        db_ok = True
    except Exception:
        db_ok = False
    checks.append(("Database OK", db_ok))

    source_note = ""
    if getattr(args, "telegram_sources", False):
        from . import puller

        try:
            timeout = max(1, int(getattr(args, "timeout", 30)))
            report = asyncio.run(asyncio.wait_for(puller.inspect_source_selection(), timeout=timeout))
            source_ok = bool(report.get("ok"))
            checks.append((
                "Telegram source selection "
                f"({report.get('selected_sources', 0)} selected / "
                f"{report.get('configured_sources', 0)} configured)",
                source_ok,
            ))
            if report.get("selection_mode") == "joined_dialog_fallback":
                source_note = (
                    f"{report.get('unresolved_private_invites', 0)} private invite label(s) did not match "
                    "an exact joined-dialog title. The worker is using eligible already-joined dialogs "
                    "only; it did not check or join invites."
                )
            elif not report.get("configured_sources"):
                source_note = "No production source selectors are configured."
            elif not report.get("selected_sources"):
                source_note = (
                    "No joined dialogs matched. Join the sources with this Telegram account and "
                    "verify public usernames or exact private-dialog titles."
                )
        except asyncio.TimeoutError:
            checks.append(("Telegram live source selection finished before timeout", False))
            source_note = "Telegram source inspection timed out; check the session, network and worker logs."
        except Exception as exc:
            checks.append((f"Telegram live source selection ({type(exc).__name__})", False))
            source_note = "Telegram source inspection failed; check API credentials and session authorization."

    print("=== Influencer Hub configuration checks ===")
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'XX'}] {name}")
    if source_note:
        print(f"  [WARN] {source_note}")
    all_ok = all(o for _, o in checks)
    verdict = (
        "selected checks passed (this is not an end-to-end delivery test)"
        if all_ok else "check the XX items above"
    )
    print("\nVerdict:", verdict)
    return 0 if all_ok else 1


def _cmd_amazon_items(args):  # pragma: no cover - requires configured Amazon credentials
    """Fetch product details from the official Amazon Creators API."""
    import json
    from .amazon_creators import AmazonCreatorsAPI

    async def _fetch():
        client = AmazonCreatorsAPI()
        return await client.get_items(args.asins)

    result = asyncio.run(_fetch())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="influencer_hub", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="initialise the database").set_defaults(func=_cmd_init)

    a = sub.add_parser("add-influencer", help="register an influencer")
    a.add_argument("name")
    a.add_argument("--tag", default=config.AMAZON_ASSOCIATE_TAG,
                   help=f"Amazon associate tag (default: {config.AMAZON_ASSOCIATE_TAG})")
    a.add_argument("--handle", default="")
    a.add_argument("--notes", default="")
    a.add_argument("--dummy", action="store_true", help="stage using dummy sources first")
    a.add_argument("--no-tg", action="store_true", help="disable Telegram for this influencer")
    a.add_argument("--no-wa", action="store_true", help="disable WhatsApp for this influencer")
    a.set_defaults(func=_cmd_add)

    sub.add_parser("list", help="list influencers + channels").set_defaults(func=_cmd_list)

    t = sub.add_parser("create-tg", help="create the influencer's Telegram channel")
    t.add_argument("id", type=int)
    t.add_argument("--title", default="")
    t.add_argument("--about", default="")
    t.set_defaults(func=_cmd_create_tg)

    w = sub.add_parser("pair-wa", help="start a WhatsApp session (QR) for an influencer")
    w.add_argument("id", type=int)
    w.set_defaults(func=_cmd_pair_wa)

    otg = sub.add_parser("onboard-tg", help="onboard Telegram channels (approval + broadcast)")
    otg.add_argument("id", type=int)
    otg.add_argument("--title", default="", help="approval channel title")
    otg.add_argument("--title2", default="", help="broadcast channel title")
    otg.add_argument("--about", default="")
    otg.set_defaults(func=_cmd_onboard_tg)

    ow = sub.add_parser("onboard-wa", help="onboard WhatsApp (QR) only")
    ow.add_argument("id", type=int)
    ow.set_defaults(func=_cmd_onboard_wa)

    ob = sub.add_parser("onboard", help="one-shot onboard (TG + WA, per enabled flags)")
    ob.add_argument("id", type=int)
    ob.add_argument("--title", default="")
    ob.add_argument("--about", default="")
    ob.set_defaults(func=_cmd_onboard)

    b = sub.add_parser("add-influencers", help="bulk import from CSV (name,tag,handle,dummy)")
    b.add_argument("--csv", required=True)
    b.set_defaults(func=_cmd_add_bulk)

    sf = sub.add_parser("set-flags", help="enable/disable TG or WA per influencer")
    sf.add_argument("id", type=int)
    sf.add_argument("--tg", type=lambda v: v.lower() in ("1", "true", "yes", "on"), required=True)
    sf.add_argument("--wa", type=lambda v: v.lower() in ("1", "true", "yes", "on"), required=True)
    sf.set_defaults(func=_cmd_set_flags)

    ve = sub.add_parser("verify-earnkaro", help="live test conversion against EarnKaro API")
    ve.add_argument("--url", default="https://www.flipkart.com/p/itmEXAMPLE12345")
    ve.set_defaults(func=_cmd_verify_earnkaro)

    amz = sub.add_parser("amazon-items", help="look up ASINs with Amazon Creators API")
    amz.add_argument("asins", nargs="+", help="one to ten Amazon ASINs")
    amz.set_defaults(func=_cmd_amazon_items)

    d = sub.add_parser("doctor", help="check selected local configuration and service health")
    d.add_argument(
        "--telegram-sources",
        action="store_true",
        help="also run a read-only Telegram source-selection check; never checks invites or reads history",
    )
    d.add_argument("--timeout", type=int, default=30, help="live Telegram source-check timeout in seconds")
    d.set_defaults(func=_cmd_doctor)

    sub.add_parser("status", help="overview of everything").set_defaults(func=_cmd_status)

    r = sub.add_parser("render-demo", help="show how a sample deal renders for an influencer")
    r.add_argument("id", type=int)
    r.set_defaults(func=_cmd_render_demo)

    al = sub.add_parser(
        "audit-links",
        help="show what a source post becomes (read-only): links, wrappers, OUR links, verdict",
    )
    al.add_argument("--text", default="", help="the source post text")
    al.add_argument("--file", default="", help="read the post text from a file instead")
    al.add_argument("--tag", default="", help="override the Amazon Associate tag")
    al.add_argument("--store", default="", help="override the HYPD store id")
    al.add_argument("--publisher-id", default="", help="override the EarnKaro publisher id")
    al.add_argument("--offline", action="store_true", help="skip resolution/conversion network calls")
    al.add_argument("--json", action="store_true", help="machine-readable output")
    al.set_defaults(func=_cmd_audit_links)

    v = sub.add_parser("vm-watch", help="snapshot VM health (or loop)")
    v.add_argument("--loop", action="store_true")
    v.set_defaults(func=_cmd_vm_watch)

    rp = sub.add_parser("run-pipeline", help="pull shared deals and dispatch to influencers")
    rp.add_argument("--id", type=int, default=None, help="limit to one influencer")
    rp.add_argument("--limit", type=int, default=10, help="messages per source")
    rp.set_defaults(func=_cmd_run_pipeline)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    return rc or 0


if __name__ == "__main__":
    raise SystemExit(main())
