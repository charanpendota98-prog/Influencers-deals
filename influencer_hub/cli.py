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
  python -m influencer_hub.cli self-check --live
  python -m influencer_hub.cli self-check --alert        # only when it got worse
  python -m influencer_hub.cli digest --send
  python -m influencer_hub.cli amazon-scan --file /tmp/post.txt --fleet   # every creator, own tag
  python -m influencer_hub.cli amazon-scan --report --check               # what we really sent
  python -m influencer_hub.cli export-inventory --out /tmp/hub-creators.csv
  python -m influencer_hub.cli import-inventory --file /tmp/hub-creators.csv --apply
  python -m influencer_hub.cli assignments        # whose tag / channel is whose (read-only)
  python -m influencer_hub.cli add-defaults --show                     # what new creators start with
  python -m influencer_hub.cli add-defaults --only-amazon off --mirror-whatsapp on
  python -m influencer_hub.cli totp --new        # admin two-factor secret for the authenticator app
  python -m influencer_hub.cli snapshot          # one row of history per day
  python -m influencer_hub.cli backup --out /var/backups/hub --keep 14
  python -m influencer_hub.cli restore /var/backups/hub/hub-20261011-0900.sqlite3 --yes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import db, deploy, link_router, vm_watch
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


def _cmd_deploy_check(args):  # noqa: D401 - read-only by construction
    """What this box has, and what the next deploy would do — changing nothing."""
    report = deploy.preflight(
        repo=Path(args.repo) if args.repo else None,
        branch=args.branch or "",
        backup_dir=Path(args.backups) if args.backups else None,
    )
    if args.json:
        print(deploy.as_json(report))
        return 0 if report["ok"] else 1
    print(f"== Deploy preflight · {report['repo']} · branch {report['branch']} ==")
    for check in report["checks"]:
        mark = "OK" if check["ok"] else ("XX" if check["required"] else "--")
        print(f"  [{mark}] {check['name']:<24} {check['detail']}")
        if not check["ok"] and check["fix"]:
            print(f"       fix: {check['fix']}")
    print()
    print(f"Verdict: {report['summary']}")
    if args.show_plan:
        print()
        print("A deploy would run, in order:")
        for step in report["next_deploy"]:
            print(f"  {step['step']:>2}. {step['name']:<20} {step['command']}")
            print(f"      {step['why']}")
    return 0 if report["ok"] else 1


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

    short_link_note = ""
    if getattr(args, "short_links", False):
        from . import amazon_shortlinks

        status = amazon_shortlinks.short_link_status()
        short_ok = bool(status["active"])
        checks.append((
            "Branded affiliate short-link HTTPS origin "
            + (status["base_url"] if short_ok else "configured"),
            short_ok,
        ))
        short_link_note = (
            "Local configuration is valid. This check does not make an external request; "
            "save/Test a Deal, then open the returned /a/<code> URL once to verify your DNS/TLS proxy."
            if short_ok else status["reason"]
        )

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
    if short_link_note:
        print(f"  [INFO] {short_link_note}")
    all_ok = all(o for _, o in checks)
    verdict = (
        "selected checks passed (this is not an end-to-end delivery test)"
        if all_ok else "check the XX items above"
    )
    print("\nVerdict:", verdict)
    return 0 if all_ok else 1


def _cmd_self_check(args):
    """Run the hub self-check from the shell (same checks as the dashboard)."""
    import json as _json

    from . import ops, selfcheck

    report = selfcheck.run_checks(live=args.live)
    if args.alert:
        result = ops.maybe_alert(report, force=args.force)
        report["alert"] = result
    if args.json:
        print(_json.dumps(report, indent=2))
    else:
        icons = {"ok": "✅", "warn": "⚠️", "fail": "⛔", "skip": "·"}
        print(f"self-check: {report['verdict']} ({report['summary']})")
        for check in report["checks"]:
            print(f"  {icons.get(check['state'], '·')} {check['label']}: {check['detail']}")
            if check["state"] in {"warn", "fail"} and check["fix"]:
                print(f"      → {check['fix']}")
        if args.alert:
            alert = report.get("alert") or {}
            decision = alert.get("decision") or {}
            state = "sent" if alert.get("sent") else f"not sent ({decision.get('reason', 'n/a')})"
            print(f"ops alert: {state}" + (f" — {alert['error']}" if alert.get("error") else ""))
    return 0 if report["verdict"] == "ready" else 1


