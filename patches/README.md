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
same 20/hour + 5/10min rate limits and the same 45–65s WhatsApp safety pacing.
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
4. Paste the WhatsApp Channel link (`https://whatsapp.com/channel/…`) or a
   group invite. It resolves to the real JID and is saved as **Pending**.
5. Press **Test** once. Only a successful visible test activates the
   destination; after that, deals post there — including mirrored
   main-channel deals.

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

**Export** (one row per creator, 29 columns): `creator_id`, `name`, `added_by`,
`created_date`, `created_at`, `active`, `ready`, `amazon_tag` + `tag_source`
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

### 🚀 Dashboard: the setup command center

The dashboard home (`/`) is not just counters. Above the metrics it renders a
**read-only checklist** for every creator and the *single next action* that
unblocks the most:

| Column | What it answers |
|---|---|
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
our publisher proof; HYPD uses `/m/<code>`; approved LehLah uses `/l/<code>`.
Raw, foreign, unresolved, or unverified links are not branded—they are converted,
held for retry, or logged instead of being presented as ours.

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
| 🟢 Earning | Amazon with our tag · HYPD afflink on our store · EarnKaro link with our publisher · LehLah · a verified stored branded `/a/`, `/m/`, or `/l/` short link on our configured HTTPS origin |
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
