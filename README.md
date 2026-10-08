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
  by username/ID. A private invite hash is never resolved; its optional label
  only matches an exact normalized joined-dialog title. If a private label does
  not match, the worker logs a warning and falls back to eligible already-joined
  dialogs (output dialogs and unconfigured account-owned channels stay excluded).
  The live check in Setup Center shows this fallback; run
  `python -m influencer_hub.cli doctor --telegram-sources` on the VM for the same
  read-only selection audit. To keep ingestion narrow, add public usernames or
  use the exact title of the joined private dialog as its source label.
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

Set `DASHBOARD_ADMIN_PASSWORD` to the first value, `DASHBOARD_SECRET_KEY` to the second, `WA_HUB_TOKEN` to the third, plus `HUB_ENV=production`. The admin password must be at least 16 characters; the signing key must remain stable across restarts and workers. Leave `ADMIN_DELETE_PASSWORD` unset to reuse the dashboard password for destructive confirmations. Production mode enables Secure cookies on HTTPS requests (over plain HTTP the flag is dropped so sign-in still works), and the dashboard listens on `0.0.0.0:5000` so it is reachable on the VM IP — keep that port firewalled or fronted by HTTPS. The WhatsApp hub binds to loopback and refuses production startup without its private token. `/healthz` is the sole unauthenticated health endpoint besides validated first-party affiliate redirects. Do not deploy publicly with missing/weak dashboard credentials.

### Setup password: asked once, then remembered

Adding, saving and deleting channels (and every other sensitive setup change)
is password-protected, but the password is only asked **once at the start of a
work session**:

1. The first *Connect Channel* / *Save* / *Remove* opens one confirmation dialog.
2. After the correct password, setup stays **unlocked for 30 minutes of work**
   (`DASHBOARD_SETUP_UNLOCK_SECONDS`, default `1800`). Every later add, save and
   delete goes straight through — no second prompt.
3. The window is idle-based: each confirmed change renews it, so an active
   operator is never interrupted. It locks itself after 30 idle minutes, on
   **Sign out**, or when you press **🔓 Unlocked · Lock now** in the header.

If a form is submitted after the window expired, nothing is saved: the page
returns with your typed values restored and asks for the password once. Channel
removals keep a one-step **Undo** so a misclick is not permanent. The unlock is
also bound to the IP that confirmed it (`DASHBOARD_SETUP_UNLOCK_BIND_IP=0` to
disable behind a fixed-IP tunnel).

Adding a channel validates the destination before saving: a Telegram
`@username` (4-32 chars), a numeric channel id, or a private `t.me/+…` invite
link. A duplicate destination for the same creator is updated in place instead
of creating a second copy, and every result is reported on the page.

### Reaching the dashboard on `http://<vm-ip>:5000`

The systemd unit and `deploy/start_services.sh` bind Gunicorn to
`0.0.0.0:5000`, so the dashboard answers on the VM's IP. That port must be
opened twice — in the **OCI/VCN security list ingress rule** (source
`0.0.0.0/0`, TCP, destination port 5000) *and* in the VM firewall
(`sudo ufw allow 5000/tcp`) — and it must stay that way only if you accept
plain HTTP. Preferred alternatives: restrict the ingress rule to your own IP,
or terminate HTTPS in front of it and set `DASHBOARD_TRUST_PROXY=1`.

```bash
sudo ss -tlnp | grep 5000          # expect 0.0.0.0:5000 (not 127.0.0.1:5000)
curl -I http://127.0.0.1:5000      # expect 302 → /login?next=/
```

Two failure modes look identical in the browser and are worth checking first:

* **`POST /login` returns 400 in the logs** — the session cookie was rejected,
  usually because each Gunicorn worker was signing sessions with a different
  key. Set `DASHBOARD_SECRET_KEY` in `.env` (or leave it empty and let the app
  persist a generated key in `DASHBOARD_SECRET_KEY_FILE`, mode `0600`), then
  `sudo systemctl restart influencer-dashboard.service`.
* **Sign-in works but every page bounces back to `/login`** — you are on plain
  HTTP with `HUB_ENV=production`. The `Secure` cookie flag is now dropped
  automatically on non-HTTPS requests, but a browser that already stored a
  Secure cookie must be cleared once (or use HTTPS / `HUB_ENV=development` for
  LAN-only use).

### Private phone/laptop access with Tailscale (no purchased domain)

For a small, trusted set of devices, use Tailscale Serve rather than a public IP or Funnel:

1. Install Tailscale on the `influencers` VM and only the chosen phones/laptops. Use separate Tailscale identities; turn on **Device Approval** in the admin console and approve only these devices. If the tailnet contains other devices, add an access policy that denies them access to this server.
2. Enable MagicDNS and HTTPS certificates in Tailscale's DNS settings. Authenticate the VM, deploy the production dashboard so it answers on `127.0.0.1:5000` (`--bind 127.0.0.1:5000` for this private-only setup), and verify `/healthz` locally.
3. Run `sudo ./deploy/enable_tailscale_serve.sh`. It verifies the dashboard is healthy and loopback-only, checks that the WhatsApp hub is not listening on a public interface, then configures HTTPS Serve to proxy to the dashboard. It never enables Funnel or opens cloud firewall ports.
4. Open the printed `https://<vm>.<tailnet>.ts.net` URL only from an approved device with Tailscale connected. Verify access from one approved device and denial from an unapproved device. Keep OCI ingress for 5000/8088 closed; with this private setup, no paid domain or public web port is needed.