def _cmd_digest(args):
    """Print (and optionally send) the operational digest."""
    import json as _json

    from . import ops

    data = ops.digest(hours=args.hours)
    text = ops.render_digest(data)
    sent, error = False, ""
    if args.send:
        channel = str(config.OPS_TELEGRAM_CHANNEL or "").strip()
        if not channel:
            error = "OPS_TELEGRAM_CHANNEL is not configured"
        else:
            try:
                from . import telegram_ops

                asyncio.run(telegram_ops.post_to_channel(channel, text))
                sent = True
            except Exception as exc:  # pragma: no cover - network
                error = str(exc)[:200]
    # A daily card that goes to the ops channel is also the day's history row.
    snapshot = None
    if args.send and sent:
        try:
            snapshot = ops.record_snapshot(hours=args.hours, data=data)
        except Exception as exc:  # pragma: no cover - history is best-effort
            error = error or str(exc)[:200]
    if args.json:
        data = {**data, "sent": sent, "error": error, "snapshot": snapshot}
        print(_json.dumps(data, indent=2))
    else:
        print(text)
        if args.send:
            print(f"\nops send: {'sent' if sent else 'not sent'}" + (f" — {error}" if error else ""))
            if snapshot:
                print(f"history: day {snapshot['day']} recorded ({snapshot['attempts']} attempts)")
    return 0 if not error else 1


def _cmd_assignments(args):
    """The money map: two creators on one tag, one channel on two creators."""
    from . import guard

    findings = guard.scan()
    if args.json:
        print(json.dumps({
            "ok": guard.is_clear(findings),
            "counts": guard.counts(findings),
            "summary": guard.summary_line(findings),
            "findings": findings,
        }, indent=2))
        return 0 if guard.is_clear(findings) else 1
    print(guard.render_report(findings))
    if args.fix_hint:
        print("\nFix them on the dashboard (/setup → 🛡️ Money map) or with the "
              "inventory export: edit the amazon_tag column and paste it back "
              "(`import-inventory --file … --apply`).")
    return 0 if guard.is_clear(findings) else 1


