# Influencer Hub — multi-tenant affiliate/loot deals system

> **Telugu (summary):** Bestgaa loot bot meeda okka **influencer layer** add
> chesaamu. Prathi influencer ki separate **Telegram channel** (mana bot account
> tho create cheyyam) + **WhatsApp** (vaala number QR tho pair — group feed +
> official Channel). Strategy: **Amazon links valladi tag**, **remaining anni
> mana EarnKaro** links. Okka shared deal pool, only Amazon tag differs per
> person. Web dashboard tho influencers add cheyyachu, QR chudachu, stats &
> VM health chudachu. Unlimited influencers support.

This repo extends your existing `bestgaa` loot-deals bot with an **influencer
scheme**: you recruit influencers, give each their own branded Telegram +
WhatsApp channels, and monetise deals with a split strategy:

| Link type | Commission goes to |
|-----------|--------------------|
| Amazon    | **the influencer** (their Amazon associate tag) |
| Everything else (Flipkart, Myntra, Ajio, …) | **you** (your EarnKaro publisher id) |

One shared deal pool feeds everyone; only the Amazon tag changes per influencer.

---

## The 3-Channel Per-Influencer Setup (Amazon Approval + Real Deals + WhatsApp)

Prathi kotha influencer vachinappudu manam **3 separate channels** auto ga set up chestam:

| Channel | Platform | Content & Link Strategy | Purpose |
|---|---|---|---|
| **Channel 1: Amazon Approval Channel** | Telegram | **Amazon-only native links** (`https://www.amazon.in/dp/...?tag=influencer_tag`) + mandatory `#ad (paid link)` disclosure. NO shorteners. Non-Amazon merchant links completely removed. | **Amazon Associate Account Review 100% Pass** avvadaniki (SmartBuy Hub template match). |
| **Channel 2: Real / Broadcast Channel** | Telegram | **Full Loot Deals**: Amazon links carry their tag, all other merchants (Flipkart, Myntra, Ajio...) get converted to **our EarnKaro short links** (Publisher `5478322`). | High-volume daily monetization channel. |
| **Channel 3: WhatsApp Feed** | WhatsApp | **Full Loot Deals**: WhatsApp group & official channel feed. Amazon = their tag, others = our EarnKaro. | High engagement WhatsApp community monetization. |

### How to onboard unlimited influencers (1-Click Easy Setup)
1. **Dashboard Self-Serve (/onboard):**
   Go to `/onboard`, enter Name + Amazon Tag (e.g. `mama086-21`). Click **"⚡ 1-Click Auto Setup (3 Channels)"**.
   Both Telegram channels (Approval + Broadcast) are generated with bots pre-configured, and the WhatsApp session QR code is generated.
2. **CLI Command:**
   ```bash
   python -m influencer_hub.cli onboard-tg <id>
   ```
   Auto-creates both Channel 1 (Approval) and Channel 2 (Broadcast) for that influencer.
3. **Check Demo Render:**
   ```bash
   python -m influencer_hub.cli render-demo <id>
   ```
   Renders sample deals across all 3 channels showing the exact format.

---

## Architecture

```
influencer_hub/   Python brain
  config.py         env config (no import-time crashes)
  db.py             SQLite: influencers, channels, wa_sessions, posts, vm_stats, sources
  link_router.py   CORE: Amazon->their tag, others->our EarnKaro (pure, tested)
  earnkaro.py      EarnKaro converter client (OUR publisher id)
  telegram_ops.py  create channel under OUR bot account + add posting bot as admin
  whatsapp_client.py  HTTP client for the wa_hub
  pipeline.py      shared deal -> render per influencer -> dispatch to channels
  vm_watch.py      host health (cpu/mem/disk/bot) -> DB + ops Telegram channel
  cli.py           management CLI

wa_hub/            Node/baileys multi-session WhatsApp service
  server.js        REST + SSE API (sessions, QR, send, group, newsletter)
  wa_manager.js    one baileys socket per influencer number, persistent auth

dashboard/         Flask web UI
  app.py           register influencers, show QR, per-influencer stats, VM health
  templates/ static/

tests/             pytest for link_router + db (no creds needed)
```

### Why this split?
- **link_router + db** are pure and unit-tested with zero credentials — the
  business logic ("Amazon = theirs, rest = ours") is provably correct.
- **Telegram** creation reuses your existing Telethon session, so new channels
  appear under the same account that already runs your 2 channels.
- **WhatsApp** lives in Node (baileys) — the exact stack your `tg-wa-bridge`
  already uses — because it must hold a live WA-Web socket per phone number.
  The Python side only speaks JSON to it.

---

