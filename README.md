# Influencer Hub — multi-tenant affiliate/loot deals system

> **Telugu (summary):** Bestgaa loot bot meeda okka **influencer layer** add
> chesaamu. Prathi influencer ki separate **Telegram channel** (mana bot account
> tho create cheyyam) + **WhatsApp** (vaala number QR tho pair — group feed +
> official Channel). Strategy: Amazon links use each influencer's saved tag;
> other merchant links follow that profile's enabled, configured affiliate
> networks. One shared deal pool serves all profiles. Dashboard features include
> influencer setup, QR display, stats, and VM health. Commission attribution is
> controlled by the affiliate networks and is not guaranteed by this app.

This repo extends your existing `bestgaa` loot-deals bot with an **influencer
scheme**: you recruit influencers, configure per-profile Telegram and WhatsApp
destinations, and apply each profile's enabled affiliate-routing settings:

| Link type | Routing behavior |
|-----------|------------------|
| Amazon ON | Amazon links are posted with the influencer's saved tag or channel override. |
| EarnKaro ON | Supported non-Amazon merchant URLs other than Meesho are sent to the configured EarnKaro account when credentials and merchant support are available. Amazon is routed separately through its Associates tag. |
| HYPD ON | HYPD is reserved for Meesho. Existing valid HYPD affiliate links can be retagged to the configured store ID; raw Meesho URLs stay unchanged until HYPD's official generator is configured. |
| Only Amazon | Explicit exclusive mode: suppress non-Amazon affiliate categories, regardless of the other toggles. |

Amazon, EarnKaro, and HYPD switches are independent, and selected networks can route together: Amazon uses its Associates tag, EarnKaro handles supported merchants except Meesho, and HYPD is reserved for Meesho. Profile and channel settings combine; `only_amazon` is the exclusive override. The override does not clear saved switches, so turning it off restores their selected behavior. These controls determine routing, not external approval, successful conversion, or commission attribution.

---

## Per-Influencer Destinations (Approval Preview + Broadcast + WhatsApp)

Each profile can use an Amazon-only Telegram preview, a broadcast Telegram channel, and WhatsApp destinations. Onboarding attempts to create the two Telegram channels and starts a WhatsApp QR session when those platforms are enabled; pair WhatsApp, then connect or create its group/channel from the profile page.

| Channel | Platform | Content & Link Strategy | Purpose |
|---|---|---|---|
| **Channel 1: Amazon Approval Channel** | Telegram | Amazon URLs use the influencer's saved tag and stay native (not shortened), with `#ad (paid link)` disclosure. Non-Amazon affiliate links are removed. | Approval-oriented preview only; Amazon review outcomes are not guaranteed. |
| **Channel 2: Real / Broadcast Channel** | Telegram | Amazon, EarnKaro, and HYPD links follow their independent selected toggles; the explicit Only Amazon mode suppresses non-Amazon affiliate links. | Per-profile and per-channel routing controls. |
| **Channel 3: WhatsApp Feed** | WhatsApp | Same independent network routing as its profile/channel settings; the explicit Only Amazon mode remains exclusive. | Per-profile and per-channel routing controls. |

### Native engagement polls (manual + no-repeat)
The influencer detail page includes an explicit poll composer for native Telegram
polls and WhatsApp **groups**. Questions and answer options are validated,
Telegram/WhatsApp supports single- or multiple-answer voting, and selected
ready destinations are sent once. WhatsApp newsletters/Channels are excluded:
poll delivery is not verified in the pinned unofficial Baileys integration.

Poll questions are recorded in a global SQLite history. Case, punctuation, and
spacing variants—and high-confidence near-duplicate long questions—are blocked
across profiles and platforms, even if a profile or channel is later removed.
The dashboard queues sends durably and the background worker drains one poll
destination per cycle, so conservative WhatsApp pacing cannot hold an admin web
request open. Destinations are reserved before network I/O; failures or uncertain
sends are recorded and are not blindly retried because a timeout may occur after
a platform accepted the poll. Poll creation is manual and requires fresh
password confirmation; no AI prompt generator or automatic poll
schedule runs in the background.