def _cmd_tag_proof(args):
    """Whose Amazon tag signs which active channel — and what is held.

    Exit code 1 when any active channel would post an Amazon link under a tag
    that is not its creator's own: those deals are held by the pipeline, so the
    owner fixes the tag and they go out signed correctly.
    """
    from . import tag_proof

    if getattr(args, "creator", 0):
        profile = db.get_influencer(int(args.creator))
        if not profile:
            print(f"No creator with id {args.creator}.")
            return 2
        link = tag_proof.tagged_link(profile)
        proof = tag_proof.verify(link, profile) if link else {"ok": False, "issues": ["no tag saved"]}
        channels = []
        for channel in db.list_channels(int(profile["id"])):
            info = tag_proof.signing(profile, channel)
            channels.append({
                "channel_id": info["channel_id"],
                "identifier": str(channel.get("identifier") or ""),
                "tag": info["tag"],
                "own": info["own"],
                "source": info["source"],
                "shared_default": info["shared_default"],
                "status": str(channel.get("status") or ""),
            })
        payload = {
            "ok": bool(link) and proof["ok"],
            "creator_id": int(profile["id"]),
            "name": str(profile.get("name") or ""),
            "own_tag": tag_proof.own_tag(profile),
            "stored_tag": tag_proof.stored_tag(profile),
            "link": link,
            "link_summary": tag_proof.url_summary(link) if link else "",
            "hold_reason": tag_proof.HOLD_REASON,
            "channels": channels,
            "verify": proof,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            stored = payload["stored_tag"] or "(none)"
            print(f"{payload['name']} — own tag: {payload['own_tag'] or '(none)'} "
                  f"(stored: {stored})")
            print(f"  Amazon links will read: {payload['link_summary'] or '(nothing: tag missing)'}")
            for row in channels:
                mark = "✅ own" if row["own"] else "⛔ held"
                shared = " (the hub's shared id)" if row["shared_default"] else ""
                print(f"  {mark}  {row['identifier'] or row['channel_id']} "
                      f"[{row['source']}] tag={row['tag'] or '(none)'}{shared}")
            if not payload["ok"]:
                print("  Amazon deals for this creator are held, not signed with the "
                      "shared id:")
                for issue in (proof.get("issues") or ["paste their own Associate tag"])[:3]:
                    print(f"    · {issue}")
        return 0 if payload["ok"] else 1

    fleet = tag_proof.fleet(active_only=True)
    if args.json:
        print(json.dumps(fleet, indent=2))
        return 0 if fleet["ok"] else 1
    print(fleet["summary"])
    fix = tag_proof.describe_fix(fleet["blocked"])
    if fix:
        print(fix)
    if not fleet["blocked"]:
        print(f"Nothing to fix — {fleet['creators']} creator(s), {fleet['channels']} active "
              "channel(s) checked.")
        return 0
    print()
    for row in fleet["blocked"]:
        held = ", ".join(channel["identifier"] or f"#{channel['channel_id']}"
                         for channel in row["channels"])
        print(f"  ⛔ {row['name']} (#{row['creator_id']}) — {held}")
        print(f"     Amazon deals held: no own tag "
              f"(would post as {row['fallback_tag'] or '(none)'})")
    print("\nFix: paste each creator's own Associate tag (🧭 Guided setup or the creator "
          "page). Held deals post on the next worker run.")
    return 1


def _cmd_add_defaults(args):
    """Read or write what every NEW creator starts with."""
    from . import presets

    def _flag(value):
        if value is None:
            return None
        return str(value).strip().lower() in {"on", "1", "true", "yes"}

    updates: dict = {}
    for key in ("only_amazon", "amazon", "earnkaro", "hypd", "mirror_whatsapp"):
        setattr(args, key, _flag(getattr(args, key)))
    if args.only_amazon is not None:
        updates["only_amazon"] = args.only_amazon
    if args.amazon is not None:
        updates["allow_amazon"] = args.amazon
    if args.earnkaro is not None:
        updates["allow_earnkaro"] = args.earnkaro
    if args.hypd is not None:
        updates["allow_hypd"] = args.hypd
    if args.mirror_whatsapp is not None:
        updates["mirror_whatsapp"] = args.mirror_whatsapp
    for name, value in (("categories", args.categories), ("posting_schedule", args.hours),
                        ("price_filter", args.price), ("hypd_store_id", args.store)):
        if value is not None:
            updates[name] = value
    if updates and not args.dry_run:
        try:
            presets.save(updates, actor=args.actor or "cli")
        except Exception as exc:
            print(f"could not save the new-creator defaults: {exc}")
            return 1
    if args.json:
        print(json.dumps({"defaults": presets.resolved(),
                          "summary": presets.summary_line(),
                          "changed": sorted(updates)}, indent=2))
        return 0
    if updates and args.dry_run:
        print("dry run — nothing saved. Would set: " + ", ".join(sorted(updates)))
    print(presets.summary_line())
    for row in presets.describe():
        print(f"  {row['label']:24s} {row['shown']:12s} {row['hint']}")
    return 0


def _cmd_export_inventory(args):
    """Write the whole creator inventory (with dates and channels) to a file."""
    from . import inventory

    text = inventory.csv_text(member=args.member or "")
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        rows = len(text.strip().splitlines()) - 1
        print(f"wrote {rows} creator(s) to {args.out}")
        print("columns: " + ", ".join(inventory.COLUMNS))
    else:
        print(text, end="")
    return 0


def _cmd_import_inventory(args):
    """Preview an edited inventory file, or apply it with --apply."""
    from . import inventory

    try:
        text = Path(args.file).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"could not read {args.file}: {exc}")
        return 1
    try:
        plan = inventory.plan_import(text, create=args.create)
    except Exception as exc:
        print(f"that file could not be read: {exc}")
        return 1
    if args.json:
        payload = {key: plan[key] for key in
                   ("records", "changed", "created", "unchanged", "issues")}
        payload["edits"] = [{"line": op["line"], "name": op["name"], "create": op["create"],
                             "edits": inventory.edits(op)}
                            for op in plan["operations"]
                            if op["changes"] or op["channels"] or op["create"]]
        print(json.dumps(payload, indent=2))
    else:
        print(f"rows read: {plan['records']} · would change: {plan['changed']} "
              f"· unchanged: {plan['unchanged']} · would create: {plan['created']} "
              f"· cannot apply: {len(plan['issues'])}")
        for op in plan["operations"]:
            if op["changes"] or op["channels"] or op["create"]:
                print(f"  line {op['line']:>5} {op['name']}"
                      + (" [new]" if op["create"] else "") + ":")
                for edit in inventory.edits(op):
                    was = f"{edit['from']} → " if edit["from"] else ""
                    print(f"          {edit['field']}: {was}{edit['to']}")
        for issue in plan["issues"][:20]:
            print(f"  ! line {issue['line']} {issue['name']}: {issue['detail']}")
    if not args.apply:
        if not args.json:
            print("preview only — run again with --apply to write the changes.")
        return 0
    report = inventory.apply_import(plan, actor=args.actor or "admin")
    print("applied: " + inventory.render_report(report))
    for row in report["results"]:
        if row["status"] == "failed":
            print(f"  ⛔ {row['name']}: {row['detail']}")
    return 0 if not report["failed"] else 1