## Quick start (no creds — validates the logic)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. HUB_DB_PATH=/tmp/hub.sqlite3 \
  python -m influencer_hub.cli init
PYTHONPATH=. HUB_DB_PATH=/tmp/hub.sqlite3 \
  python -m influencer_hub.cli add-influencer "Ravi" --tag ravi099-21 --handle @ravi --dummy
PYTHONPATH=. HUB_DB_PATH=/tmp/hub.sqlite3 \
  python -m influencer_hub.cli render-demo 1      # shows Amazon=their tag, others=EarnKaro
PYTHONPATH=. HUB_DB_PATH=/tmp/hub.sqlite3 python -m pytest tests/ -q
```

## Onboarding an influencer (production)

1. `add-influencer "Ravi" --tag ravi099-21` (their Amazon associate tag).
2. **Telegram:** `create-tg <id> --title "Ravi Loots"` — we create the channel
   under our bot account and add the posting bot as admin.
3. **WhatsApp:** `pair-wa <id>` starts a WA session; the dashboard shows the QR.
   The influencer scans it with **their** WhatsApp number. Then:
   - `create-group <id>` makes a group they own (our automated deal feed).
   - `create-newsletter <id>` creates their official WhatsApp Channel.
4. Deals from the shared pool are rendered per influencer and pushed to every
   ready channel. Dedup is per (influencer, channel, deal).

All of this is also doable from the **dashboard**: `cd dashboard && PYTHONPATH=.. python app.py`
(binds `0.0.0.0:5000`). Open it, add an influencer, click through the steps.

### One-shot & self-serve onboarding (scale to unlimited)
- **Self-serve page:** send influencers to `/onboard` — they enter their name +
  Amazon tag (+ optional WhatsApp number) and we create their channel(s) and
  show the QR. No operator CLI needed.
- **One command:** `python -m influencer_hub.cli onboard <id>` does Telegram +
  WhatsApp (per the influencer's enabled channels) in one shot.
- **Bulk:** `python -m influencer_hub.cli add-influencers --csv influencers.csv`
  (columns: `name,tag,handle,dummy,telegram,whatsapp`) imports hundreds at once.
- The DB is the source of truth, so there is **no cap** on influencer count.

### Telegram and WhatsApp are fully independent
Each influencer has `telegram_enabled` / `whatsapp_enabled` flags, so you can run
**TG-only**, **WA-only**, or both — and manage them separately:
- `onboard-tg <id>` / `onboard-wa <id>` (or the dashboard buttons) act on one
  channel without touching the other.
- `set-flags <id> --tg 1 --wa 0` toggles them anytime (dashboard: "Save channels").

### Verify EarnKaro converts (live)
The converter call is the exact contract your working `bestgaa` bot uses
(`POST …/api/converter/public`, `Authorization: Bearer <JWT>`, `{"deal": url}`).
To prove it on the VM (which has network), run:

```bash
python -m influencer_hub.cli verify-earnkaro --url "https://www.flipkart.com/p/itmEXAMPLE"
# -> {'ok': True, 'converted_link': 'https://ekaro.in/AbC123', 'http_status': 200, ...}
```

(This sandbox has no outbound network, so the call fails here with a connection
error — expected. On the VM it returns the short `ekaro.in` link.)

**✅ Verified live (2026-09-20):** publisher id `5478322` confirmed earning —
a real Flipkart link came back with `affExtParam2=5478322`, which equals the
JWT's `earnkaro` claim and `EARNKARO_PUBLISHER_ID`. The `affExtParam2=5478322`
provenance lock in `earnkaro.py` guarantees every converted link stays on this
account.

---

## Honest limitation — official WhatsApp Channels

baileys can **create** a WhatsApp Channel (newsletter) on the connected number
and the influencer can follow it, but reliable **automated posting** to a
newsletter is not exposed by this baileys build. So:

- The **WhatsApp group** is the reliable, fully-automated deal feed.
- The **official Channel** is created + followed; posting there is done from the
  phone (or we mirror the group). This is flagged in `wa_manager.js` and the UI.

If you later want fully automated Channel posting, the path is the Meta
Business/Cloud API for Channels, which is a separate integration.

## VM watch ("ma VM chusthundta bot")

`python -m influencer_hub.cli vm-watch --loop` samples CPU/mem/disk + whether
the bot process is alive, writes to `vm_stats`, and posts a compact card to
`OPS_TELEGRAM_CHANNEL`. The dashboard shows the latest snapshot at `/vm`.

## Dummy / separate sources

`use_dummy_sources` per influencer + the `sources` table (kind `production` |
`dummy`) let you stage a new influencer against isolated test sources before
pointing them at the live shared pool. Dummy sources are kept completely
separate from production.