### How to onboard unlimited influencers (1-Click Easy Setup)
1. **Dashboard Self-Serve (/onboard):**
   Go to `/onboard`, enter the influencer's own Amazon Associate tag (the configured `mama086-21` value is only a fallback). Choose the supported affiliate-network and platform toggles, then click **"⚡ Save & Create Selected Destinations"**. When Telegram is enabled, the app attempts to create the Approval + Broadcast channels; when WhatsApp is enabled, it starts a QR session. Check the result messages, pair WhatsApp, and connect or create a group/channel afterward.
2. **CLI Command:**
   ```bash
   python -m influencer_hub.cli onboard-tg <id>
   ```
   Auto-creates both Channel 1 (Approval) and Channel 2 (Broadcast) for that influencer.
3. **Check Demo Render:**
   ```bash
   python -m influencer_hub.cli render-demo <id>
   ```
   Prints a sample render using the influencer's saved routing settings; it does not dispatch a real post.

---

## Architecture

```
influencer_hub/   Python brain
  config.py          env config and safe production defaults
  db.py              SQLite: profiles, channels, posts, sources, durable cursors and shared WhatsApp pacing
  link_router.py     Amazon tag handling, configured affiliate routing and deal filters
  amazon_creators.py official Amazon Creators API OAuth/catalog client
  earnkaro.py        EarnKaro converter client with configured publisher checks
  telegram_ops.py    Telethon operations with isolated per-process session copies
  puller.py          ingestion from already joined Telegram dialogs (no invite checks)
  whatsapp_client.py HTTP client for the wa_hub
  pipeline.py        shared deal -> render per influencer -> dispatch to channels
  polls.py           manual Telegram/WhatsApp-group polls with global deduplication
  worker.py          24/7 pull/dispatch daemon with cursoring and recovery
  vm_watch.py        host health (cpu/mem/disk/bot) -> DB + ops Telegram channel
  cli.py             management CLI

wa_hub/            Node/baileys multi-session WhatsApp service
  server.js        REST + SSE API (sessions, QR, send, group, newsletter)
  wa_manager.js    one baileys socket per influencer number, persistent auth

dashboard/         Flask web UI
  app.py           register influencers, show QR, per-influencer stats, VM health
  templates/ static/

tests/             pytest for link_router + db (no creds needed)
```

### Why this split?
- **link_router + db** are exercised by unit tests with zero credentials —
  tag placement, supported URL rewrites, and filter behavior can be checked
  locally; external approval and commission attribution remain outside the app.
- **Telegram** uses the existing authorized account. The base Telethon session
  is copied with SQLite's online-backup API into a private process-local session
  file; Flask, the worker, and CLI therefore never contend for one `.session`
  database. Dashboard requests also share one stable asyncio loop.
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
  python -m influencer_hub.cli render-demo 1      # shows routing for this profile's saved settings
PYTHONPATH=. HUB_DB_PATH=/tmp/hub.sqlite3 python -m pytest tests/ -q
```

## Production integrations and defaults

- New influencer records and omitted Amazon tags use `mama086-21` only as a
  configured fallback; supply each influencer's own Associates tag whenever
  available. Explicit per-influencer/channel tags are supported. Amazon.in
  product ASIN links are cleaned to `www.amazon.in/dp/<ASIN>`; Amazon.com ASINs
  stay on Amazon.com, and non-product routes keep their original marketplace,
  path, and required query parameters. The effective `tag` replaces the source
  tag; safe numeric product selectors (`th`, `psc`) are preserved. An
  optional first-party short route (`/amazon/<code>?tag=<effective-tag>`) is
  available without an external API key when `AMAZON_SHORT_LINK_BASE_URL` points
  to an operator-owned HTTPS hostname routed to the dashboard. It keeps the tag
  visible, validates it before redirecting, and falls back to the canonical URL
  when no hostname is configured. Approval-role posts stay on native Amazon URLs.
  Amazon-issued `amzn.to` codes must come from Amazon Associates/SiteStripe; the
  app does not invent them.
- HYPD is reserved for Meesho; raw Meesho URLs are never sent through EarnKaro.
  Existing HYPD affiliate URLs are first rewritten to the effective HYPD store
  ID (default `93944`). Raw Meesho URLs remain unchanged until HYPD's official
  generator contract is configured. Optionally, valid HYPD `/afflink/<token>` URLs can be
  wrapped as stable first-party redirects at `/m/<code>` when
  `MEESHO_SHORT_LINK_BASE_URL` points to an operator-owned HTTPS origin routed
  to Flask (for example, a subdomain under your own brand domain). Each code is
  stored persistently and the redirect only accepts a stored `https://hypd.store`
  affiliate target; generic Bitly does not replace these links. With the setting
  blank, the original HYPD affiliate URL remains. This shortens an existing
  HYPD affiliate link; it does not generate HYPD tokens from a raw Meesho URL.
