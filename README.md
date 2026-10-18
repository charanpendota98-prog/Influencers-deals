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

### Main channel → WhatsApp mirror ("main lo vachinavi WhatsApp lo raval")

A deal that really went out on a creator's ready **main Telegram channel** is
also delivered to that creator's ready **WhatsApp destinations** (Channel or
group). The mirror is one click in Easy Setup and one switch in the Setup
Center (global) or per main channel (`Mirror: inherit / ON / OFF`).

The copy is not a second-class renderer. It runs the **same** delivery path as
a direct post — the same WhatsApp rendering, the creator's **own** Amazon tag
(channel override → creator tag → configured fallback, never another creator's
tag), the same earned-link checks (`commission guard`), the same dedup layers
(signature, physical identifier, 24h content hash, 12h near-duplicate), the
same 20/hour + 5/10min rate limits and the same random 60–80s WhatsApp safety pacing (`WA_SEND_GAP_MIN`/`WA_SEND_GAP_MAX`).
A mirrored post is stored with its source channel, so
`pipeline.mirror_plan()` (and the creator page) can state exactly which main
channel feeds which WhatsApp destination and how many posts were mirrored in
the last 24h.

The only thing the mirror does not re-apply is the *display* filtering the main
channel already decided — source label, posting window, category, price and
quality tier. The WhatsApp destination keeps its **own affiliate policy**: an
Amazon-only destination still refuses a non-Amazon deal instead of publishing a
link that earns nothing, and `strip_amazon` is respected. Mirroring never
happens from a channel that did not actually post the deal, never double-posts
a deal a destination already received, and is off entirely until that creator
has a **ready** WhatsApp destination (paired phone + one successful test post).

### Creator-scoped smart channel navigation

On a creator's profile, enable **Smart cross-channel buttons (this creator only)** to add a Telegram inline-button menu under posts. The buttons point to that creator's *other* ready Telegram destinations; the channel currently receiving the post is omitted. WhatsApp destinations get the same links as a readable footer. Public `@handles` and saved `t.me` invite links are supported, unlinked/private destinations are safely omitted, and the menu is capped at eight links. The feature is opt-in per creator, does not mix links across creators, and leaves the existing manual/global CTA behavior unchanged when switched off.

A creator or individual destination can also enable and configure **up to two manual post buttons** (custom text + destination). Telegram renders the two buttons side by side; WhatsApp receives the same destinations as a readable text footer because the current WhatsApp integration does not provide equivalent inline URL buttons. Channel-level CTAs take precedence over creator-level CTAs, then the legacy global CTA. Custom button targets must be HTTPS URLs or Telegram `@handles`; unsafe schemes are rejected. Smart navigation uses dynamically discovered creator-owned channels instead of the manual CTA at that selected settings level.

Affiliate routing remains separate from CTA buttons: Amazon product links use the creator's Associates tag (or a saved channel override); supported merchant URLs are converted through the configured EarnKaro publisher account when that route is enabled; existing HYPD links use the configured HYPD store. `Only Amazon` is exclusive and suppresses the other affiliate routes. Raw Meesho conversion is provider-dependent: the configured fallback may use EarnKaro when enabled and credentials are valid; an HYPD toggle alone cannot manufacture an HYPD affiliate link if the provider does not supply a raw-Meesho converter.

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

### Pairing a creator's phone: QR or login code

1. Open the creator page → **💬 Dedicated WhatsApp Account & Channel Linker**.
2. **QR path:** click *Generate Live QR Code* and scan it on the influencer's
   phone (WhatsApp → Linked devices).
3. **Code path (no camera):** type the full international number
   (`919876543210`, country code included) and click *🔢 Get Login Code*. The
   dashboard shows the live 8-character code; on the phone open
   *Linked devices → Link with phone number* and type it. Status and code are
   polled read-only — the browser never creates a session or sends a message.
4. **Pick the channel by name.** Once the phone is connected the card fetches
   the chats where that number is **admin or owner** and lists them under
   *⭐ Channels / Groups where +<number> is ADMIN* — name, kind and size
   (`Ravi Loot Deals · channel · owner · 4.2k`). Tick *Mirror main-channel
   posts here* if the WhatsApp destination should also receive everything the
   main Telegram channel posts. Pasting a
   `https://whatsapp.com/channel/…` link or a group invite still works, and is
   what you use for a destination the account is not an admin of yet; every
   other chat the account can see stays in the collapsed *see all chats* list.
5. Press **Test** once. Only a successful visible test activates the
   destination (the saved name is kept either way); after that, deals post
   there — including mirrored main-channel deals. The stored name is editable
   in the channel row on the creator page.
6. **Rehearse it first, without a phone.** `python3 tools/demo_wa_hub.py`
   answers the dashboard with a pretend paired number and three channels it is
   admin of, so the whole click-through (status → pick by name → test → a deal
   landing in it) can be exercised against a scratch database without logging
   in a real WhatsApp account. See `tools/DEMO_WHATSAPP.md`.

### 🅱️ Telegram gets the same smart handling

A Telegram destination is now treated like the WhatsApp one: you paste whatever
you were given (`@ravi_loots`, `https://t.me/ravi_loots`, `-1001234567890`) and
the hub **asks Telegram for the truth — read-only** (`get_entity` +
`get_permissions`; it never joins a group, never posts, never writes anything):

* the destination's **real name** is saved on the channel, so the creator page,
  the channel rows and the lists show `Ravi Loot Deals` instead of a JID;
* the hub also reports whether the account it would post with is **allowed to
  post there**. If not, the destination is still saved — an approval/preview
  channel where we only read is perfectly valid — with a warning that says
  exactly that instead of leaving you to discover it on delivery day;
* on a box with **no Telegram session yet**, nothing changes: what you pasted is
  saved exactly as pasted, and nothing waits on the network. The same is true if
  Telegram cannot be reached or the channel is private and unjoined.

This happens on the creator page, in the guided wizard's channel step and in the
one-screen **Add Influencer** form (where a channel we cannot post to is flashed as
a warning). Channels saved **before** this existed can be named in one read-only
pass:

```bash
python -m influencer_hub.cli channel-names            # what it would name, and who can post
python -m influencer_hub.cli channel-names --apply     # save the names it found
python -m influencer_hub.cli channel-names --id 62 --all --json
```

### ✅ Proving it works: the self-check panel

The dashboard's **🧪 Self-check** card (and the read-only
`GET /api/self-check?live=1`, per creator `?inf_id=<id>`) answers one question
plainly: *does everything really work right now?* Ten checks — database &
schema, Telegram session, dashboard access, worker heartbeat, deal sources,
Amazon attribution (including "nobody is still on the shared tag"), central
EarnKaro/HYPD accounts, creator readiness, the main → WhatsApp mirror, and a
live ping to the WhatsApp hub — each with a ✅ / ⚠️ / ⛔ state and, when
something is off, the exact command or screen that fixes it. It is read-only and
prints booleans, counts and short messages only: never a key, token or session
string. The panel re-runs itself every 30 seconds.

The same checks, in VM order, live in **[VERIFICATION.md](VERIFICATION.md)** —
the copy-paste runbook (blanked-credential test run, schema, stack, per-creator
proof, worker start, and what each red check means).

### 👥 Two sign-ins: the team adds, the owner edits

The hub has two doors, and the second one is deliberately narrow.

| | Admin (you) | Team member |
|---|---|---|
| Sign in at | `/login` — admin password **+ 6-digit code** | `/team/login` — their name + **team password** |
| Can do | everything: edit profiles, channels, money switches, vault, deletes | **add** creators and read the ones they added |
| Cannot | — | open `/`, `/setup`, `/money`, every admin API, edit, delete, change settings |

```bash
# .env on the server
DASHBOARD_TEAM_PASSWORD=<a long password you give only to the team>
ADMIN_TOTP_SECRET=<printed by the command below>      # optional but recommended
TEAM_ADDS_PER_HOUR=120                                 # per member; the admin is unlimited
```