Tailscale Serve is tailnet-only; Funnel is public internet exposure. See [Tailscale Serve docs](https://tailscale.com/docs/reference/tailscale-cli/serve). To remove the HTTPS handler, run `sudo tailscale serve --https=443 off`.

### ⚡ Easy Setup — one screen, most easy

Open **⚡ Easy** in the header (or `/easy-setup`). One form sets up a creator end
to end, and the same rule the switches promise is the rule the pipeline follows:

**Amazon → only Amazon · EarnKaro → the other merchants · HYPD → Meesho.**

1. **Creator** — name + that creator's own Amazon Associate tag.
2. **Channels** — 🛡️ *approval* channel and 📢 *main* channel. Paste an existing
   `@username` or a `t.me/…` link. Nothing is created or joined here — the hub
   only posts to channels the connected account can already post in.
3. **Three switches** (all on by default, they can run together):
   * 📦 **Amazon** — Amazon links get that creator's tag. Nothing else touches them.
   * 💰 **EarnKaro** — Flipkart, Shopsy, Myntra, Ajio, Nykaa, Croma, TataCliq…
     become EarnKaro links (needs the EarnKaro key in **Vault & Sources**).
   * 🛍️ **HYPD (Meesho)** — HYPD affiliate links (`hypd.store/…/afflink/…`) are
     retagged to that creator's store. A raw `meesho.com` link cannot be minted
     into an affiliate link yet, so it is posted as-is.
   * 🔥 **Only Amazon (strict)** — post Amazon deals only; the other two are off.
4. **Save & start posting** — both channels are saved `ready`, the creator is
   activated, and the page shows exactly what each kind of link becomes.

The routing table is live: flip a switch and see the outcome before saving.
Approval channels always render Amazon-only native links with the
`#ad (paid link)` disclosure; main channels post every network you switched on.
Running Easy Setup again with an existing creator name **updates** that creator
instead of creating a duplicate. Deals keep coming from the shared source list —
if none are configured yet, Easy Setup offers the recommended loot catalog in one
click, and every creator you add starts receiving them automatically.

### 💰 Money Radar — stop posting deals that pay nothing

Routing a link correctly is not the same as earning on it. A post can carry a
link that pays zero: a raw `meesho.com` URL, an unconverted Flipkart link, or an
Amazon link tagged for somebody else. **💰 Money** (`/money`) reads the deals we
actually posted and reports, per creator and per reason, how many links carried
our attribution and how many leaked.

It counts **attribution, not rupees** — network approval, cookies and
cancellations are outside what any link inspection can promise, so the page
never shows an earnings figure.

| State | What it means |
| --- | --- |
| 🟢 Earning | Amazon with our tag · HYPD afflink on our store · EarnKaro link with our publisher · LehLah · our own `/amazon/` and `/m/` short links |
| 🔴 Leak | Amazon tagged for someone else (or untagged) · HYPD on another store · EarnKaro for another publisher · raw merchant with no EarnKaro conversion · raw Meesho |
| ⚪ Neutral | Informational links, and generic short links whose attribution sits behind the redirect |

Two switches turn the findings into money (both password-confirmed like every
other setup change):

* **Meesho → EarnKaro fallback** *(on by default)* — HYPD owns Meesho but cannot
  mint an affiliate link from a raw `meesho.com` product URL, so that deal posts
  for free. With this on, the raw URL goes to EarnKaro, which runs a Meesho
  programme, and the deal converts instead of leaking. Meesho still never goes
  through generic Bitly shortening, and the source URL is kept whenever EarnKaro
  returns nothing.
* **Only post deals that earn** *(off by default — your call)* — holds back a
  deal when none of its links would carry our attribution
  (`skipped:no_commission_link`). It trades volume for earnings, so it starts
  off and the Radar recommends it only once it has seen free posts.
* **Post unconverted links anyway** *(off by default)* — mirrored by
  `ALLOW_UNCONVERTED_POSTS=1`. See the retry queue below: when a merchant link
  did not convert, the deal is parked and retried, so this switch is only for an
  operator who prefers volume over commission during a converter outage.

#### Conversion guarantees, link shape by link shape

Every link kind a source deal can carry has exactly one defined outcome:

| Source link | What is posted |
| --- | --- |
| Amazon `/dp/<ASIN>`, `/gp/product/<ASIN>`, `?asin=<ASIN>`, search, storefront | One canonical `https://www.amazon.in/dp/<ASIN>` (or the same search/store page) carrying exactly **our** tag; every other `tag=` is removed |
| Amazon short (`amzn.to`, `amzn.in`, `amzn.eu`, `amzn.asia`, `a.co`), long link for the same product in the same post | Both collapse into that one canonical OUR link |
| Amazon short alone | Resolved over the network into `https://www.amazon.in/dp/<ASIN>?tag=<OURS>`; if the network cannot resolve it, the short is kept with our tag appended and the run logs `UNRESOLVED AMAZON SHORT` |
| Flipkart / Shopsy / Myntra / Ajio / Nykaa / Croma / TataCliq… | The EarnKaro short link (`ekaro.in`, `fktr.in`, `myntr.it`, …) carrying our publisher id, verified by following the redirect |
| Same merchants while EarnKaro is down | The deal is **parked** (see below) instead of posting a raw zero-commission URL |
| Raw Meesho | Sent to EarnKaro when the fallback is on; otherwise posted as-is, because HYPD cannot mint an affiliate link from a product URL (with a `ZERO-COMMISSION post` log line) |
| HYPD `hypd.store/<store>/afflink/<token>` | Retagged to our store id |
| LehLah Meesho (`mcn=LEHLAH`, `af_siteid=lehlah`, `pid=lehlah`) | Kept exactly as it is — it already pays us |
| Wrapper (`bit.ly`, `tinyurl.com`, `cutt.ly`, `dl.flipkart.com/…`, `fkrt.it/…`) | Resolved first: an Amazon destination becomes the canonical OUR link, a merchant destination is converted by EarnKaro; if the destination cannot be resolved the deal is parked and the run logs `UNRESOLVED WRAPPED LINK` |
| Link written without `http://` (`flipkart.com/…`, `amazon.in/dp/…`, `amzn.to/…`) | Promoted to a real URL before anything else, then handled like every other link above (retagged/converted) |
| Anything else (news, YouTube, blog) | Untouched |

#### Add a channel → it posts with your tag

Save a Telegram/WhatsApp destination and the hub uses the tag you gave, with no
extra steps:

| Where | What is saved | What is posted |
| --- | --- | --- |
| Easy Setup screen (`name` + your tag + channel) | creator tag + both channels (`status=ready`), every network you left switched on | the confirmation shows the exact link: `Amazon deals will post as https://www.amazon.in/dp/…?tag=<YOUR TAG> (verified ✅)` |
| Creator page → Connect Channel (`@username` only) | channel inherits the creator's networks instead of switching them off | deals start flowing on the next worker poll — no restart |
| A per-channel tag in `Alt Tag` | that channel's tag wins over the creator's | links on that channel are signed with the channel tag |

A tag the Associates format cannot accept (`[A-Za-z0-9_-]{3,30}`, e.g. `ravi-21`)
is **refused at save time** with the reason instead of being silently replaced by
the fallback; an unusual-but-usable tag is accepted with a warning, and an empty
tag is reported. The pipeline logs `AMAZON TAG UNUSABLE` once per channel if it
ever has to fall back.

#### Check a post before it goes out

```bash
python3 -m influencer_hub.cli audit-links --text "🔥 deal … bit.ly/abc"
python3 -m influencer_hub.cli audit-links --file /tmp/post.txt --json
python3 -m influencer_hub.cli audit-links --offline --text "…"   # no network calls
```

Read-only: prints every link, its kind, where a wrapper really points, the text
that would be published and the verdict — exit code `0` when at least one of OUR
links is present, `1` when the worker would hold the deal instead of posting it.

#### The retry queue — no free posts, no lost deals

When the only link in a deal is a merchant URL EarnKaro should have converted
but did not (key missing/expired, API outage, transient error), posting now
would earn nothing and posting later would be too late. The same applies to a
wrapper link (`bit.ly`, `fkrt.it`, …) whose destination could not be resolved —
until it is, nobody can say whether the link pays us. Instead the deal is
parked in `deferred_deals` and the worker retries it every
`DEFERRED_RETRY_DELAY_SECONDS` (5 min) up to `DEFERRED_MAX_ATTEMPTS` (6) times —
the source cursor has already moved on, so this queue is the only way the deal
can still go out. As soon as EarnKaro converts, the deal is posted with OUR
link. If the outage outlasts the retry budget the deal is posted anyway and the
log says why, so a converter outage delays a deal instead of dropping it.

`/api/flow` reports the queue as `deferred: {waiting, retry_seconds,
allow_unconverted_posts}` and adds a note to the flow board, and the Money page
carries the `Post unconverted links anyway` switch. Two link shapes stay
zero-commission by design and are logged rather than delayed: a raw Meesho URL
(nothing can mint it) and a store EarnKaro does not cover.

Per-creator and per-channel **⭐ Minimum Deal Quality** (S/A/B/C, resolved
channel → creator → global) keeps a channel's attention for the deals worth a
click. The dashboard home page shows the same 7-day coverage number with a link
into the full report.

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

All of this is also doable from the **dashboard**. For local development only, run `cd dashboard && PYTHONPATH=.. python app.py` (binds `0.0.0.0:5000`; do not expose this development server publicly). Production uses Gunicorn under systemd on `0.0.0.0:5000`; keep the port restricted to your own IP, or put HTTPS in front of it.

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