- Meesho links carrying LehLah/AppsFlyer attribution (`pid` containing `lehlah`,
  `af_siteid=lehlah`, or `mcn=LEHLAH`) are detected separately from raw Meesho
  merchant links. Their complete original URL is preserved and they are not sent
  through EarnKaro/HYPD/Bitly. Short-link wrapping is disabled by default and is
  only enabled with `LEHLAH_SHORTLINKS_ENABLED=true` after account approval, plus
  an operator-owned `MEESHO_SHORT_LINK_BASE_URL`.
- Generic Bitly shortening requires a valid API token, with priority channel
  token -> influencer token -> global `BITLY_API_KEY` only for influencer IDs in
  `BITLY_GLOBAL_INFLUENCER_IDS`. The allowlist is empty by default, so the global
  key is never shared with other profiles. Amazon, HYPD, and LehLah-attributed
  links bypass generic Bitly. Raw Meesho links also stay out of generic Bitly.
  Without an applicable token, generic links stay
  unchanged.
- Telegram API ID defaults to `33595682`. Keep `TELEGRAM_API_HASH` in the VM's
  private `.env`/secret store; it is intentionally not embedded in Git. The
  session at `TELEGRAM_SESSION` is used as the seed for isolated process copies.
- The official Amazon Creators API client defaults to application
  `SMART_BUY` (`amzn1.application-oa2-client.83229d9d14664351be9fc2059038a4f2`)
  and India marketplace `www.amazon.in`. Set the credential secret in
  `AMAZON_CREATORS_API_CLIENT_SECRET` and set the exact credential version
  shown in Associates Central (the checked-in `3.2` is a configurable default). Try a
  catalog lookup with `python -m influencer_hub.cli amazon-items B0...`.
  Catalog API access is optional and not part of the deal-dispatch hot path.
- `puller.py` calls `client.iter_dialogs()` and reads only joined groups/channels;
  it never sends `CheckChatInviteRequest` or joins invite URLs. Use
  `TELEGRAM_OUTPUT_CHANNELS` and registered Telegram destination channels to
  keep output/ops dialogs out of source ingestion. Public source selectors match
  by username; private invite hashes are never resolved.
- `deploy/start_services.sh` starts supervised `deal-worker`, WhatsApp hub,
  dashboard, and VM watcher processes. Each service writes to `logs/` and is
  restarted by its launcher if it exits; `worker.py` additionally retries
  transient pull/delivery failures and persists per-dialog Telegram message
  cursors. Use `deploy/stop_services.sh` to stop that service set. For systemd,
  run `sudo ./deploy/install_systemd.sh` from the checkout to render units for
  that path and the invoking non-root account, then enable only the services
  you have configured. Do not run the shell supervisor and systemd unit for the
  same service at the same time (especially the deal worker). Keep the dashboard
  and worker on the same writable SQLite database: WhatsApp send slots are
  atomically shared there and survive restarts. This coordinates this app's
  send paths only; it does not guarantee delivery or account safety. The Setup
  Center's Launch readiness panel checks worker heartbeat and offers bounded,
  read-only Telegram/source-selector and WhatsApp-hub diagnostics; it does not
  join sources or send messages. Channel permissions still require explicit
  per-destination test posts.