```bash
# switch on two-factor for the admin sign-in (30 seconds, stdlib only)
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli totp --new    # prints secret + otpauth URL
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli totp --check 123456   # verify before you rely on it
```

What makes the boundary real rather than cosmetic:

* **Refused, not hidden.** The team's session is checked *before any view runs*:
  every endpoint outside the team page is refused — a bookmarked admin URL, a
  hand-crafted form post and an API call all fail. Attempts are recorded as
  `team_blocked_route`.
* **Add is add.** A name that already exists is reported back ("already in the
  hub — ask the owner"), never silently updated, so the team cannot edit through
  the add form.
* **Named, stamped, audited.** Team members type their own name at sign-in; every
  creator they add is stamped with it (`added_by`) and every add/change is written
  to the audit log with actor, role, action, target and IP. `/setup` has a
  **👥 Team access & audit** card showing the team link, who added what, an
  hourly-limit status and the last 40 log lines.
* **Hard limits.** Five wrong passwords block that IP for 15 minutes — counted in
  the database, so a restart is not a fresh budget. Each member may add
  `TEAM_ADDS_PER_HOUR` creators per hour; the admin is never throttled.
* **The team only ever sees its own work.** `/team` and `/api/team/creators` read
  `list_influencers(added_by=…)`, so nothing the owner added is exposed there.

### 🅰️ Amazon deals from our sources: detected, re-signed, posted

When a source post carries an Amazon link, the hub finds it, works out which
Amazon page it really is, and re-signs it with **that creator's own Associates
tag** before the post goes to their channel. Nothing else is invented: a link
the hub cannot vouch for is reported, never faked.

| What the source wrote | What the creator's channel receives |
|---|---|
| `https://www.amazon.in/dp/B09N3ZNHTY/ref=sr_1_3?tag=someoneelse-21&th=1` | `https://www.amazon.in/dp/B09N3ZNHTY?th=1&tag=ravi-21` |
| `amazon.in/dp/B0BDHWDR12` (no `https://`) | `https://www.amazon.in/dp/B0BDHWDR12?tag=ravi-21` |
| `https://amzn.to/3xYzAbC` (opaque short code) | resolved to the product page, then `/dp/<ASIN>?tag=ravi-21` |
| `https://www.amazon.in/s?k=earbuds` (search page) | route kept, `tag=ravi-21` added |

Read it before it goes out, and audit it after:

```bash
# what does this source post carry, and what will the hub do with each link?
python -m influencer_hub.cli amazon-scan --file /tmp/post.txt

# one creator's exact post (any role: broadcast / whatsapp / approval)
python -m influencer_hub.cli amazon-scan --file /tmp/post.txt --creator 7

# all 1000+ creators in one pass: does each one get their OWN tag?
python -m influencer_hub.cli amazon-scan --file /tmp/post.txt --fleet --check   # exit 1 on any problem

# the posts we really sent: whose tag is on them? (cron-safe, read-only)
python -m influencer_hub.cli amazon-scan --report --hours 48 --check
```

`--fleet` is pure text work (about 0.8 s for a thousand creators on the demo
hub) and writes nothing, so it is safe as a preflight before a big paste is
switched on. `--report` reads `posts.deal_text` — the message the subscriber
actually received — and names any link that carried **someone else's** tag, so
the mistake is impossible to miss instead of hidden in a log line. The
dashboard's 📈 Ops digest card shows the same proof live
(`GET /api/amazon-report`), the 🔎 block under it lets you paste a source post
and scan it in place (`GET /api/amazon-scan?text=…&fleet=1`), and the self-check
has a twelfth check — **Amazon links in real posts** — that fails loudly when a
post went out paying the wrong person.

### 📈 Ops: the digest, the alerts, and the exports

Three things make a 1000-creator hub something you *operate* rather than watch.

**The digest** — one card, on the dashboard (📈 Ops digest, refreshed live at
`/api/digest?hours=24`) and on the command line:

```bash
python -m influencer_hub.cli digest                 # print the 24h card
python -m influencer_hub.cli digest --send          # ...to OPS_TELEGRAM_CHANNEL
python -m influencer_hub.cli digest --json --hours 6
```

It reports delivered / failed / skipped / mirrored posts, the conversion-retry
queue, worker state, active sources, creator readiness (ready · blocked ·
mirroring · setup %), the current self-check verdict and the two most common
failure reasons. Failure counting uses each attempt's own timestamp
(`posts.created_at`), so a bad hour can never look like a quiet one.

**The alert policy** — `python -m influencer_hub.cli self-check --alert` (cron
or a systemd timer) speaks to the ops channel only when it matters:

| Situation | What happens |
|---|---|
| verdict got worse (e.g. `attention` → `fail`) | alerts immediately |
| same verdict still not green | re-alerts once the cooldown (default 3h) passes |
| everything is green again after being broken | one “recovered” message |
| green and was green | silence |

The message names the failing checks **and the fix** (`systemctl start …`,
“save each creator's own tag”, …). Without `OPS_TELEGRAM_CHANNEL` nothing is
sent and nothing is recorded, so a hub that has not opted in gets no surprise
messages. State lives in global settings, so a restart does not re-alert.

**The history** — one row per day in `ops_snapshots` (`python -m influencer_hub.cli
snapshot`, or automatically whenever the daily digest is sent). From it the card
draws a delivery trend: `7d: ▁▅▇█ · green 2d`, plus "±N% vs the previous day".
Without history a hub can only ever say how it is *right now*; with it you can
tell a slow week from a bad afternoon. The self-check has an eleventh check for
it ("Ops history") that names the cron line when the row is not being written.

**One story per outage** — alerts carry the incident with them: the first
"got worse" message opens an incident, repeat messages say `(open 4h · alert #2)`
instead of pretending it is new, and the recovery message closes it with the
duration ("it was broken for 1d · 3 alert(s)").

**The chart** — the ops card draws one bar per stored day (height = delivered
share, colour = that day's verdict), so a slow week is visible at a glance, and
**“What would the ops alert say right now?”** previews the exact message the ops
channel would get — computed with the pure policy, so it neither sends anything
nor moves the cooldown.

**Metrics and the audit log** — `GET /api/metrics` is Prometheus text
(`hub_posts_attempts{status="failed"}`, `hub_worker_up`, `hub_creators{state=…}`,
`hub_selfcheck_state{verdict=…}`, `hub_green_streak_days`), so whatever already
watches the VM can watch the hub too. `GET /api/posts.csv?hours=24` (the
**⬇️ Posts log (CSV)** button) is every attempt with creator, channel, status and
reason — the audit trail behind the counts.

**Backup and restore** — `python -m influencer_hub.cli backup --out /var/backups/hub
--keep 14` takes a *consistent* copy with SQLite's online backup API (safe while
the worker is posting; a plain `cp` of a WAL database is not) plus a JSON sidecar
saying what is inside. `cli restore <file>` refuses anything that is not a hub
database, asks before doing anything (`--yes`), and keeps the previous file as
`*.pre-restore-<timestamp>`. Secrets live in `.env` and are deliberately **not**
in a backup, so a copy is safe to move to another machine.

**The exports** — the checklist and the bulk template as spreadsheets:

* `GET /api/readiness.csv` — one row per creator (same `q`/`state` filters as
  the API): ready, tag + where it came from, the three channels, phone status,
  next step + action, mirror state, warnings and the per-step track.
* `GET /api/easy-setup/template.csv` — the exact columns the bulk paste accepts
  with three example rows (the template is parsed by the same code that imports
  it, so a filled-in spreadsheet always loads).

### 🛡️ The money map: one Amazon tag belongs to one creator

Two mistakes cost real money and are invisible until the payout report: two
creators posting with **one** Amazon tag (Amazon pays one account for both
people's clicks), and one destination saved on **two** creators (every deal
posted twice, each copy signed with a different tag). The hub now looks for both
everywhere — and refuses them at the door.

```bash
python -m influencer_hub.cli assignments             # exit 1 when money is mixed
python -m influencer_hub.cli assignments --json
python -m influencer_hub.cli assignments --fix-hint
curl -s http://127.0.0.1:5000/api/assignments        # admin only
```

`/setup → 🛡️ Money map` lists every finding with the creators (linked) and the
fix, and the self-check has a **Money map (tags & channels)** item that turns
amber the moment anything would pay the wrong person. A scan of 1000 creators +
1000 channels takes ~45 ms, so it is cheap enough for a card and a cron.

| Finding | Severity | Why it matters |
|---|---|---|
| Two creators share one Amazon tag | ⛔ high | the clicks earn for one account only |
| One destination saved on two creators | ⛔ high | the deal is posted twice, signed differently |
| A saved tag that is not usable | ⛔ high | the links fall back to the hub-wide tag |
| Creator still on the hub-wide tag | ⚠️ medium | earns, but under the shared id |
| Same channel as preview **and** main | ⚠️ medium | the preview is the broadcast |
| Two profiles with one name | ⚠️ medium | a paste could match the wrong one |

Channel identity is compared properly: `@Ravi_Loots`, `t.me/ravi_loots`,
`https://t.me/Ravi_Loots/` and a trailing `?query` are the **same** channel, and a
WhatsApp newsletter JID never collides with a Telegram handle.

**The door.** Every add path — the team page, Easy Setup, a bulk paste and the
inventory import — runs the guard *before* writing: a row that would reuse
somebody's tag or channel is refused with that creator's name and the reason
(`⛔ Amazon tag “ravi-21” already belongs to Ravi Loots`), and the attempt is
audited (`add_blocked_duplicate`). Inside one paste the index stays honest too, so
two lines of the same file cannot create the collision. Updating a creator's own
tag or channel is of course still allowed (its own id is ignored).

### 🏷️ Tag proof: the tag saved is the tag posted — or nothing posts

The money map finds the **mix-ups**. This is the promise: whatever tag you type
for a creator, the Amazon links that go out on **their** channels carry exactly
that tag — and a link that would carry anybody else's id is never published.

Every destination resolves its tag the same way, in one place
(`tag_proof.signing`): a channel's own Amazon override → the creator's own
Associate id → the hub-wide `AMAZON_ASSOCIATE_TAG`. That last one is **not** the
creator's own: the database fills a blank tag with it so a deal is never
dropped, which is exactly the mix-up that loses a creator their commission. So
the hub treats it as missing, and:

| Situation | What happens |
|---|---|
| Creator has their own tag (or a channel override) | the post goes out, links signed with it — approval, main, WhatsApp mirror alike |
| Creator is still on the hub-wide tag, deal has Amazon links | the post is **held**: nothing is published under the shared id. The deal is parked with the reason `no_own_amazon_tag`, logged, audited (`amazon_tag_hold`) and retried by the worker |
| Creator saved the tag before the worker's next pass | the parked deal goes out — signed with **their** tag |
| Deal has no Amazon link (Flipkart/EarnKaro/HYPD/Meesho) | untouched: EarnKaro and HYPD money goes to the hub's central accounts by design |
| Channel strips Amazon links | never held (there is no Amazon link to sign) |

The hold applies to **every** role including the approval preview: an approval
channel is still a channel of that creator, and the link there is the one you
are being asked to approve — a wrongly-signed link is one click from going out.

```bash
python -m influencer_hub.cli tag-proof                  # every active channel, exit 1 if any is held
python -m influencer_hub.cli tag-proof --creator 62      # the exact link #62's posts will carry
python -m influencer_hub.cli tag-proof --json
curl -s http://127.0.0.1:5000/api/tag-proof              # admin only: counts + who is held
curl -s "http://127.0.0.1:5000/api/tag-proof?creator=62" # one creator: link + per-channel tag
```

```
$ python -m influencer_hub.cli tag-proof
⛔ 2 of 62 active channel(s) still on the hub's shared tag (No Tag Yet, Copy Cat)
Paste each creator's own Associate tag (for example on No Tag Yet's page) — until
then their Amazon deals are held, never signed with mama086-21.
  ⛔ No Tag Yet (#41) — @notag
     Amazon deals held: no own tag (would post as mama086-21)
```

Where you see it: `/setup → 🏷️ Tag proof` (one line per held creator, with the
link their posts would carry today and a **Paste their own tag →** link), a chip
on the dashboard home, the **Amazon attribution** self-check item, the daily ops
digest (`🏷️ Amazon tags: 61/62 channel(s) own · ⛔ 1 held (No Tag Yet)`) and the
creator's own page — where the review line now states the exact link
(`Amazon links will read www.amazon.in/dp/…?tag=ravi-21`).

The whole-fleet proof is **one batched pass**: 1001 creators + 1001 channels are
checked with two queries (measured < 1 s in the suite), so it runs on a page
render and on a cron alike.

One switch, for a hub that really does run a single shared account:

```bash
REQUIRE_OWN_AMAZON_TAG=off        # restores the old fallback signing (default: on)
```

### 🧾 Text fidelity: nothing goes missing, nothing is invented

Two failures a creator notices before the owner does: a line from the source
post that never reached their channel (a title, a price, a size list — a lost
sale), and a line that was **never** in the source appearing under their name
("unwanted text"). The hub now checks every post against the text it came from
before it is published.

| What is compared | What it means | What happens |
|---|---|---|
| Product text | every deal line's words **and numbers** (a price going missing is a lost sale) | a line that would vanish is caught before the post goes out |
| Inventions | output lines that are not derived from the source | anything the hub did not put there on purpose is reported |
| Links | the **link budget** — a post must not come out with fewer links than it went in with | a rewritten link (own tag, EarnKaro, shortener) is fine; a link that vanished is not |

The renderer runs twice when needed: the normal render first, and — if real text
would be lost — a conservative render that keeps **every** source line and only
rewrites links. The better one is posted; a recovery is logged and audited
(`text_fidelity_recovered`). What a switched-off network removed is recorded on
the delivery row, so "why is this post different from the source?" has an answer
in the log itself:

```
$ curl -s -b cookies http://127.0.0.1:5000/api/posts.csv | tail -2
…,posted,6d1f…,,,network merchant: NIRLON Shirt at ₹399 — Sizes: M, L, XL
```

The self-check has a **Source text & duplicates** item that turns amber with the
creator's name the moment a post changed its source text.

### 📡 Deal sources: the default channel, and picking several

A creator's posts come from the sources **you** set beside their channels.

* **Nothing selected** → the hub's default source, `https://t.me/LootZoneIndia11`
  (`DEFAULT_SOURCE_SPEC`), is used — a creator never quietly posts from every
  source in the hub. One click on `/setup → 📡 Source pool` adds it to the pool
  (`/sources/ensure-default`), the guided wizard does it the first time it is
  needed, and the worker does it at startup — so a brand-new box posts from
  LootZoneIndia11 even if nobody opened the dashboard yet.
* **Tick as many as you want** in the guided wizard (step 2) — "pakka manam set
  chesina source channels pakkana multiple ga select cheskunelaga". What is
  stored is the plain token list the pipeline filters on
  (`allowed_sources`: `LootZoneIndia11, powerloot`); anything you paste —
  `https://t.me/LootZoneIndia11`, `@LootZoneIndia11`, a private invite — is
  normalized to its token, because that is what a Telegram dialog name matches.
* **Every source** is still possible: tick *Post from every source in the hub*.
* The picker is one shared block, so it is identical on the **Setup Center**,
  in the **guided wizard** and on a **creator page**: tick-boxes side by side
  (tick several), each row labelled with the source's **name** and its spec —
  `Power Loots · @powerloot` — never a bare token, plus a *paste as many as you
  want* box that understands names, `@handles`, `https://t.me/…` and private
  invites, and resolves a configured source's name to the token the pipeline
  filters on.
* The creator page's source fields suggest the tokens that exist (a `<datalist>`),
  per creator **and** per channel — one channel can read one source while the
  creator's others read several.
* The **Quick Add** form has the same field, pre-filled with the default, plus a
  *post from every source* tick — so a creator added in one line reads the
  default without anybody thinking about it.
* Creators saved **before** the default existed have no source at all, which has
  always meant "every source in the hub". The pool card says how many there are
  and switches them in one click (`POST /sources/apply-default`, audited); it
  never touches a creator the operator already pointed somewhere.

### 🎯 Categories never cost a sale

A creator can be narrowed to chosen categories ("this one posts electronics"),
and the filter is keyword based — so it can only ever be sure about the deals it
recognises. `🔥 BOAT AIRDOPES at ₹1299 · Use code BOAT500` matches no electronics
keyword and only the word *code*: dropping it for an electronics creator would be
a lost sale caused by our own vocabulary. So the filter now drops a deal only
when the hub is sure it belongs to a **different product category**:

| Deal | Creator asked for | Result |
|---|---|---|
| `Banarasi Silk Saree at ₹899` | electronics | dropped — it is clothing |
| `BOAT AIRDOPES … Use code BOAT500` | electronics | **posted** — the brand is unknown to us, the coupon is not a category |
| `General quiz contest information` | electronics | posted (unknown), unless strict mode is on |

`CATEGORY_FILTER_STRICT_UNKNOWN=on` restores the old, strict behaviour for an
operator who wants a tightly curated feed.

### 🔁 No duplicates — the same product never lands twice

The operator's rule: "multiple posts same products asalu same channel lo
cheyoddu … 1 hours lo same products vasthe skip cheyali". Four guards run on
every destination, cheapest first, and each one writes its reason to the delivery
log (so the self-check can report "N duplicates were blocked"):

| Guard | Catches |
|---|---|
| deal signature (ASIN / HYPD token / Flipkart item / URL) | the exact same product, however it was worded |
| identifier dedup + in-run set | two creators (or two passes) posting to one shared channel |
| content hash, 24 h | identical title+price even when the signature differed slightly |
| near-duplicate, 12 h, 60% word overlap + same price | the same product from another source, re-worded — **well past** the 1-hour rule |
| 20/hour and 5/10 min per channel | a bug or a backlog turning into spam |

### Per-creator networks: only Amazon on means only Amazon products post

A creator's switches (Amazon / EarnKaro / HYPD, `only_amazon`) decide what their
channels may post, and the **whole product block** follows the switch: a Flipkart
product for a creator with EarnKaro off is removed with its title, sizes and
price (a title with no link to buy it is a dead deal, not copy), and the delivery
note says which network removed it. With EarnKaro on, the same block posts with
the hub's converted `ekaro.in` link; Amazon blocks always post signed with that
creator's own tag. Two creators, one source post, two correct channels:

```
@both_on:   🔥 BOAT AIRDOPES at ₹1299 → amazon.in/dp/…?tag=bothon-21
            NIRLON Shirt at ₹399 Sizes M,L → ekaro.in/our-conversion
@amz_only:  🔥 BOAT AIRDOPES at ₹1299 → amazon.in/dp/…?tag=amzonly-21
            (the Flipkart block removed — EarnKaro is off for this creator)
```

### ✍️ Adding creators the easy way: one line, anything you have

Whoever is onboarding does not need a column order. This is all the same row:

```
Ravi Loots ravi-21 @ravi_loots @ravi_amazon_deals 120363012345678901@newsletter +91 98765 43210
Sita Deals | sita-21 | t.me/sita_loots | https://t.me/sita_approval | 120363019999999999@g.us
Kiran, kiran-21, @kiran_main, @kiran_approval, , 9876543210, electronics, 09-23, budget, VIP
```

Commas, tabs, pipes, semicolons or just spaces; the Amazon tag, both Telegram
channels, the WhatsApp id and a phone number in any order; `key=value` extras
(`notes="instagram 12k"`, `price=premium`, `hours=09-23`, `store=93944`); a header
row from any CSV export; `#` comments and blank lines skipped. The parser says
what it understood (`main channel ← @ravi_loots`) and, on the team page,
**🔎 Check these lines first** shows the table before anything is written — the
same code path as the add itself, so the preview cannot lie. A value in the wrong
column is moved and reported (`1203…@newsletter → WhatsApp, was in the main
column`); a plainly wrong tag stays wrong, so the row is refused with the reason
instead of being guessed at. After the six documented columns the order is
`categories, posting hours, price filter, notes`, and a note typed into the price
column is kept as a note. A leftover word in a line that has **no** separators
also becomes a note rather than a guess: notes are internal, while a category
decides which deals a creator sees — so settings there are written explicitly
(`categories=electronics`) or in a comma/tab row.

### 👥 New-creator defaults: set once, every add inherits

The team should never have to decide which networks are on, which HYPD store
earns, or whether the main channel mirrors into WhatsApp:

```
/setup → 👥 New-creator defaults   (or: python -m influencer_hub.cli add-defaults --only-amazon on)
```

Amazon / EarnKaro / HYPD switches, Amazon-only, the mirror, categories, posting
hours, the price filter and the HYPD store id — stored in the hub's own settings
(they travel with the database), audited (`add_defaults_changed`), and shown to
the team as one sentence on their page: *“New creators start with: Amazon,
EarnKaro, HYPD · mirror on · HYPD store 93944.”* Precedence is always: the pasted
row → the form on screen → your presets → the code defaults, so a specific paste
still wins and nothing that was set once gets silently overridden.

### 📦 Creator inventory: export EVERYTHING, edit in Excel, paste it back

The owner can take the whole hub out in one click and put it back — with the
safety of seeing exactly what would change **before** anything is written.

**Export** (one row per creator, 31 columns): `creator_id`, `name`, `added_by`,
`created_date`, `created_at`, `setup_age`, `setup_age_days`, `active`, `ready`,
`amazon_tag` + `tag_source`
(creator / admin / none), the approval / main / WhatsApp channel identifiers,
`wa_status`, `phone_number`, `mirror_whatsapp` (`yes`/`no`/`inherit`), the four
money switches (`allow_amazon`, `allow_earnkaro`, `allow_hypd`, `only_amazon`),
`hypd_store_id`, `categories`, `posting_schedule`, `price_filter`, `notes`,
`next_step`, `next_action`, the 24h counters (`posts_24h`, `mirrored_24h`,
`last_post_at`). Every value comes from the same tables the worker posts from,
so the file and the hub cannot disagree.

```bash
# the button on /setup (📦 card) and on the console (⬇️ Full inventory (CSV))
python -m influencer_hub.cli export-inventory --out inventory.csv
python -m influencer_hub.cli export-inventory --member Ravi --out ravi.csv
curl -s "http://localhost:5000/api/inventory.csv" -o inventory.csv   # admin only
```

**Import** — same file, edited in Excel/LibreOffice/Sheets, pasted into the 📦
card (or uploaded): the hub plans first and shows **line, creator, and
`field: old → new` for every row**, lists the rows it cannot use with the line
number and the reason, and only the “✅ Apply the changes” button writes.
Nothing is ever written from a preview.

```bash
python -m influencer_hub.cli import-inventory --file inventory.csv            # preview
python -m influencer_hub.cli import-inventory --file inventory.csv --apply    # write
python -m influencer_hub.cli import-inventory --file new.csv --create --apply # + new creators
python -m influencer_hub.cli import-inventory --file inventory.csv --json     # for scripts
```

A 1001-creator hub exports in ~0.2 s and previews a full edited file in ~0.1 s
(the hub-wide values — channels, sessions, mirror counts, post activity, the
mirror default — are read once, not once per creator). The pasted file is kept
in a **private server-side stash** (mode 0600, three-hour TTL) and only a random
token travels in the browser session, so a 250 KB spreadsheet cannot bloat or
break the sign-in cookie; if a preview expires the card says so instead of
applying nothing.

The rules that make a spreadsheet safe to hand back:

| You write | What happens |
|---|---|
| a new value | that one field is updated (validated first) |
| an **empty** cell | the value is left exactly as it is — an empty spreadsheet column can never wipe data |
| a single `-` | that field (or channel) is cleared on purpose |
| `yes` / `no` | the money switches, `active`, mirroring |
| a name that is not in the hub | reported, and only created when “Add rows whose name is not in the hub yet” (`--create`) is ticked |
| an unusable Amazon tag | reported with its line number, skipped — a creator is never re-pointed at somebody else's tag |
| a row it cannot match | reported with its line number, the rest of the batch still applies |

Creators are matched by `creator_id` first (safe when two people share a name),
then by name. Channels are matched by role, so pasting a channel identifier in
`whatsapp_channel` pairs that WhatsApp destination exactly where the worker
looks for it. Every applied row is written to the audit log
(`creator_updated` / `creator_created` / `inventory_imported` with counts), and
the result table stays on `/setup` as “Result of the last import”.

### 🧭 Guided setup: name → channels → categories → go live

One page that takes a creator from nothing to posting, in four numbered steps.
Every step saves on its own, so the operator can close the laptop in the middle
and come back — the page reopens on the step that is still missing.

```
/setup → 🧭 Guided setup        (top navigation: 🧭 Guided)
```

| Step | What is saved | What the page does for you |
|---|---|---|
| 1 · 🧑 Creator | the name and **their own Amazon Associate tag** (e.g. `ravi-21`) | creates or resumes the creator; a tag or channel that already belongs to somebody else is refused **naming the owner**; the creator is stamped with who added them |
| 2 · 🔗 Channels | approval (preview), main, and the WhatsApp channel / group link | pastes in any shape (`@handle`, `t.me/…`, full link); both Telegram channels inherit that one tag; the main channel mirrors to WhatsApp unless you switch it off; **QR / login-code pairing is inline**; a pasted WhatsApp invite link that needs the phone is kept (never silently dropped) and re-resolved on the next save |
| 3 · 🛍️ Deals & hours | the category picker (12, the same list the creator page uses), posting hours, price filter, the four network switches | pre-filled from your **new-creator defaults**; ticking “All categories” clears the rest (it means *any loot*); a save can never leave a creator with no network |
| 4 · 🚀 Go live | active + the review | a line-by-line review (“their own Amazon tag — `ravi-21`”, “WhatsApp destination — not linked yet”) marked ✅ or *still missing*, then **Go live**, **Send a test deal now** (the same code path the worker uses) and Test a Deal |

A progress pill always says where this creator is: *“3 of 4 steps done · next:
Deals & hours”*. The whole flow is plain forms — it works with JavaScript
switched off, and JavaScript only moves the highlight along as each step saves.
Every save is audited (`guided_creator_created`, `guided_channels_saved`,
`guided_deals_saved`, `guided_finished`).

**The team may use it to add, never to edit.** A team member can create a
creator and keep filling in that creator's channels and settings **while that run
is open**; the moment the run finishes (or on any creator they did not create in
that run), the wizard refuses them: *“already saved — ask the owner to change
it.”* Every step re-checks ownership server-side before any write — walking the
flow for a creator somebody else added is refused before the view runs, however
the request is shaped, and recorded as `team_blocked_route` — exactly how
`/team/add` already treats a name that is already in the hub. Editing and
removing stay the owner's job.

### 🕒 Setup date and age beside every creator

When many people are adding creators, “*when* was this one set up, and *how long*
has it been running?” is the question the owner asks first. The hub answers it
everywhere the creator list appears.

```
today · 1 day · 6 days · 4 weeks · 2 months · 11 months · 1 year · 1 year 3 months · 2 years 6 months
```

* `influencer_hub/age.py` does the wording once: **one unit** in lists
  (`2 months`, `1 year 3 months`), **two units** on a card (`1 week 6 days`,
  `1 month 29 days`); under 7 days it counts days, under 30 weeks, and past 345
  days it becomes years + months — “12 months” can never appear. A date the hub
  cannot read shows as *“Set up date unknown”* instead of a wrong number.
* **Dashboard command center**: a **Set up** column next to every creator
  (`2 years 6 months` · the date · *by Mohan*), plus chips for the fleet —
  *oldest 2 years 6 months · newest today* and *➕ 6 added in the last 30 days*
  with the names, the age and who added each one.
* **Setup directory** (`/setup#creator-directory`): each card says *Added
  2026-08-10 by Mohan · 🕒 2 months*; the list can be ordered **oldest first,
  newest first, A–Z or Z–A** and cut to a **date range** (`since` / `until`), so
  “show me what we set up last month” is two clicks. A creator whose stored date
  is unreadable is never hidden by a filter.
* **Easy Setup**: every creator pill carries its age, and the strip header prints
  the hub line — *“Set up: oldest 2 years 6 months ago · newest today · 6 added in
  the last 30 days.”*
* **Team page**: the same age column for the creators that member added.
* Everywhere: hover the line (or read the export) for the full
  `Set up 1 year 3 months ago · 2025-07-14 · by Mohan`.

One `summarise()` call per page — the age of the whole hub costs no extra query
per creator, so the 1000-creator hub stays a single fast read.

### 🚀 Dashboard: the setup command center

The dashboard home (`/`) is not just counters. Above the metrics it renders a
**read-only checklist** for every creator and the *single next action* that
unblocks the most:

| Column | What it answers |
|---|---|
| Set up | When this creator was set up, how long ago, and which team member added them |
| Own Amazon tag | Is this creator's Amazon money going to *their* id, or still the hub-wide shared default? |
| Approval · Main · WhatsApp | Which of the three channels exist, and which are Pending instead of live |
| Phone | Is the creator's WhatsApp phone actually linked (QR or login code)? |
| Live | Has a WhatsApp destination passed its visible Test post? |
| Mirror · 24h | Is main → WhatsApp copying deals right now, and how many went out today |
| Next action | The one button/link for the next step, deep-linked to that creator page |

A progress bar, chips (ready / blocked / mirroring / worker / sources) and a
**"Do this next"** list put the blocked creators first, so a hub with twenty
creators still answers "what do I do now?" in one line. The panel re-checks
itself every 20 seconds against `/api/readiness?live=1` — a GET-only JSON API
(also available per creator: `/api/readiness?inf_id=<id>`) that never creates a
session, sends a message or flips a switch. A stopped WhatsApp hub degrades to
the saved state with a visible "hub unreachable" note instead of an empty panel.

The Setup Center's creator directory shows the same next step on each card, and
Easy Setup shows it for the creator being edited.

Everything in the pairing list can also be done without leaving **Easy Setup**
(`/easy-setup`): pick the creator from the pill strip at the top (a 🟡 pill is
blocked on a step), and the screen shows that creator's readiness track, the
inline QR / login-code form and, once the phone is linked, the **Send Test
post** button for the Pending destination. If the creator has no WhatsApp
channel at all, the same screen creates one from the creator's own linked
account in one click (saved as Pending, then the Test post activates it).
Actions triggered there return to the same screen with their status, and the
form prefills the creator's saved tag, channels and switches, so editing an
existing creator is one screen too.

### 1000+ creators: one paste, and a dashboard that stays fast

Onboarding a hundred or a thousand creators is the same screen as one, and it is
**one paste**:

```
Ravi Loots, ravi-21, @ravi_loots, @ravi_amazon_deals, 120363012345678901@newsletter
Sita Deals, sita-21, @sita_loots, @sita_amazon_deals
Kiran Deals, kiran-21, @kiran_loots
```

Columns are `name, amazon tag, main channel, approval channel, whatsapp id`.
Comma, tab, `|` or `;` separators all work, `#` comments and blank lines are
ignored, and a header row (`name,amazon_tag,broadcast_tg,approval_tg,whatsapp_id`)
or a CSV/TXT upload is understood too. Up to **2000 lines per submit**.

* Re-pasting the same list **updates** those creators (matched by name) instead
  of duplicating them, so a corrected spreadsheet is simply pasted again.
* Every line is validated on its own and reported line by line: unusable
  Amazon tag, missing channels, approval == main, and so on. One bad line never
  stops the batch.
* A real WhatsApp channel id (`…@newsletter`, `…@g.us`) is saved as **Pending**
  immediately. An invite *link* needs that creator's paired phone to resolve, so
  it is reported as "paste it after pairing" rather than guessed at — a
  1000-line import never fires a thousand hub calls.
* The three network switches and the main → WhatsApp mirror switch apply to the
  whole batch.

Afterwards the 🚀 command center is the to-do list: filter by `⚠️ Blocked`, read
the next action per creator, and clear them page by page.

**What keeps it fast at that size** (verified with 1000 creators / 3000 channels):

| Screen | Time | What is rendered |
|---|---|---|
| `/` command center | ~0.6 s, 65 KB | one page of rows (25), hub-wide chips and the priority list |
| `/easy-setup` | ~0.1 s, 41 KB | 40 creator pills + paging + the bulk paste card |
| `/setup` | ~0.15 s | 60 directory cards + a capped simulator list |
| `/api/readiness?live=1&limit=25` | ~0.1 s | one page of creators, 50 warnings, 200 actions |

The readiness engine reads each table **once** for the whole hub (channels,
sessions, mirror counts, the mirror setting) — a 400-creator overview makes
7 database reads, not 1200 — and the dashboard never ships a thousand rows to
the browser. Search (`q`), state filters (`state=blocked|ready|mirroring|…`) and
paging (`limit`/`offset`) work the same in the UI and in the JSON API.

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
HUB_SKIP_DOTENV=1 PYTHONPATH=. HUB_DB_PATH=/tmp/hub.sqlite3 python -m pytest tests/ -q
```

`HUB_SKIP_DOTENV=1` makes the run ignore any provisioned `.env` completely, so
the suite always exercises plain code defaults — no real Telegram session,
affiliate key, shortener token or bot credential is touched (the whole suite is
green this way; see VERIFICATION §1).

## Production integrations and defaults

- New influencer records and omitted Amazon tags use `mama086-21` only as a
  configured fallback; supply each influencer's own Associates tag whenever
  available. Explicit per-influencer/channel tags are supported. Amazon.in
  product ASIN links are cleaned to `www.amazon.in/dp/<ASIN>`; Amazon.com ASINs
  stay on Amazon.com, and non-product routes keep their original marketplace,
  path, and required query parameters. The effective `tag` replaces the source
  tag; safe numeric product selectors (`th`, `psc`) are preserved.

  **Compact Amazon links, without losing the creator ID:** set one
  operator-owned HTTPS origin in `AFFILIATE_SHORT_LINK_BASE_URL` (for example
  `https://go.your-domain.in`) and route it to this dashboard. Broadcast and
  WhatsApp posts then use
  `https://go.your-domain.in/a/<opaque-code>?tag=<creator-tag>`. The route only
  redirects when **the configured origin + stored code + exact tag** match; its
  only destination is the canonical Amazon item with that same tag. A forged
  `/a/...` URL, a stale code, an attacker host, an altered tag, or an unsafe
  stored target is rejected and never counted as an earning link. The previous
  `/amazon/<code>` path remains live for already-posted links. The dashboard's
  Save confirmation and **Test a Deal** preview mint and display the actual
  clickable compact link plus its canonical redirect target. With no public
  HTTPS origin configured, the safe canonical URL stays visible instead.

  Approval-role posts deliberately remain native Amazon URLs. Amazon-issued
  `amzn.to` codes must come from Amazon Associates/SiteStripe; the app does not
  invent them.
### One branded short-link domain (production setup)

A short URL must have a real public HTTPS hostname; the app cannot safely make
one out of `localhost`, a VM IP, a request `Host` header, or somebody else's
shortener. Point a domain/subdomain you control (for example `go.example.in`) to
the same reverse proxy as the dashboard, then put only the origin in the private
VM `.env`:

```dotenv
AFFILIATE_SHORT_LINK_BASE_URL=https://go.example.in
# Optional only when Amazon/HYPD need separate domains:
# AMAZON_SHORT_LINK_BASE_URL=https://amz.example.in
# MEESHO_SHORT_LINK_BASE_URL=https://go.example.in
DASHBOARD_TRUST_PROXY=true  # only behind the one trusted TLS proxy
```

A minimal nginx virtual host can proxy the short domain to the existing dashboard
service (keep the dashboard's admin routes firewalled/authenticated; only the
validated `/a/`, legacy `/amazon/`, `/m/`, `/l/` redirects and `/healthz` are
public):

```nginx
server {
    listen 443 ssl http2;
    server_name go.example.in;
    # certificate directives managed by your TLS/Certbot setup
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Restart `influencer-dashboard` and `influencer-deal-worker`, then use **Test a
Deal** or save a channel. It shows a real `/a/<code>?tag=<creator-tag>` link;
open it once and verify its `302` location is the printed canonical Amazon URL
with the same tag. Do not enable generic Bitly merely to hide attribution:
EarnKaro outputs (`ekaro.in`/`fktr.in`/…) are already network-short and retain
our publisher proof; verified HYPD links may use `/m/<code>`. A LehLah `/l/<code>`
redirect preserves the source link but is not proof of this hub's account.
Raw, foreign, unresolved, or unverified links are not presented as ours; strict
mode holds them for retry or operator review.

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
  through EarnKaro/HYPD/Bitly. Since this identifies the source network rather
  than this hub's publisher account, strict commission mode holds these links;
  no LehLah account-ID verification is configured. Optional `/l/<code>` wrapping
  is disabled by default and does not turn source attribution into our proof.
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

Set `DASHBOARD_ADMIN_PASSWORD` to the first value, `DASHBOARD_SECRET_KEY` to the second, `WA_HUB_TOKEN` to the third, plus `HUB_ENV=production`. The admin password must be at least 16 characters; the signing key must remain stable across restarts and workers. Leave `ADMIN_DELETE_PASSWORD` unset to reuse the dashboard password for destructive confirmations. Production mode enables Secure cookies on HTTPS requests (over plain HTTP the flag is dropped so sign-in still works), and the dashboard listens on `0.0.0.0:5000` so it is reachable on the VM IP — keep that port firewalled or fronted by HTTPS. The WhatsApp hub binds to loopback by default, refuses non-loopback production binding, and requires a strong private token for any non-loopback bind in every environment. `/healthz` is the sole unauthenticated health endpoint besides validated first-party affiliate redirects. Do not deploy publicly with missing/weak dashboard credentials.

### 🚀 Deploying and updating in one command (`deploy/deploy.sh`)

Updating the VM used to be "pull, remember the migrations, restart, hope". It is
now one command that cannot surprise you — it **checks first, backs up, and can
undo itself**:

```bash
./deploy/deploy.sh --check     # read-only: is this box ready? what is missing?
./deploy/deploy.sh --dry-run   # print every command it would run, change nothing
./deploy/deploy.sh             # update to this checkout's branch
./deploy/deploy.sh --ref <sha> # pin exactly one commit
```

| Step | What happens | The rule that keeps you safe |
|---|---|---|
| 1 | preflight | a required failure stops it *before* anything is written |
| 2 | `cli backup` | no backup, no deploy (safe while the worker is posting) |
| 3 | `git fetch` + `git merge --ff-only` | never rewrites history, never discards a modified file |
| 4 | `pip install` / `npm ci` | only when the locks changed — **`npm audit fix` never runs** |
| 5 | `cli init` | new columns added; safe to re-run |
| 6 | restart | systemd units, or `start_services.sh` (supervised) |
| 7 | health | `/healthz` → `cli self-check --blocking` → `cli tag-proof`: ⛔ a real failure rolls back, ⚠️ warnings are printed with their fixes and never do |
| 8 | rollback | any failure: previous commit + the backup + restart, then a non-zero exit |

`--check` prints one line per thing it looked at — branch and commit, whether the
worktree has modified files, the venv and the real dependency versions, Node and
the WhatsApp modules, **which `.env` keys exist** (names only, never values), the
database and its schema, free disk, backup age, whether the systemd units are
installed, and whether the dashboard and WhatsApp hub answer right now.

```bash
$ ./deploy/deploy.sh --check
  [OK] branch        on main at 5b5696f (wanted main, remote 5b5696f)
  [OK] worktree      0 modified file(s), 3 untracked (usually .env, a venv, logs)
  [OK] python-deps   flask 3.1.3 | aiohttp 3.14.5 | telethon 1.45.0 | gunicorn 23.0.0
  [XX] env:AMAZON_ASSOCIATE_TAG  missing — hub-wide Amazon fallback tag
       fix: add AMAZON_ASSOCIATE_TAG=… to .env
Verdict: ⛔ fix first: env:AMAZON_ASSOCIATE_TAG
```

Undoing is written down for you at the end of every deploy (the exact
`--from-backup` and `git reset --hard <sha>` lines), and `deploy/DEPLOY.md` walks
through the first install, the update, the two kinds of rollback, and the
firewall. The same preflight is available in the CLI, so it can be called from
your own scripts:

```bash
python -m influencer_hub.cli deploy-check --json | python -m json.tool | head -20
```

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
| 🟢 Earning | Amazon with our tag · HYPD afflink on our store · EarnKaro link whose publisher and supported destination are explicitly or durably verified · a verified stored branded `/a/` or `/m/` short link on our configured HTTPS origin |
| 🔴 Leak | Amazon tagged for someone else (or untagged) · HYPD on another store · EarnKaro for another publisher · raw merchant with no EarnKaro conversion · raw Meesho · source-owned LehLah links without our own account proof |
| ⚪ Neutral | Informational links, and generic short links whose attribution sits behind the redirect |

Money Radar switches control what is posted. Attribution checks verify link
shape/provenance only; affiliate approval, clicks, cookies, product eligibility,
provider uptime and payout remain external and cannot be guaranteed by code.

* **Meesho → EarnKaro fallback** *(on by default)* — HYPD cannot mint an
  affiliate link from a raw `meesho.com` product URL. When enabled, the URL is
  offered to EarnKaro; it is posted only if conversion returns a link whose
  attribution passes the strict checks. Otherwise the whole deal is held. Meesho
  is never sent through generic Bitly shortening.
* **Strict attribution lock** *(on by default; `STRICT_COMMISSION_MODE=true`)* —
  fail closed if any URL in the post is unverified, mixed with a raw merchant,
  unresolved, from an unrecognized store, or lacks our publisher/tag/store
  proof. It holds the entire post, records it in the Pending attribution holds
  panel, and retries with backoff; a retry limit never forces a raw post. This
  can substantially reduce volume during provider outages.
* **Only post deals that earn** *(on by default)* — an additional zero-earning
  filter (`ONLY_EARNING_DEALS=true`). Strict attribution lock is broader: it
  also blocks a mixed post with one earning link and another unverified link.
* **Post unconverted links anyway** *(off by default)* — mirrored by
  `ALLOW_UNCONVERTED_POSTS=1`. This legacy volume-over-commission option is
  subordinate to Strict attribution lock; it cannot bypass the default-on
  strict gate. Turning strict mode off is an explicit weakening of protection.

#### Conversion guarantees, link shape by link shape

Known link types have a defined routing policy; strict mode holds anything whose
attribution or destination cannot be verified:

| Source link | What is posted |
| --- | --- |
| Amazon `/dp/<ASIN>`, `/gp/product/<ASIN>`, `?asin=<ASIN>`, search, storefront | One canonical `https://www.amazon.in/dp/<ASIN>` (or the same search/store page) carrying exactly **our** tag; every other `tag=` is removed |
| Amazon short (`amzn.to`, `amzn.in`, `amzn.eu`, `amzn.asia`, `a.co`), long link for the same product in the same post | Both collapse into that one canonical OUR link |
| Amazon short alone | Resolved over the network into a canonical link carrying the creator tag; if unresolved, strict mode holds it for review/retry rather than posting an opaque short best-effort |
| Flipkart / Shopsy / Myntra / Ajio / Nykaa / Croma / TataCliq… | A supported EarnKaro link is accepted only after its redirect reaches a supported store with our publisher ID; verified provenance is stored for later Money Radar audits |
| Same merchants while EarnKaro is down | The deal is **parked** (see below) instead of posting a raw zero-commission URL |
| Raw Meesho | Sent to EarnKaro when fallback is on; if it is not converted to a verifiable supported affiliate URL, strict mode holds the entire post |
| HYPD `hypd.store/<store>/afflink/<token>` | Retagged to our store id |
| LehLah Meesho (`mcn=LEHLAH`, `af_siteid=lehlah`, `pid=lehlah`) | Identified as source-owned attribution; strict mode holds it because this hub has no verified LehLah publisher/account ID |
| Wrapper (`bit.ly`, `tinyurl.com`, `cutt.ly`, `dl.flipkart.com/…`, `fkrt.it/…`) | Resolved first: an Amazon destination becomes the canonical OUR link, a merchant destination is converted by EarnKaro; if the destination cannot be resolved the deal is parked and the run logs `UNRESOLVED WRAPPED LINK` |
| Link written without `http://` (`flipkart.com/…`, `amazon.in/dp/…`, `amzn.to/…`) | Promoted to a real URL before anything else, then handled like every other link above (retagged/converted) |
| Known neutral links (Telegram/social destinations) | Allowed alongside at least one verified earning link |
| Anything else (news, YouTube, blog or a newly discovered product host) | Unknown product/merchant hosts are held in strict mode; recognized neutral informational links are allowed |

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

#### The retry queue — unverified posts stay held

When the only link in a deal is a merchant URL EarnKaro should have converted
but did not (key missing/expired, API outage, transient error), posting now
would earn nothing and posting later would be too late. The same applies to a
wrapper link (`bit.ly`, `fkrt.it`, …) whose destination could not be resolved —
until it is, nobody can say whether the link pays us. Instead the deal is
parked in `deferred_deals` and retried every
`DEFERRED_RETRY_DELAY_SECONDS` (5 min) for the first `DEFERRED_MAX_ATTEMPTS`
(6) attempts. With strict mode on, retry exhaustion only changes the schedule
to capped backoff (up to six hours); the worker never force-posts the unverified
URL. Held items remain visible on Money Radar until they become verifiable or
an operator reviews them. This durable queue still depends on the database and
worker being healthy; it cannot guarantee a deal is ultimately converted or
paid.

`/api/flow` reports the queue as `deferred: {waiting, retry_seconds,
allow_unconverted_posts}` and adds a note to the flow board. Money Radar shows
pending attribution holds and their retry reasons. Strict mode also holds raw
Meesho and unsupported/unrecognized product hosts rather than knowingly
posting a zero-commission link.

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
must resolve to a supported store with that ID). This checks link provenance
for the test URL, not a sale or commission attribution. The current automated
tests use mocked responses, and no production credential, source URL, or live
conversion was tested in this workspace. Normal dispatch fails closed: a short
link that cannot resolve to a supported destination with matching publisher
provenance is held rather than posted.

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
lists Channels as unsupported ([Meta onboarding feature comparison](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/)). Baileys uses the WhatsApp-Web protocol; behavior can change and account restrictions cannot be ruled out. The pipeline sends text and links to Channels; newsletter media delivery is not guaranteed (see the [Baileys newsletter text/media report](https://github.com/WhiskeySockets/Baileys/issues/2345)). Two pairing routes are implemented and produce the same registered device: scanning the QR, or typing the influencer's full international number and entering the 8-character **phone-linking code** WhatsApp shows on that phone (*Linked devices → Link with phone number*). A code belongs to one socket attempt — it is re-issued after a reconnect, cleared the moment the number is linked, never logged, and shown only on the authenticated creator page. Test with a low-risk creator/channel before enabling automatic posts.

## VM watch ("ma VM chusthundta bot")

`python -m influencer_hub.cli vm-watch --loop` samples CPU/mem/disk + whether
the bot process is alive, writes to `vm_stats`, and posts a compact card to
`OPS_TELEGRAM_CHANNEL`. The dashboard shows the latest snapshot at `/vm`.

## Dummy / separate sources

`use_dummy_sources` per influencer + the `sources` table (kind `production` |
`dummy`) let you stage a new influencer against isolated test sources before
pointing them at the live shared pool. Dummy sources are kept completely
separate from production.

### Destination safety and preview-first repairs

Each destination has its own durable permanent-failure streak in
`delivery_failures`. Only **3 consecutive known permanent delivery failures**
(e.g. Telegram `ChatWriteForbiddenError`) pause an active channel. The pause is
recorded in the team audit with its reason. Successful delivery resets the
streak; transient/unknown send failures break it. Skipped/deferred deals never
increment it. Dashboard re-enable resets it, so an operator gets a fresh start.
A watchdog persistence error is logged but cannot stop ingestion. Classification
is intentionally conservative: timeouts, FloodWait and unknown errors never
cause automatic pauses. No live posting guarantee is implied by a green check.

```bash
python -m influencer_hub.cli check-channels         # read-only Telegram checks
python -m influencer_hub.cli check-channels --fix   # explicit permanent fixes
python -m influencer_hub.cli doctor --channels     # local preview only
python -m influencer_hub.cli doctor --channels --resolve-channels # read-only lookup
python -m influencer_hub.cli doctor --channels --resolve-channels --apply-channel-fixes
```

Doctor never sends or joins. Only `--apply-channel-fixes` permits its writes;
transient Telegram lookup failures report `deferred` and leave channels active,
even during apply. Review permissions and fix the account/channel before
re-enabling paused destinations. Self-check warns about threshold breaches and
distinguishes a starting worker from a stale heartbeat (>180 seconds).

Amazon sponsored `/sspa/click?url=...` links re-sign the **inner** Amazon
URL with the creator's tag while preserving other inner and outer query
parameters. Ordinary product canonicalization remains unchanged.

For an older checkout at this release's base, copy both
`patches/apply-delivery-safety.py` and `patches/delivery-safety.patch` together:

```bash
python patches/apply-delivery-safety.py --check
python patches/apply-delivery-safety.py --dry-run
python patches/apply-delivery-safety.py
PYTHONPATH=. python -m pytest -q tests
```

The installer checks the complete patch, uses atomic `git apply` (no partial
rejects), syntax-checks and smoke-imports installed modules, and refuses a plain
repeat apply (exit 1; `--check` / `--dry-run` return 0 when installed). Missing
runtime dependencies permit syntax-only verification with an explicit warning;
this is not a substitute for the full test suite. Mismatch exits 2 without
applying. Tests and documentation are included in the bundle. Neither installer
nor preview restarts services. Deployment executes a stable temporary script
snapshot before fast-forwarding, avoiding mid-run shell input replacement.

### Easier category setup and product checks

Open **Guided setup → Deals & hours**:

1. Pick **Everything**, **Tech & appliances**, **Fashion & beauty**, or
   **Family essentials**. Quick picks only tick boxes; they do not save or post.
2. Fine-tune individual categories. Appliances, gaming, pets and travel now have
   explicit choices. Existing `books`, `toys`, `jewellery` settings also match the
   classifier's canonical IDs (no database rewrite needed).
3. Paste a product/deal and press **Preview these unsaved choices** to test
   the creator's complete category/product policy. **Check saved rules** tests
   persisted preferences, optionally narrowed by a specific owned channel.
   **Easy Setup** also has a basic **Check product — no posts** category preview;
   use its Guided Setup link for per-creator strict rules.
4. Review the settings, save, and use the existing review/go-live flow.

Offline CLI equivalent (does not initialise or modify the database):

```bash
python -m influencer_hub.cli category-preview \
  --text "Pet shampoo 500ml ₹299" --categories pet_supplies
python -m influencer_hub.cli category-preview \
  --text "Samsung smartphone gold colour" --categories electronics
```

Detection uses bounded, local word/phrase rules: no AI service, API key or
product-page request is needed. Product phrases distinguish pet shampoo from
human toiletries, notebook computers from stationery, and smart watches from
ordinary fashion watches. Multi-product posts can have multiple categories;
any selected category can match in flexible mode (strict mixed-post rules are
explained below). Common join/follow/subscribe footer lines and
URLs are ignored so a link's tracking parameters cannot choose the category.
A limited Telugu vocabulary (e.g. చీరలు, బియ్యం, మొబైల్) is supported, not full
multilingual understanding. Ambiguous colour/marketing terms alone (e.g. gold,
silver) are not treated as sufficient product evidence.

**Limits:** these are explainable heuristics, not verified catalogue metadata.
Rule scores are not accuracy probabilities. Unknown products pass by default;
`CATEGORY_FILTER_STRICT_UNKNOWN=on` holds unknowns on category-restricted feeds.
The preview identifies unknown/invalid filters and never silently changes saved
settings. Category ALLOW does not bypass channel readiness, price, schedule,
affiliate or network checks. The preview API is an authenticated, CSRF-protected,
read-only POST, keeping pasted product text out of query URLs.

### Per-creator “only these products” rules

For each creator, open **Guided setup → Deals & hours**, or their creator page
→ **🎯 Product preferences**. Choose categories, choose **Strict**, preview,
and **Save**. Nothing is changed for any other creator. Strict is suggested by
the new form, but existing installations keep their previous behaviour until
an operator explicitly saves a strict policy.

Strict creator rules form a hard cap:

- **Known selected products only.** Unknown products and offer-only text do not
  enter a category-restricted feed. An explicitly selected Recharge & Offers
  feed can accept recognised offers without a recognised physical product.
- **Optional product phrases:** `smartphone, earbuds` means at least one whole
  word/phrase must appear in product text. This narrows a broad category, e.g.
  two Electronics creators can request phones and headphones separately.
- **Never post:** excluded categories and blocked phrases always win; e.g.
  exclude Gaming, or block `refurbished, renewed`. URL parameters and common
  join/follow/subscribe footer lines do not count as product phrases.
- **Mixed posts:** default is to hold detected outside-category product
  evidence. Shared evidence is not falsely treated as another product: shoes
  can match Footwear despite also being tagged Clothing. A separate shirt in
  the post is outside-category evidence. Operators can explicitly allow a mixed
  post when at least one selected category matches; exclusions still win.
- **Channel filters only narrow** strict creator choices. A channel configured
  with All categories cannot widen an Electronics-only creator to fashion.
  A channel configured for Clothing under an Electronics-only creator may
  therefore receive nothing; use the saved-channel preview to diagnose it.
- **Normal posts, deferred retries, hourly highlights and WhatsApp mirrors**
  respect strict rules. Mirror display-filter bypass and forced unconverted
  retries do not bypass this creator product cap.

**Preview these unsaved choices** tests the form without saving. **Check saved
rules** tests the persisted creator policy and an optional owned channel. The
preview uses the same pure decision function as delivery; no network fetch,
message send, audit mutation or settings write occurs for an authorised preview.
Saving is authenticated, CSRF-protected, validated and creator-scoped, with an
audit event. Malformed stored strict rules hold delivery instead of silently
falling back. Invalid saves leave the existing category/rule configuration intact.

In strict mode choose at least one category or explicitly choose **All
categories**. All categories with no extra restrictions intentionally remains
unrestricted. Use the creator's master pause switch if nothing should post.
Flexible mode preserves previous category routing; extra strict exclusions and
phrase controls are inactive in that mode. Existing per-channel legacy override
behaviour is unchanged for flexible creators.

These remain offline heuristics, **not catalogue or per-item verification**.
Mixed-product detection only covers recognised evidence; an unrecognised item
inside a recognised deal may not be identified. Review the evidence, test real
source examples, and use narrow product phrases for curated feeds. There is no
claim of perfect semantic classification. SQLite backups preserve these rules;
the older CSV inventory format does not currently transfer the new preference
JSON, so recheck preferences after CSV-based migrations.