def _cmd_totp(args):
    """Create or verify the admin two-factor secret (RFC 6238 TOTP)."""
    import time as _time

    from . import config, security

    if args.uri_only:
        print(security.totp_uri(config.ADMIN_TOTP_SECRET, account=args.account))
        return 0
    if args.check:
        ok = security.verify_totp(args.check, secret=config.ADMIN_TOTP_SECRET
                                  or args.secret or "")
        if ok:
            print("✅ the code matches — two-factor sign-in will accept it right now.")
            return 0
        print("⛔ the code does not match (check the device clock; codes last 30 seconds).")
        return 1
    if args.new:
        secret = security.new_totp_secret()
        print("New two-factor secret (keep it private):")
        print(f"  ADMIN_TOTP_SECRET={secret}")
        print("")
        print("1. Add it to your authenticator app (manual entry, time-based, 6 digits):")
        print("   account: admin · key: " + secret)
        print("   or paste this URL in the app:")
        print("   " + security.totp_uri(secret))
        print("2. Put the line above in the server .env and restart the dashboard.")
        print("3. Test before you rely on it (a fresh code):")
        print("   PYTHONPATH=. .venv/bin/python -m influencer_hub.cli totp --check <code>")
        return 0
    if args.secret:
        print(f"current code for that secret: {security.totp_code(args.secret)}")
        print("(valid for this 30-second window)")
        return 0
    configured = bool(str(config.ADMIN_TOTP_SECRET or "").strip())
    print(f"two-factor sign-in: {'ON' if configured else 'OFF'}")
    if configured:
        print(f"current code: {security.totp_code(config.ADMIN_TOTP_SECRET)} "
              f"(for {_time.strftime('%H:%M:%S', _time.gmtime())} UTC)")
        print("otpauth URL: " + security.totp_uri(config.ADMIN_TOTP_SECRET, account=args.account))
    else:
        print("Switch it on with: PYTHONPATH=. .venv/bin/python -m influencer_hub.cli totp --new")
    return 0


def _cmd_snapshot(args):
    """Freeze today's numbers into the history (a nightly cron line)."""
    import json as _json

    from . import db, ops

    db.init()
    row = ops.record_snapshot(hours=args.hours, notes=args.note)
    trend = ops.trend(days=args.days)
    if args.json:
        print(_json.dumps({"snapshot": row, "trend": trend}, indent=2))
    else:
        print(f"history: {row['day']} · {row['posted']} posted · {row['failed']} failed "
              f"· {row['attempts']} attempts · verdict {row['verdict']}")
        if trend["captured_days"]:
            if trend["sparkline"]:
                line = f"{trend['days']}d: {trend['sparkline']}"
                if trend["green_streak"]:
                    line += f" · green {trend['green_streak']}d"
                elif trend["delta_pct"] is not None:
                    line += f" · {trend['delta_pct']:+d}% vs previous day"
            else:
                line = "no deliveries recorded yet"
            print(line + f"  ({trend['captured_days']} day(s) stored)")
    return 0


def _cmd_backup(args):
    """A consistent copy of the hub database (safe while the worker runs)."""
    import json as _json

    from . import ops

    try:
        result = ops.backup_database(args.out, keep=args.keep)
    except Exception as exc:
        print(f"backup failed: {exc}")
        return 1
    if args.json:
        print(_json.dumps(result, indent=2))
    else:
        counts = ", ".join(f"{name} {value}" for name, value in result["counts"].items())
        print(f"backup: {result['path']} ({result['size_bytes'] // 1024} KB)")
        if counts:
            print(f"contents: {counts}")
        print(f"sidecar: {result['sidecar']}")
        print(f"kept {len(result['kept'])} file(s)"
              + (f" · pruned {', '.join(result['pruned'])}" if result["pruned"] else ""))
        print("note: .env secrets are NOT in a backup — the copy is safe to move.")
    return 0