## Secure dashboard deployment

The dashboard now requires an admin password before exposing management pages or APIs; it also applies CSRF checks, rate-limits sign-in attempts, uses `HttpOnly`/`SameSite=Lax` session cookies, and adds security headers. Production service definitions run the Flask app through Gunicorn; `python dashboard/app.py` is for local development only. Configure these values in the private `.env` before starting it:

Generate three independent values and paste them into the private `.env` file (the `.env` loader does not evaluate shell substitutions):

```bash
python -c 'import secrets; print(secrets.token_urlsafe(24))'  # admin password; keep it private
python -c 'import secrets; print(secrets.token_urlsafe(48))'  # Flask session signing key
python -c 'import secrets; print(secrets.token_urlsafe(48))'  # WA_HUB_TOKEN; keep it private
```

Set `DASHBOARD_ADMIN_PASSWORD` to the first value, `DASHBOARD_SECRET_KEY` to the second, `WA_HUB_TOKEN` to the third, plus `HUB_ENV=production`. The admin password must be at least 16 characters; the signing key must remain stable across restarts and workers. Leave `ADMIN_DELETE_PASSWORD` unset to reuse the dashboard password for destructive confirmations. Production mode enables Secure cookies. The dashboard and WhatsApp hub bind to loopback; the hub refuses production startup without its private token. Never expose ports 5000 or 8088 directly. `/healthz` is the sole unauthenticated health endpoint besides validated first-party affiliate redirects. Do not deploy publicly with missing/weak dashboard credentials.

### Private phone/laptop access with Tailscale (no purchased domain)

For a small, trusted set of devices, use Tailscale Serve rather than a public IP or Funnel:

1. Install Tailscale on the `influencers` VM and only the chosen phones/laptops. Use separate Tailscale identities; turn on **Device Approval** in the admin console and approve only these devices. If the tailnet contains other devices, add an access policy that denies them access to this server.
2. Enable MagicDNS and HTTPS certificates in Tailscale's DNS settings. Authenticate the VM, deploy the production dashboard so it answers on `127.0.0.1:5000`, and verify `/healthz` locally.
3. Run `sudo ./deploy/enable_tailscale_serve.sh`. It verifies the dashboard is healthy and loopback-only, checks that the WhatsApp hub is not listening on a public interface, then configures HTTPS Serve to proxy to the dashboard. It never enables Funnel or opens cloud firewall ports.
4. Open the printed `https://<vm>.<tailnet>.ts.net` URL only from an approved device with Tailscale connected. Verify access from one approved device and denial from an unapproved device. Keep OCI ingress for 5000/8088 closed; with this private setup, no paid domain or public web port is needed.

