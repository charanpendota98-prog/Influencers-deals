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
    """One-shot health check: proves the whole setup is correctly wired."""
    db.init()
    import base64
    import json
    import urllib.request

    checks = []

    # 1) EarnKaro API key present
    checks.append(("EarnKaro API key set", bool(config.EARNKARO_API_KEY)))

    # 2) Publisher id consistency: JWT claim == .env == our known account 5478322
    try:
        parts = (config.EARNKARO_API_KEY or "").split(".")
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claim = json.loads(base64.urlsafe_b64decode(pad))
        jwt_pub = str(claim.get("earnkaro"))
    except Exception:
        jwt_pub = "?"
    pub_ok = (jwt_pub == config.EARNKARO_PUBLISHER_ID == "5478322")
    checks.append((f"Publisher id 5478322 (jwt={jwt_pub}, .env={config.EARNKARO_PUBLISHER_ID})", pub_ok))

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

    # 5) Database readable/writable
    try:
        db.list_influencers()
        db_ok = True
    except Exception:
        db_ok = False
    checks.append(("Database OK", db_ok))

    print("=== Influencer Hub doctor ===")
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'XX'}] {name}")
    all_ok = all(o for _, o in checks)
    print("\nVerdict:", "ALL GOOD ✅" if all_ok else "check the XX items above")
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

    d = sub.add_parser("doctor", help="one-shot health check of the whole setup")
    d.set_defaults(func=_cmd_doctor)

    sub.add_parser("status", help="overview of everything").set_defaults(func=_cmd_status)

    r = sub.add_parser("render-demo", help="show how a sample deal renders for an influencer")
    r.add_argument("id", type=int)
    r.set_defaults(func=_cmd_render_demo)

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