def _cmd_restore(args):
    """Put a backup back, keeping the current file aside as *.pre-restore-*."""
    import json as _json

    from . import ops

    check = ops.inspect_backup(args.file)
    if not check["ok"]:
        print(f"not a hub database: {check['reason']}")
        return 1
    if not args.yes and not args.dry_run:
        print(f"would restore {args.file} ({check.get('counts', {})}) over {config.DB_PATH}.")
        print("run again with --yes to do it (the current file is kept as *.pre-restore-*).")
        return 2
    try:
        result = ops.restore_database(args.file, dry_run=args.dry_run)
    except Exception as exc:
        print(f"restore failed: {exc}")
        return 1
    if args.json:
        print(_json.dumps({"check": check, "result": result}, indent=2))
    else:
        verb = "checked" if result["dry_run"] else "restored"
        print(f"{verb}: {result['restored_from']}")
        if result["previous_saved_as"]:
            lead = "previous file would be kept as" if result["dry_run"] else "previous file kept as"
            print(f"{lead}: {result['previous_saved_as']}")
        print(f"contents: {result['counts']}")
    return 0


def _cmd_amazon_scan(args):
    """What Amazon links a source carries, and whose tag each creator's post gets."""
    import json as _json

    from . import amazon_watch, db, link_router

    text = args.text or ""
    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"could not read {args.file}: {exc}")
            return 1
    payload: dict = {}

    if args.report:
        payload["report"] = amazon_watch.attribution_report(hours=args.hours,
                                                            limit=args.limit or 500)
    if text:
        payload["scan"] = amazon_watch.scan(text)
        if args.creator:
            inf = db.get_influencer(args.creator)
            if not inf:
                print(f"unknown creator id {args.creator}")
                return 1
            payload["plan"] = amazon_watch.plan(text, tag=inf.get("amazon_tag") or "", role=args.role)
            payload["plan"]["creator"] = inf.get("name")
        elif args.fleet or args.check:
            payload["fleet"] = amazon_watch.fleet_plan(
                text, db.list_influencers(), role=args.role, limit=args.limit or 1000,
            )
    if not payload:
        print("nothing to do: pass --text/--file (what the source carries), --report (what we sent), or both")
        return 2

    check_failures: list[str] = []
    if args.check:
        if "report" in payload and not payload["report"]["ok"]:
            check_failures.append("a post carried someone else's tag")
        if "fleet" in payload and not payload["fleet"]["all_own_tags"]:
            check_failures.append("a creator would post with a tag that is not their own")
        if "plan" in payload and not payload["plan"]["ok"]:
            check_failures.append("the rendered post has an Amazon link problem")
        payload["check"] = {
            "ok": not check_failures,
            "problems": check_failures,
            "message": ("check passed: every Amazon link is signed with the creator's own tag."
                        if not check_failures else "check failed: " + "; ".join(check_failures)),
        }

    if args.json:
        print(_json.dumps(payload, indent=2))
    else:
        if "scan" in payload:
            scan = payload["scan"]
            if not scan["has_amazon"]:
                print("Amazon links in this source post: none.")
            else:
                print(f"Amazon links in this source post: {len(scan['links'])} "
                      f"({', '.join(f'{count} {kind}' for kind, count in sorted(scan['counts'].items()))})")
                for link in scan["links"]:
                    whose = f"  [source tag: {link['source_tag']}]" if link["source_tag"] else ""
                    asin = f" ASIN {link['asin']}" if link["asin"] else ""
                    print(f"  • {link['kind']:7}{asin} → {link['action']}{whose}")
                    print(f"      {link['url']}")
                    print(f"      {link['action_text']}")
        if "plan" in payload:
            plan = payload["plan"]
            print(f"\n{plan['creator']} → tag {plan['effective_tag']}"
                  + ("" if plan.get("usable_tag") else " (fallback: their tag is not usable)"))
            print(plan["rendered"])
            if plan["issues"]:
                print("\nnotes:")
                for issue in plan["issues"]:
                    print(f"  ! {issue}")
        if "fleet" in payload:
            fleet = payload["fleet"]
            verdict = "every creator gets their OWN tag" if fleet["all_own_tags"] else "PROBLEMS"
            print(f"\nfleet: {fleet['creators']} creator(s), {fleet['own_tag']} with their own tag "
                  f"· {fleet['fallback_or_missing']} fallback/missing · {fleet['ms']}ms — {verdict}")
            for detail in fleet["details"]:
                print(f"  ! {detail['creator']} ({detail['tag'] or 'no tag'}) → "
                      f"effective {detail['effective_tag']}, tags found {detail['tags_found']}")
        if "report" in payload:
            report = payload["report"]
            print("\n" + amazon_watch.render_report(report))
            for problem in report["problems"]:
                print(f"  ⛔ {problem['creator']}: {problem['url']} carries "
                      f"{problem['found_tag']!r}, expected {problem['expected_tag']!r}")
            for example in report["examples"][:3]:
                print(f"  · {example['creator']}: {', '.join(example['links'])[:120]}")

    if args.check:
        if not args.json:
            print(payload["check"]["message"])
        return 0 if payload["check"]["ok"] else 1
    return 0


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

    aw = sub.add_parser(
        "amazon-scan",
        help="what Amazon links a source carries, whose tag each creator's post gets (read-only)",
    )
    aw.add_argument("--text", default="", help="the source post text")
    aw.add_argument("--file", default="", help="read the source post from a file instead")
    aw.add_argument("--creator", type=int, default=0, help="show the rendered post for one creator")
    aw.add_argument("--fleet", action="store_true", help="check every creator gets their own tag")
    aw.add_argument("--report", action="store_true",
                    help="audit the posts we really sent in the last --hours")
    aw.add_argument("--hours", type=int, default=24, help="window for --report (default 24)")
    aw.add_argument("--limit", type=int, default=0, help="cap creators/posts inspected")
    aw.add_argument("--role", default="broadcast", choices=["broadcast", "whatsapp", "approval"],
                    help="render as this kind of channel (default broadcast)")
    aw.add_argument("--check", action="store_true",
                    help="exit 1 when any Amazon link would go out without the creator's own tag")
    aw.add_argument("--json", action="store_true", help="machine-readable output")
    aw.set_defaults(func=_cmd_amazon_scan)

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
    d.add_argument(
        "--short-links",
        action="store_true",
        help="also validate the local branded HTTPS short-link origin (does not make an external request)",
    )
    d.set_defaults(func=_cmd_doctor)

    sub.add_parser("status", help="overview of everything").set_defaults(func=_cmd_status)

    sc = sub.add_parser(
        "self-check",
        help="check database, Telegram, worker, sources, accounts, creators and the mirror",
    )
    sc.add_argument("--live", action="store_true",
                    help="also ping the WhatsApp hub on the loopback port")
    sc.add_argument("--json", action="store_true", help="machine-readable output")
    sc.add_argument("--alert", action="store_true",
                    help="post to OPS_TELEGRAM_CHANNEL when the verdict got worse "
                         "(cooldown applies)")
    sc.add_argument("--force", action="store_true",
                    help="with --alert: send even if the verdict improved or is in cooldown")
    sc.set_defaults(func=_cmd_self_check)

    dg = sub.add_parser("digest", help="operational summary for the ops channel or a terminal")
    dg.add_argument("--hours", type=int, default=24, help="window in hours (default 24)")
    dg.add_argument("--send", action="store_true", help="post the card to OPS_TELEGRAM_CHANNEL")
    dg.add_argument("--json", action="store_true", help="machine-readable output")
    dg.set_defaults(func=_cmd_digest)

    asg = sub.add_parser("assignments",
                         help="whose Amazon tag / channel is whose (read-only, exit 1 on a mix)")
    asg.add_argument("--json", action="store_true", help="machine-readable findings")
    asg.add_argument("--fix-hint", action="store_true", help="print how to fix them")
    asg.set_defaults(func=_cmd_assignments)

    tpr = sub.add_parser(
        "tag-proof",
        help="prove every active channel posts Amazon links under its own tag (exit 1 when not)",
    )
    tpr.add_argument("--creator", type=int, default=0,
                     help="the single-creator proof: the exact link their posts will carry")
    tpr.add_argument("--json", action="store_true", help="machine-readable output")
    tpr.set_defaults(func=_cmd_tag_proof)

    ad = sub.add_parser("add-defaults",
                        help="what every NEW creator starts with (presets for the team)")
    ad.add_argument("--show", action="store_true", help="just print them (the default)")
    for name, flag, help_text in (("amazon", "--amazon", "Amazon links on/off"),
                                  ("earnkaro", "--earnkaro", "EarnKaro on/off"),
                                  ("hypd", "--hypd", "HYPD / Meesho on/off"),
                                  ("only_amazon", "--only-amazon", "Amazon-only channels on/off"),
                                  ("mirror_whatsapp", "--mirror-whatsapp",
                                   "new main channels mirror to WhatsApp")):
        ad.add_argument(flag, dest=name, choices=["on", "off"], default=None, help=help_text)
    ad.add_argument("--categories", default=None, help="default deal categories")
    ad.add_argument("--hours", default=None, help="default posting hours")
    ad.add_argument("--price", default=None, help="default price filter")
    ad.add_argument("--store", default=None, help="HYPD store id ('' = hub default)")
    ad.add_argument("--dry-run", action="store_true", help="show what would be saved")
    ad.add_argument("--actor", default="cli", help="name recorded in the audit log")
    ad.add_argument("--json", action="store_true", help="machine-readable output")
    ad.set_defaults(func=_cmd_add_defaults)

    ex = sub.add_parser("export-inventory",
                        help="write every creator (date added, channels, ids, settings) as CSV")
    ex.add_argument("--out", default="", help="file to write (default: print to stdout)")
    ex.add_argument("--member", default="", help="only the creators one team member added")
    ex.set_defaults(func=_cmd_export_inventory)

    im = sub.add_parser("import-inventory",
                        help="apply an edited inventory file (preview by default)")
    im.add_argument("--file", required=True, help="the edited CSV")
    im.add_argument("--apply", action="store_true", help="write the changes (default: preview)")
    im.add_argument("--create", action="store_true", help="also create rows not in the hub yet")
    im.add_argument("--actor", default="admin", help="name recorded in the audit log")
    im.add_argument("--json", action="store_true", help="machine-readable preview")
    im.set_defaults(func=_cmd_import_inventory)

    tp = sub.add_parser(
        "totp",
        help="create/verify the admin two-factor secret (authenticator app, 6 digits)",
    )
    tp.add_argument("--new", action="store_true", help="print a fresh secret + otpauth URL")
    tp.add_argument("--check", default="", help="verify a six-digit code against ADMIN_TOTP_SECRET")
    tp.add_argument("--secret", default="", help="work with this secret instead of the configured one")
    tp.add_argument("--uri", dest="uri_only", action="store_true",
                    help="print the otpauth URL for the configured secret")
    tp.add_argument("--account", default="admin", help="account label in the otpauth URL")
    tp.set_defaults(func=_cmd_totp)

    sn = sub.add_parser("snapshot", help="store today's numbers in the history (trends)")
    sn.add_argument("--hours", type=int, default=24, help="window to summarise (default 24)")
    sn.add_argument("--days", type=int, default=7, help="history to show back (default 7)")
    sn.add_argument("--note", default="", help="a short note to keep with the row")
    sn.add_argument("--json", action="store_true", help="machine-readable output")
    sn.set_defaults(func=_cmd_snapshot)

    bk = sub.add_parser("backup", help="copy the hub database (safe while the worker runs)")
    bk.add_argument("--out", default="backups", help="directory for the copies (default ./backups)")
    bk.add_argument("--keep", type=int, default=7, help="how many copies to keep (default 7)")
    bk.add_argument("--json", action="store_true", help="machine-readable output")
    bk.set_defaults(func=_cmd_backup)

    rs = sub.add_parser("restore", help="put a backup back (keeps the current file aside)")
    rs.add_argument("file", help="the backup file to restore")
    rs.add_argument("--yes", action="store_true", help="actually do it")
    rs.add_argument("--dry-run", action="store_true", help="only check the file")
    rs.add_argument("--json", action="store_true", help="machine-readable output")
    rs.set_defaults(func=_cmd_restore)

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

    dc = sub.add_parser(
        "deploy-check",
        help="read-only preflight for a deploy: checkout, deps, .env keys, DB, services",
    )
    dc.add_argument("--branch", default="", help="the branch the hub should run (default: its upstream)")
    dc.add_argument("--repo", default="", help="repository root (default: this checkout)")
    dc.add_argument("--backups", default="", help="backup directory to look at (default ./backups)")
    dc.add_argument("--plan", dest="show_plan", action="store_true",
                    help="also print the ordered deploy steps")
    dc.add_argument("--json", action="store_true", help="machine-readable output")
    dc.set_defaults(func=_cmd_deploy_check)

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