Tailscale Serve is tailnet-only; Funnel is public internet exposure. See [Tailscale Serve docs](https://tailscale.com/docs/reference/tailscale-cli/serve). To remove the HTTPS handler, run `sudo tailscale serve --https=443 off`.

## Onboarding an influencer (production)

1. `add-influencer "Ravi" --tag <creator-tag>` to save the influencer's own Associate tag. Omitting `--tag` uses configured `mama086-21` only as a fallback.
2. **Telegram:** `create-tg <id> --title "Ravi Loots"` uses the configured
   authorized Telegram account to create the channel. An optional bot-admin
   grant is best-effort; the authorized account itself publishes posts.
3. **WhatsApp:** `pair-wa <id>` starts a per-influencer QR session; the creator
   scans it with their own WhatsApp account. Add their Channel invite URL from
   the dashboard. It resolves to a newsletter JID and remains Pending until an
   explicit test post succeeds. The connected number must be an admin with
   posting rights. WhatsApp groups likewise require membership before testing.
4. Deals from the shared pool are rendered per influencer and pushed to ready
   channels. Amazon links use that creator's saved tag; dedup is per
   (influencer, channel, deal).

All of this is also doable from the **dashboard**. For local development only, run `cd dashboard && PYTHONPATH=.. python app.py` (binds `0.0.0.0:5000`; do not expose this development server publicly). Production uses Gunicorn under systemd, bound to loopback behind HTTPS.

### One-shot & self-serve onboarding
- **Self-serve page:** `/onboard` saves the profile, Amazon tag, and selected
  affiliate-network/platform flags. When enabled, it attempts to create the two
  Telegram channels and starts WhatsApp QR pairing; after pairing, connect or
  create the WhatsApp group/channel from the profile page.
- **One command:** `python -m influencer_hub.cli onboard <id>` performs the
  platform setup available for that influencer's enabled channels; QR pairing
  and WhatsApp destination setup can still require follow-up.
- **Bulk:** the dashboard CSV importer supports routing fields including
  `tag`/`amazon_tag`, `allow_amazon`, `allow_earnkaro`, `allow_hypd`,
  `only_amazon`, `strip_amazon`, and `hypd_store_id`, alongside profile and
  destination columns. The CLI `add-influencers --csv` command supports its
  documented basic profile/platform columns; use the dashboard importer for
  affiliate-network settings.

### Telegram and WhatsApp are fully independent
Each influencer has `telegram_enabled` / `whatsapp_enabled` flags, so you can run
**TG-only**, **WA-only**, or both — and manage them separately:
- `onboard-tg <id>` / `onboard-wa <id>` (or the dashboard buttons) act on one
  channel without touching the other.
- `set-flags <id> --tg 1 --wa 0` toggles them anytime (dashboard: "Save channels").

### Test EarnKaro conversion (live)
The client is configured to POST a cleaned merchant URL to the EarnKaro
converter endpoint with `Authorization: Bearer <credential>` and
`{"deal": url}`. Use a real product URL from a merchant supported by your
account when testing:

```bash
python -m influencer_hub.cli verify-earnkaro --url "<real supported merchant product URL>"
```

This command makes a live request. `ok: true` means it received a parseable
link and verified the configured publisher ID (for a short link, the redirect
must resolve to that ID). This checks link provenance for the test URL, not a
sale or commission attribution. The current automated tests use mocked
responses, and no production credential, source URL, or live conversion was
tested in this workspace. Normal dispatch also checks direct-link publisher
provenance; short-link checks there are best-effort if redirect resolution
fails.

---

## WhatsApp Channel posting: supported path and limits

The pinned Baileys release has a text-message route for WhatsApp newsletter
JIDs. The dashboard can resolve a `https://whatsapp.com/channel/...` invite to
its real `@newsletter` JID, save it as **Pending**, and only activate that
destination after an operator sends a visible test post successfully. The
connected influencer account must be a Channel admin with posting rights. A
WhatsApp group invite is different: resolving its JID does not silently join
the group; the paired account must already be a member with posting access.

This is **not an official Meta Channel API**: Meta's Cloud API documentation
lists Channels as unsupported ([Meta onboarding feature comparison](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/)). Baileys uses the WhatsApp-Web protocol; behavior can change and account restrictions cannot be ruled out. The pipeline sends text and links to Channels; newsletter media delivery is not guaranteed (see the [Baileys newsletter text/media report](https://github.com/WhiskeySockets/Baileys/issues/2345)). QR pairing is implemented; a phone-number pairing-code flow is not currently included. Test with a low-risk creator/channel before enabling automatic posts.

## VM watch ("ma VM chusthundta bot")

`python -m influencer_hub.cli vm-watch --loop` samples CPU/mem/disk + whether
the bot process is alive, writes to `vm_stats`, and posts a compact card to
`OPS_TELEGRAM_CHANNEL`. The dashboard shows the latest snapshot at `/vm`.

## Dummy / separate sources

`use_dummy_sources` per influencer + the `sources` table (kind `production` |
`dummy`) let you stage a new influencer against isolated test sources before
pointing them at the live shared pool. Dummy sources are kept completely
separate from production.
