# HANDOFF — Influencers-deals

**Updated:** 2026-10-10 (Asia/Kolkata)

**Session branch:** `arena/9056711b-influencers-deals`

**Checkout base:** `c6fa901` — PR #7 merged to `main`

**Branch commits:** `fb70433` through `9c6fa4c` are already pushed on PR #8;
the current short-link hardening follows on this same branch (see section 1e).

Read this before deploying. This handoff describes the current checkout; older
notes referring to `0bf913f`, an unmerged branch, or the earlier VM rollout are
historical and are not the current source of truth.

---

## 1. Current state

The merged `c6fa901` release already includes the commission-leak fixes,
creator/central-account model, dashboard password policy, worker heartbeat,
`/api/flow`, and the Easy Setup flow card. Do not replay those older patches on
top of this release.

This follow-up fixes a separate ingestion bug: named private Telegram invite
links such as a source labelled **“Priority Source”** could fail matching when
the label was not the joined group's exact title. The old fallback only ran for
unnamed invites, leaving `selected_sources = 0` and no messages to process.

The fix in this branch:

- treats a private invite name as a title hint, not as the invite's identity;
- matches private invite hints only to an exact normalized title;
- if a private invite hint is unmatched, logs one warning per configuration and
  reads the eligible dialogs the Telegram account has already joined;
- never checks an invite or joins a channel; known output channels and
  unconfigured account-owned channels remain excluded;
- explains the fallback in Setup Center live checks and in
  `python -m influencer_hub.cli doctor --telegram-sources`;
- shows the **live** selection on the deal-flow board: `/api/flow` (and the
  Easy Setup card) name the dialogs the worker would read right now, with the
  selection mode, counts and a capped dialog list. The probe is read-only,
  cached ~45 s and can be skipped with `?live=0`;
- records each source's joined-dialog title in `source_activity.source_name`
  (auto-migrated), so per-source rows show the real group name, not just the
  configured selector;
- lets operators save a private invite without inventing a required label; and
  uses `Private Telegram source` rather than displaying the invite hash as its
  label;
- adds an autouse pytest fixture that redirects every test to a new temporary
  SQLite database, protecting the real operator DB.

The requested `236 → 236` result is reproduced by a unit test with 236 eligible
joined dialogs. The actual number on a VM depends on which eligible dialogs are
joined and which known output/owned channels are excluded.

**Important scope note:** when a private invite label does not match, fallback
means all eligible joined groups/channels, not a Telegram join of the private
invite and not only the three private links. Use public usernames or the exact
joined-dialog title when you want a narrower, deterministic source set. The
worker will then poll the selected dialogs and may post their deals according
to the configured creator/channel filters and affiliate routes.

No live Telegram account or VM was available to this checkout. The live
fallback and warning are covered by fake-client tests; run the VM checks below
before calling production delivery verified.

---

## 1b. Link conversion — every shape becomes OUR link, none posts for free

A deep offline audit (23 link shapes through the real `render_for_influencer` +
`commission_guard`, 16 deals through the real `pipeline.run_once` with a stubbed
EarnKaro, temp DBs, no network) confirmed the main conversion paths were already
exact and found four real defects. All four are fixed in `f46f165`:

1. **Opaque Amazon shorts were only known on `amzn.to`/`amzn.in`.** A deal with
   `amzn.eu`, `amzn.asia`, `a.co`, an upper-case host or an `http://` short was
   classified as an *informational link* and posted **untagged** (zero
   commission). Now every Amazon shortener host is resolved over the network
   into `https://www.amazon.in/dp/<ASIN>?tag=<OURS>` on the marketplace the
   short pointed at; if it cannot be resolved the run logs
   `UNRESOLVED AMAZON SHORT` instead of staying quiet.
2. **EarnKaro down meant a raw merchant URL was posted silently** — the guard
   reported `ok=True`, the process exited 0, the link earned nothing. Now the
   guard asks "does this post contain at least one link that pays us?" after
   every render. When the only links are merchant URLs EarnKaro should have
   converted (known merchant + credentials configured), the deal is parked in
   the new `deferred_deals` table and the worker retries it every
   `DEFERRED_RETRY_DELAY_SECONDS` (300 s) up to `DEFERRED_MAX_ATTEMPTS` (6)
   times, then posts it anyway with a warning. A converter outage delays a deal
   instead of dropping it or posting it free.
   Switch: `ALLOW_UNCONVERTED_POSTS=1` (or the Money page switch
   `allow_unconverted_posts`) restores the old "post the raw link now" behaviour.
   Raw Meesho and stores EarnKaro does not cover keep the documented retention
   behaviour and are logged as `ZERO-COMMISSION post`.
3. **The approval renderer printed the same canonical Amazon link twice** when a
   post carried both the short and the long link for one product.
4. **Opaque shorts were invisible to the guard and Money Radar**, so
   `amzn.eu`/`a.co` links were filed as neutral instead of being audited.

Unchanged and verified: Amazon `/dp/`, `/gp/product/`, `?asin=`, search and
storefront pages → one canonical link with exactly our tag; short + long in the
same post → one canonical link; Flipkart/Shopsy/Myntra/Ajio/Nykaa/Croma/TataCliq
→ EarnKaro short verified by following the redirect; HYPD afflinks retagged to
our store; LehLah preserved; informational links untouched.

`/api/flow` reports the retry queue as
`deferred: {waiting, retry_seconds, allow_unconverted_posts}` and adds
"*N deal(s) are waiting for a retry*" (a missing paying link: an unconverted merchant link or an unresolved wrapper) to the flow notes. The
README documents the per-link-shape outcome table and the retry queue.

### 1c. Wrapper links and scheme-less links (second audit round, `7d68e75`+)

A second, pipeline-faithful audit (55 link shapes through
`promote_scheme_less_links` → `resolve_opaque_links_in_text_async` →
`filter_disallowed_affiliate_links` → EarnKaro conversion → `render_for_influencer`
→ `commission_guard`, all offline) found one more class of silent leaks and three
sharp edges:

5. **Generic shorteners were filed as "informational"** (`bit.ly`, `tinyurl.com`,
   `cutt.ly`, `dl.flipkart.com/…`, `fkrt.it/…`). Whatever they pointed at — a
   Flipkart product, an Amazon page, someone else's affiliate link — travelled
   through the post untouched and unverified. `classify_url` now returns
   `shortener` for them and `resolve_opaque_links_in_text_async()` unwraps every
   wrapper before anything classifies, filters or converts the deal: an Amazon
   destination becomes OUR canonical `/dp/ASIN?tag=OURTAG`, a merchant
   destination becomes the real URL that the normal EarnKaro conversion turns
   into OUR `ekaro.in` link. Wrappers that cannot be resolved are logged as
   `UNRESOLVED WRAPPED LINK`, the guard reports them (`commission_guard.WRAPPED_KINDS`),
   and the deal is **parked** (`reason="unresolved_wrapper"`) and retried by the
   worker exactly like a failed conversion — never posted as if it paid.
   Resolutions are cached per process (`clear_resolution_cache()`), bounded to 6
   links / 4 s each, and some shorteners answer `200 OK` with a meta-refresh body
   redirect, which the resolver also follows.
6. **Links written without `http://` were invisible.** `promote_scheme_less_links()`
   now rewrites known hosts (`flipkart.com/…`, `amazon.in/dp/…`, `amzn.to/…`,
   `myshop…`) into real URLs at a word boundary before classification, so a
   scheme-less Amazon link is retagged and a scheme-less Flipkart link is
   converted. Prose ("available on amazon.in"), e-mail addresses and longer
   domains are left alone.
7. **A wrapper carried by the source post is not OURS.** Our own Bitly wrappers
   are minted *after* rendering, so the guard now takes the untouched source text
   (`source_text=`) and refuses to count a short link that was already in it —
   closing the hole where `bit.ly/3xyzFlip` (someone else's link) was reported as
   "OUR link present" and the post went out free.
8. **Two-product deals no longer borrow each other's short link.** The offline
   heuristic only maps an opaque Amazon short when the deal carries exactly one
   distinct ASIN; otherwise resolution is left to the per-link network resolver.

### 1d. "Add my Telegram channel, save, and it must post with MY tag" (`8f…`+, this round)

Driving the operator's exact flow end-to-end (dashboard → save → pipeline →
dispatch) exposed four ways a freshly added channel could end up posting nothing
or posting under the wrong tag:

9. **A channel added without ticking the network boxes saved every network OFF.**
   `add-manual-channel` read an absent checkbox as `False`, so a channel added
   with only its `@username` had `allow_amazon=0, allow_earnkaro=0, allow_hypd=0`
   → every deal was skipped (`per_channel = skipped`) and the channel looked
   fine on the dashboard. Absent now means **inherit the creator's routing**; the
   forms send an explicit `0/1` hidden pair, so an operator can still switch a
   network off on purpose. The same tolerance was added to Easy Setup.
10. **Easy Setup silently swallowed every message.** Only the creator page
    rendered flashed messages, so "saved" and "the tag was refused" looked
    identical on the Easy Setup screen. The screen now shows the confirmation,
    the warnings and the errors.
11. **A tag the renderer cannot use was silently replaced.** The Associates
    format only accepts `[A-Za-z0-9_-]{3,30}`; anything else (e.g. `"my tag"`)
    was swapped for the configured fallback while rendering, so the post went out
    under the wrong tag with no signal. Now:
    * the dashboard validates with the same rule as the renderer and refuses the
      save with a clear message (`"my tag" is not a usable Amazon Associates tag…`);
    * a usable-but-unusual tag is accepted with a warning (`does not look like a
      standard Associates tag`);
    * an **empty** tag is reported instead of hiding behind the fallback;
    * the pipeline logs `AMAZON TAG UNUSABLE` once per channel when it has to fall
      back.
12. **Saving now proves the outcome.** Every save (channel add, channel edit,
    Easy Setup) flashes the exact link the tag produces, generated offline with
    the same renderer + guard the pipeline uses:
    `Amazon deals will post as https://www.amazon.in/dp/B0D9P2M1PB?tag=<MINE> (verified ✅)`.
    The Easy Setup "what gets posted" preview shows the same proof line, and
    `/api/test-render-deal` now returns `our_links`, `unresolved_wrappers` and a
    `verdict` (`our_link_present` / `held_for_retry` / `no_our_link`).

Proof (tests, offline): a channel saved with just `@username` sits at
`status=ready` with all three networks on and the next deal is posted with the
creator's tag and the EarnKaro link; an unticked box is still an explicit OFF; a
bad tag is refused and **no channel is created**; the Easy Setup screen shows
`tag=<typed tag>` in the confirmation; an untagged creator logs the fallback
warning exactly once.

### 1e. Compact branded links — click opens the exact creator-tagged target (`current`)

The earlier tree already had optional first-party short routes, but an audit of
what counted as an "OUR" link found a correctness gap: the guard and Money Radar
trusted names such as `go.*`, `/amazon/…`, or `/m/…` without proving that the
link belonged to this deployment. A lookalike URL on another host could be
reported as earning. That is not acceptable for commission reporting.

This round makes compact links both shorter **and** verifiable:

13. **One simple domain setting:** put one operator-owned public HTTPS origin in
    `AFFILIATE_SHORT_LINK_BASE_URL`, for example `https://go.your-domain.in`.
    It is inherited by Amazon, HYPD and approved LehLah links; the older
    `AMAZON_SHORT_LINK_BASE_URL` / `MEESHO_SHORT_LINK_BASE_URL` remain optional
    per-network overrides. No VM IP, localhost, path, credentials or query
    string is accepted as the origin.
14. **Amazon broadcast / WhatsApp links are now compact:**
    `https://go.your-domain.in/a/<8-char-code>?tag=<creator-tag>`. The code is
    durable; it redirects with `302` only to its stored canonical
    `https://www.amazon.in/dp/<ASIN>?…&tag=<the-same-creator-tag>` target.
    The older `/amazon/<code>` route remains live, so existing posts do not
    break. Approval-channel links deliberately remain native Amazon URLs.
15. **No name-based trust:** a compact URL earns / passes the guard only when
    all facts agree: **configured HTTPS origin + exact route/code + durable DB
    record + safe target + exact tag/store**. A forged host, changed tag,
    extra attribution query, stale/deleted code or unsafe target is a hard guard
    failure, is sanitized before posting, and Money Radar counts it as a leak —
    never as OUR commission.
16. **Every saved/tested link is proof:** channel save and Easy Setup now show
    the actual compact Amazon URL (when configured) and `→ opens only <canonical
    target>`. `/api/test-render-deal` runs the same final compacting pass as the
    worker, allocates a local route only (no post) and returns that real clickable
    URL in `our_links`. The Setup Center shows **ACTIVE** vs **CANONICAL MODE**
    with the exact safe `.env` setting; `doctor --short-links` validates the
    local origin configuration without making an external request.
17. **All eligible existing OUR links stay short safely:** Amazon `/a/`, HYPD
    `/m/`, approved LehLah `/l/`; EarnKaro's `ekaro.in`/`fktr.in` output is
    already network-short and is not double-wrapped (so its publisher proof is
    not obscured). Raw/foreign/unresolved URLs are converted, held/retried or
    logged—not branded as ours.

Proof: six adversarial tests cover one common origin through the real pipeline,
Amazon code/tag redirect and legacy compatibility, incorrect-tag/open-redirect
attempts, forged/stale foreign routes, unsafe bases/targets, save/test preview,
and HYPD/LehLah target validation. Full suite: **380 passed**. This proves the
app's redirect and attribution logic locally; public DNS/TLS reachability still
requires the operator to point an owned hostname at the VM proxy and click one
returned link after deployment—do not claim external reachability before that
real check.

**VM enablement (private `.env`, never commit it):**

```dotenv
AFFILIATE_SHORT_LINK_BASE_URL=https://go.your-domain.in
# DASHBOARD_TRUST_PROXY=true only when one trusted TLS proxy forwards this host
```

Route that HTTPS host to the dashboard service, restart dashboard + worker, then
run `python3 -m influencer_hub.cli doctor --short-links`; save a channel or run
**Test a Deal**, open the returned `/a/<code>?tag=<creator-tag>` link once and
verify its redirect location has the identical creator tag.

Operator tool (read-only, nothing is posted): 

```bash
python3 -m influencer_hub.cli audit-links --text "🔥 deal … bit.ly/abc"
python3 -m influencer_hub.cli audit-links --file /tmp/post.txt --json
python3 -m influencer_hub.cli audit-links --offline --text "…"   # no network
```

It prints every link, its kind, where a wrapper really points, the text that
would be published and the verdict; exit code `0` = at least one of OUR links is
present, `1` = the worker would hold the deal. Audit result on the 55-shape
corpus: 8 unmonetised/unverified links before this round → 1 after, and that last
one (a HYPD store page with no `afflink` token) is *skipped*, never posted.


Two-cycle proof on a temp DB: cycle 1 with EarnKaro down → merchant-only deals
parked (`deferred queue: 2`), Amazon and raw-Meesho deals posted; cycle 2 with
EarnKaro recovered → `drain: 2`, posts carry
`fktr.in/ek402049?affExtParam2=5478322` and `ekaro.in/ek352378?affExtParam2=5478322`,
queue `0`.

---

## 2. Test and audit commands

From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
python3 -m influencer_hub.cli doctor --telegram-sources --short-links
```

The test suite on this revision is expected to report **380 passed**. The
source doctor is read-only: it enumerates joined dialogs, checks the same
selection rules as the worker, does not read message history, and does not
check or join invites. It requires valid Telegram credentials/session on the
VM. Its fallback warning is not a delivery guarantee.

The dashboard has the same bounded check under **Setup Center → Launch
readiness → Run live checks**. With the sample 3-source/236-dialog situation,
expect 3 unmatched private invite labels, fallback mode, and up to 236 eligible
joined dialogs selected (subject to output exclusions).

---

## 3. Apply on the VM

First back up the private `.env` and SQLite DB using the VM's normal backup
procedure. Never copy secrets into Git or chat. From the deployed checkout:

```bash
cd ~/Influencers-deals
# 1) source fallback + live source visibility (skip if already applied)
python3 patches/apply-private-source-fallback.py --check
python3 patches/apply-private-source-fallback.py --dry-run
python3 patches/apply-private-source-fallback.py --with-tests
# 2) link conversion fixes, retry queue, flow board (diffed against state 1)
python3 patches/apply-link-conversion-fixes.py --check
python3 patches/apply-link-conversion-fixes.py --dry-run
python3 patches/apply-link-conversion-fixes.py --with-tests
python3 -m influencer_hub.cli doctor --telegram-sources --short-links
python3 -m influencer_hub.cli audit-links --offline --text "Deal https://www.amazon.in/dp/B08XYZ1234?tag=old-21"   # expect exit 0 + [OUR] amazon
python3 -m pytest -q            # expect 380 passed
sudo systemctl restart influencer-deal-worker influencer-dashboard
systemctl is-active influencer-deal-worker influencer-dashboard
journalctl -u influencer-deal-worker -n 100 --no-pager
curl -s localhost:5000/api/flow | head -c 400
```

The second applier validates its bundled exact patch with `git apply --check`
before writing anything and refuses to run on a checkout that does not have the
first bundle installed (it prints that hint). Both are idempotent reporters:
`--check` exits 0 when the work is already in place and a plain repeat run exits
1 without touching a file.

The applier validates its bundled exact patch before writing. It aborts without
writing if the VM checkout differs; inspect the diff rather than forcing it.
If the code is already applied, `--check` exits successfully and a normal
second run is refused. `--with-tests` refreshes the verified tests and installs
the test-only DB-isolating conftest.

After restart, use Setup Center's live check and `/api/flow`. Confirm that the
worker is alive, source selection is non-zero, source `last_seen` advances, and
ready destinations show posts or explain failures. Check worker logs for the
one-time private-invite fallback warning. A warning is expected when the label
is only a label. It is not evidence that a deal was successfully delivered.

The worker can scan many joined dialogs sequentially. On a 236-dialog fallback,
monitor Telegram flood-wait logs, worker poll duration, and channel posting
filters. If only the three intended sources should be read, replace private
invites with public usernames where possible, or save each exact joined group
title as its label so fallback is not needed.

---

## 4. Affiliate and delivery verification

The source fix only addresses source selection. It does not repair missing
Telegram authorization, inactive sources, no ready destination channels,
creator/channel source filters, categories/schedules, disabled networks, or a
missing affiliate credential. If sources are selected but posts still do not
appear:

1. Read the source fallback / selector result in Setup Center or the CLI doctor.
2. Check worker state, source activity, destination readiness, and failures on
   `/api/flow`.
3. Check the worker log for pipeline filtering or delivery errors. After this
   branch, a quiet channel can also mean deals are parked for a conversion
   retry: read `deferred.waiting` on `/api/flow` and look for
   `EARNKARO UNCONVERTED` / `EARNKARO CONVERSION FAILED` lines, which name the
   links and tell you whether `EARNKARO_API_KEY` or the publisher id needs
   attention.
4. Confirm the EarnKaro token/publisher ID in Vault and run
   `python -m influencer_hub.cli verify-earnkaro` when eligible merchant links
   must earn. Amazon attribution uses each creator's configured tag; HYPD and
   EarnKaro use the configured central accounts per the existing account model.
5. Use a deliberate test post to verify destination permissions; the read-only
   source doctor does not send a post.

No software can guarantee a “perfect” stream: Telegram group content, account
permissions, external affiliate conversion, filtering, rate limits and network
availability are outside the app's control. The code now makes the source
selection failure visible and avoids silently returning zero for unmatched
named private invites.

---

## 5. Replayable work and safety

- `patches/private-source-fallback.patch` contains the source-visibility patch for
  the merged `c6fa901` release; `patches/link-conversion-fixes.patch` (diffed
  against that state) contains the conversion fixes, retry queue and docs.
- `patches/apply-private-source-fallback.py` and
  `patches/apply-link-conversion-fixes.py` support `--check`, `--dry-run`, and
  `--with-tests`; they use `git apply --check`, never reset the checkout, and
  never touch `.env`, the Telegram session or the SQLite DB.
- `patches/tests/` carries byte-identical test copies; `tests/test_patch_bundle.py`
  checks copies, patch presence, and applier state.
- The original commission/account/password/deal-flow work and its four
  historical appliers remain documented in `patches/README.md`; they are
  already in the current base release.
- This checkout did not contain a reachable `6b6c1a8` commit despite the earlier
  handoff message. The fix here was recreated on the session's required branch;
  verify `git status` before committing or opening a PR.
- Never run pytest against a provisioned/live DB: root `tests/conftest.py`
  redirects every test to `tmp_path`.

---

## 6. This session: main-channel → WhatsApp mirror + phone-code pairing

Two operator-facing additions on top of the release above (see `README.md` →
“Main channel → WhatsApp mirror” and “Pairing a creator's phone: QR or login
code” for the behaviour in full).

**1. Main Telegram channel → the creator's WhatsApp destinations (mirror).**
`influencer_hub/pipeline.py` now renders and delivers *every* destination
through one function (`_render_and_deliver_destination`), so a mirrored post
passes the same rendering, `commission_guard`, dedup (`deal_sig`, physical
identifier, 24h content hash, 12h near-duplicate), rate limits (20/hour,
5/10min) and 45–65s WhatsApp pacing as a direct post. The mirror pass runs
after the channel loop: a ready Telegram `broadcast` channel that really posted
the deal feeds the creator's ready WhatsApp destinations (group or Channel)
with the creator's own Amazon tag (channel override → creator tag → fallback).
It re-applies only the *display* filters the main channel already decided
(source label, posting window, category, price, tier); the destination's own
affiliate switches (`only_amazon`, `strip_amazon`, EarnKaro/HYPD off) and the
money guards still decide what may appear there. Posts are recorded with
`posts.mirror_source_id`, and `pipeline.mirror_plan(inf)` is the read-only truth
the creator page shows (sources, destinations, mirrored posts in 24h).

Switches: `.env` `MIRROR_MAIN_TO_WHATSAPP` (default `1`), the Setup Center
global switch (`mirror_main_to_whatsapp`), the per-main-channel
`Mirror: inherit / ON / OFF` select (`channels.mirror_whatsapp` = `''`/`1`/`0`),
and the Easy Setup checkbox. Mirroring is inert until the creator has a **ready**
WhatsApp destination, i.e. paired phone plus one successful test post — the
Pending-activation rule is unchanged.

**2. Phone-linking (OTP) pairing in the WhatsApp hub.** `POST
/sessions/:key/pairing-code {phone}` asks WhatsApp for the 8-character code
shown on the influencer's phone; `GET /sessions/:key/pairing` is the read-only
live view the dashboard polls. One code per socket attempt, re-issued after a
reconnect, cleared on link, never logged and never included in `/sessions`
listings. New dashboard route `POST /influencer/<id>/pair-wa-phone` validates
10–15 digits, stores the attempt (`wa_sessions.pairing_*`) and reports the
outcome on the creator page; `db.set_wa_session_status(key, "connected")` clears
the code.

**Verification performed in this session (sandbox, no VM changes):**

```bash
# WhatsApp hub unit tests (8 contracts incl. pairing-code lifecycle)
cd wa_hub && npm install --no-audit --no-fund && node --test

# Full Python suite against an isolated temporary DB
HUB_ENV=development HUB_DB_PATH="$(mktemp /tmp/influencer-hub-deploy-test.XXXXXX.sqlite3)" \
PYTHONPATH=. .venv/bin/python -m pytest -q      # 394 passed (380 pre-existing + 14 new)
```

New tests: `tests/test_mirror_to_whatsapp.py` (8), `tests/test_mirror_dashboard_switch.py` (3),
`tests/test_whatsapp_pairing_code.py` (5), plus 4 pairing tests in
`wa_hub/wa_manager.test.js`. Schema changes are additive and applied by
`db.migrate()` (`channels.mirror_whatsapp`, `posts.mirror_source_id`,
`wa_sessions.pairing_phone/pairing_code/pairing_error`); run the normal
deployment sequence and `db.migrate()` will add them to an existing DB.

**What the mirror deliberately does not do:** it never mirrors from a channel
that did not post the deal, never re-posts a deal a destination already
received, never re-applies a destination's display filters, and never posts a
link that fails the commission guard — a WhatsApp destination whose own policy
forbids the deal records `mirrored/skipped` in the log and on the page instead
of publishing a link that earns nothing.

---

## 7. This session, part 2: advanced dashboard + fully-easy setup

### `influencer_hub/readiness.py` — one checklist, three screens

A read-only engine that answers "is this creator ready, and what is the next
step?" for every creator, in onboarding order:

1. **Amazon tag** (own id vs. the hub-wide shared default — the DB always stores
   *some* tag, so "is it theirs?" is the honest question; a shared default is a
   warning, not a blocker, because the deal still earns)
2. **Approval channel** (optional: recommended, never required, so a creator who
   does not want a preview is not stuck at "not ready")
3. **Main channel** (required)
4. **WhatsApp channel link** (required: done once the destination is registered,
   even while it is Pending — pending-ness is step 6)
5. **Phone paired** (required; from the stored session row, or from the hub's
   live state when the caller overlays it)
6. **WhatsApp live** (required: the destination passed its visible Test post)
7. **Mirror** (main → WhatsApp actually copying)

`REQUIRED_STEPS` decides `ready`; the headline `next_step` is always a mandatory
one, so the UI never sends an operator after an optional extra first.
`global_checks()` adds the two hub-level steps (active deal source, worker
heartbeat). `overview()` also returns `actions` (priority list), `warnings`
(shared tag, saved-but-unpaired destination) and a `summary` with `percent`.

### Dashboard command center (`/`) and `/api/readiness`

The home page renders the checklist server-side and then refreshes itself every
20 s from `GET /api/readiness?live=1` (`?inf_id=` narrows it; `live=1` overlays
the hub's real session statuses through the existing loopback proxy). The API is
GET-only and never creates a session or sends anything; when the hub is down it
returns `live_error` and the saved state. `app.js` patches only the cells that
change (progress, worker chip, phone, live) and backs off on failure.

### Easy Setup (`/easy-setup`) is now the whole flow

- A pill strip of every creator (🟢 ready / 🟡 blocked) links to
  `?creator=<id>`, which prefills the form from the saved profile
  (`_easy_setup_values_from_profile`) — one screen to edit, not only to create.
- The focused creator gets a **step track**, the **next action** card, an inline
  **QR + login-code** form (`#qrbox` / `#pairbox` reuse the existing read-only
  pollers) and, once the phone is linked, either the **Send Test post** button
  for the Pending destination or — when no WhatsApp channel exists yet — a
  one-click **Create WhatsApp Channel** action that builds it from the creator's
  own linked account (`create-newsletter` now also honours `next`).
- `_safe_next_url` / `_redirect_after_action` let those actions post from Easy
  Setup and land back there with their status (`wa_pair_status`, `test_status`),
  while ignoring an off-site or scheme-relative `next`. The older behaviour
  (no `next` → creator page with the same status) is unchanged and still tested.

### Setup Center

Each creator-directory card now shows its next step (or "ready · N/M steps ·
🔁 mirror on") and flags a shared default tag, matching the dashboard.

### Verification

```bash
HUB_ENV=development HUB_DB_PATH="$(mktemp /tmp/influencer-hub-deploy-test.XXXXXX.sqlite3)" \
PYTHONPATH=. .venv/bin/python -m pytest -q      # 413 passed (397 + 16 new)
cd wa_hub && node --test                       # 8 tests / 8 pass
```

New file: `tests/test_setup_readiness.py` (16 tests) — the engine, the
command center, the JSON API (narrowing, live overlay, hub failure, read-only
guarantee), Easy Setup (pill strip, prefill, inline tools gated on the phone
link, off-site `next` rejected) and the Setup Center cards.

---

## 8. This session, part 3: 1000+ creators (scale) and bulk paste

Everything below was verified in the sandbox against a hub with **1000 creators
and 3000 channels** (seeded through the real code path).

**Bulk paste (`POST /easy-setup/bulk`).** `parse_bulk_creators()` accepts one
creator per line — `name, tag, main, approval, whatsapp` — separated by comma,
tab, `|` or `;`, skipping blanks/`#` comments, and understands either a short
header row or the legacy CSV column names. An uploaded CSV/TXT file is parsed by
the same function. `BULK_MAX_ROWS = 2000` per submit. Rows are applied through
the *same* upsert the single form uses (`_apply_creator`), so re-pasting updates
by name instead of duplicating. Per-line results are collected (`created`,
`updated`, `failed: <reason>`) and shown on `/easy-setup?bulk=<token>`; the
result list lives in the dashboard process (`BULK_RESULTS`, last 5, 200 rows
kept) because a thousand rows do not belong in a session cookie. WhatsApp values
that are already JIDs are saved Pending; invite links are reported as
"paste it after pairing" so a bulk import never makes N hub calls.

**Readiness at scale.** `readiness.overview()` now reads channels, sessions and
mirror counts in one query each (`db.mirrored_post_counts()` is new; the mirror
setting is read once and passed down, so a per-channel "inherit" no longer costs
a query per channel), and it supports `query`, `state`
(`ready|blocked|mirroring|paused|warning|no_phone|no_whatsapp|no_main|shared_tag`),
`limit`/`offset`, `max_actions` and `max_warnings`. `summary`/`globals` always
describe the whole hub; `creators` is the requested slice, with `total`,
`matched`, `returned` and `warnings_total` alongside. A 400-creator overview
issues 7 database reads (asserted in `tests/test_bulk_setup_scale.py`).

**Screens:**
* `/` command center gained a search + state filter form, honest counters
  ("N of M creators match"), paging, and its live refresh now asks for exactly
  the rows on screen.
* `/easy-setup` gained the 📦 bulk paste card and a **paged** creator strip
  (`pill_q`, `pill_offset`), with the focused creator always included even when
  the current search would hide them.
* `/setup` directory renders 60 cards per page (`dir_offset`), the simulator's
  profile list is capped at 200, and Easy Setup's per-channel table shows 40
  rows with a pointer to `/api/flow` for the rest.
* `db.ensure_schema()` runs the schema/migration once per database file instead
  of once per insert, and the bulk upsert reads the profile list once for the
  whole paste: a 1000-line import takes ~13 s (was ~21 s) and is a one-off.

**Tests:** `tests/test_bulk_setup_scale.py` (10 tests) — bounded query count,
paged/filtered dashboard + API, pill paging/search, bulk create/update/report,
CSV upload, invalid rows, 1000-line import with a page-speed check, and the
"no screen renders the whole hub" rule. Full suite: 424 passed; wa_hub `node
--test` 8/8.

---

## 9. This session, part 4: the self-check

`influencer_hub/selfcheck.py` runs ten read-only checks and returns
`{ok, verdict, checks, summary, next}` where verdict ∈ `ready|attention|fail`:

`schema` (columns + the mirror-count index), `telegram` (credentials **and** a
session file), `dashboard` (admin password + production cookie flags), `worker`
(heartbeat state, last poll, last error code), `sources` (at least one active),
`amazon` (fallback tag set and how many creators are still on the shared id),
`central` (EarnKaro key/publisher + HYPD store), `creators` (ready/blocked from
the readiness engine), `mirror` (global switch + how many creators actually
mirror) and `whatsapp` (the only network call, `live=True`, a loopback GET to
the hub `/health`).

Design rules worth keeping: every non-green check carries the fix text; results
are JSON-serialisable and contain **no credential values** (tests assert a
stored key never appears in the report or the page); the checks never write —
one test snapshots the database before/after; and `live=False` (the default for
the page) makes exactly zero network calls, so a slow hub cannot hold up the
dashboard.

Surfaces: `GET /api/self-check` (`?live=1`, `?inf_id=<id>`) and the dashboard's
🧪 Self-check panel, refreshed by `app.js` every 30 s. `VERIFICATION.md` is the
VM runbook built from the same checks.

Tests: `tests/test_self_check.py` (9 tests) — honesty (empty hub ≠ ready, dead
worker and shared tag are amber), secret non-leakage, live-only hub failure,
read-only guarantee, API shape/404, and the rendered panel naming the fix.

---

## 10. This session, part 5: the ops layer (digest, alerts, exports)

`influencer_hub/ops.py` adds the three pieces that make a large hub operable:

* **`digest(hours=24)`** — one JSON object (posted/failed/skipped/mirrored, the
  conversion-retry queue, worker, sources, creator readiness, self-check verdict,
  top failure reasons) plus `render_digest()` for the ops Telegram card. Failure
  counting needed a real timestamp on failed attempts, so `posts.created_at` was
  added (schema + migration, filled by `record_post` for every attempt); the
  window is `COALESCE(NULLIF(posted_at,''), created_at)`. The digest tolerates a
  fresh/unmigrated database instead of raising (it must be printable from cron).
* **`alert_decision()` / `maybe_alert()` / `render_alert()`** — a pure policy
  (got worse → alert; same verdict → cooldown, default 180 min; recovered →
  one message; green → silence) plus the function that stores `ops_last_verdict`
  / `ops_last_alert_at` and posts to `OPS_TELEGRAM_CHANNEL`. No channel
  configured ⇒ nothing sent *and nothing recorded* (so the first configured run
  still alerts). Every message carries the failing checks and their fixes, never
  a secret.
* **`readiness_csv()` / `bulk_template_csv()`** — the checklist as one row per
  creator (filters honoured) and the exact columns the bulk paste accepts. A
  test round-trips the template through `parse_bulk_creators()`.

CLI: `python -m influencer_hub.cli self-check [--live] [--json] [--alert
[--force]]` (exit code 0 only when the verdict is `ready`) and `digest [--hours
N] [--send] [--json]`. Every check in `run_checks()` is guarded, so a missing
table shows as a failed check with a fix instead of a traceback in a cron log.

Dashboard: the 📈 **Ops digest** card (metrics + the two CSV downloads + the
copy-paste commands), live-refreshed from `GET /api/digest?hours=24`; plus
`GET /api/readiness.csv` and `GET /api/easy-setup/template.csv`. README has the
"Ops" section and VERIFICATION.md a step 5b with crontab lines.

Tests: `tests/test_ops_digest.py` (12) — digest window/attempt counting, card
shape, alert policy table, one-send-then-cooldown, recovery, no-channel
behaviour, forced runbook alerts, CSV contents/filters and template round-trip,
CLI exit codes + JSON, and the HTTP surfaces. Full suite: 445 passed.

---

## 11. This session, part 6: history, incidents, metrics, backup/restore

Advanced round on top of `ops.py`:

* **History** — `ops_snapshots(day PRIMARY KEY, captured_at, posted, failed,
  skipped, mirrored, attempts, ready, blocked, setup_percent, verdict, notes)`,
  written by `ops.record_snapshot()` (upsert per day, pruned after
  `SNAPSHOT_RETENTION_DAYS=400`), exposed by `cli snapshot --days N --note`,
  and written automatically by `cli digest --send`. `ops.trend(days)` reads it
  back (delivery %, green streak, ±% vs yesterday) and `ops.sparkline()` draws
  the `▁▅▇█` line used by both the digest card and the dashboard.
  Digest additions: `trend`, `creators.blocked_by_step`, `creators.biggest_blocker`,
  `creators.top` (leaderboard via `db.top_posting_creators`).
* **Incident continuity** — settings `ops_incident_started_at` /
  `ops_incident_alerts` / `ops_incident_verdict`; repeat alerts read
  `(open 4h · alert #2)`, recovery closes with a duration, `humanize_minutes()`
  formats them, and the first alert of a fresh incident stays short.
* **Metrics + audit** — `ops.metrics_text()` (Prometheus) is served at
  `GET /api/metrics`; `ops.posts_csv()` at `GET /api/posts.csv?hours=` falls
  back to `db.list_recent_posts()`.
* **Backup/restore** — `ops.backup_database(out, keep)` (SQLite online backup +
  JSON sidecar + retention that never deletes the file it just wrote),
  `ops.inspect_backup()` (validates the four required tables), and
  `ops.restore_database(file, dry_run)` (keeps `*.pre-restore-*`, deletes stale
  `-wal`/`-shm`, re-runs `migrate()`).
* **Self-check grew an eleventh check** — "Ops history" warns when the table is
  missing or no snapshot has been written in 48 h, with the cron line as the fix.
* **Dashboard** — the ops card now carries the trend sparkline, the biggest
  blocker (step labels in plain words), the leaderboard line, a Posts-log CSV
  button, a metrics link and the new commands in its `<details>` block; the
  poller refreshes trend/blocker/failures too.

`db.py` gained `record_ops_snapshot`, `list_ops_snapshots`,
`prune_ops_snapshots`, `top_posting_creators`, `list_recent_posts`,
`snapshot_table_exists` (all tolerant of a missing table).

Plus the two read-only dashboard touches: a per-day bar chart in the ops card
(server-rendered SVG-free markup, re-rendered by the poller) and
`GET /api/self-check?preview_alert=1`, which renders the message the ops channel
*would* receive using the pure policy only — it never sends and never moves the
cooldown or the incident state. `render_alert` now also has headers for the
quiet reasons ("not green yet — the next message is held for 2h 21m").

Tests: `tests/test_ops_advanced.py` (17) + the updated `tests/test_ops_digest.py`
(12) and `tests/test_self_check.py` (10). Full suite: 463 passed.

---

## 12. This session, part 7: the Amazon watcher

`influencer_hub/amazon_watch.py` (read-only, no network, no writes):

* `scan(text)` — every Amazon link in a source post with its shape
  (`product` / `short` / `search` / `store`), ASIN, marketplace, the tag already
  on it (the *source's* tag) and the planned action (`retag`, `resolve`,
  `carry`). Scheme-less links are promoted first, exactly like the pipeline
  does, so they are never missed.
* `plan(text, tag, role)` — one creator's rendered post plus per-link checks;
  `fleet_plan(text, influencers)` — the same for every creator in one pass
  (pure text, ~0.8 s for 1000 creators, zero writes) and `all_own_tags`.
* `attribution_report(hours)` — reads `posts.deal_text` for the posts that were
  really sent and counts links by whose tag they carry: `own_tag`,
  `foreign_tag` (a leak, with creator/tag/URL named), `untagged`, `short_links`.
* `render_report(data)` — one plain line for the ops card.

Surfaces: `cli amazon-scan` (`--text/--file`, `--creator`, `--fleet`,
`--report`, `--hours`, `--role`, `--check`, `--json`; exit 1 when `--check`
fails), `GET /api/amazon-report?hours=`, `GET /api/amazon-scan?text=&creator=`
`&fleet=`, a live line + a paste-and-scan block on the ops card, the digest's
`amazon` section (with a card line) and the twelfth self-check
("Amazon links in real posts": FAIL on a foreign tag, WARN on untagged links or
short codes, SKIP when nothing was sent yet).

Two CLI bugs found while testing: `--limit 0` read zero posts (0 now means the
default cap), and `--json --check` mixed the verdict line into the JSON.

Tests: `tests/test_amazon_watch.py` (15). Full suite: 478 passed.

---

## 13. This session, part 8: team access (add-only) and admin 2FA

`influencer_hub/security.py` (stdlib only) holds the primitives:

* `password_matches` / `admin_password_matches` / `team_password_matches` —
  salted SHA-256 digests compared with `hmac.compare_digest`, so string length
  is not observable. `strong_password()` is the 16-character floor.
* RFC 6238 TOTP built on `hmac` + `struct`: `new_totp_secret`, `totp_code`,
  `verify_totp` (±1 step of drift), `totp_uri`, `totp_enabled`.
* Throttling in the database (`login_attempts`): `block_state`, `note_login`
  (a success clears the budget), `LOGIN_MAX_FAILURES=5` per 15 minutes, scopes
  `admin` and `team` counted separately.
* `audit()` plus `TEAM_ALLOWED_ENDPOINTS`; `team_quota()` (per-member hourly cap).

`db.py`: `influencers.added_by` (schema + migration), `add_influencer(added_by=)`,
`add_bulk_influencers(added_by=)`, `list_influencers(added_by=)`, `record_audit`,
`list_audit`, `count_audit_since`, `note_login_attempt`, `count_login_failures`,
`clear_login_failures`, `prune_login_attempts`, tables `team_audit` and
`login_attempts` (+ indexes).

Dashboard:
* Session roles (`dashboard_role`, `team_name`); `_session_role()` treats a
  pre-upgrade session as admin so nobody is locked out.
* Admin `/login` gained the TOTP field and the database-backed lockout, and
  writes `admin_login_ok` / `admin_login_failed` / `admin_login_blocked`.
* `/team/login` (name + team password), `/team` (add form, paste-many, "creators
  you added" with next steps, own activity), `/team/add` (add-only: an existing
  name is reported, never updated; hourly quota; every add audited, `added_by`
  stamped) and `/api/team/creators` (own rows only).
* The guard in `before_request` refuses every endpoint outside
  `security.TEAM_ALLOWED_ENDPOINTS` for the team role (JSON 403 for `/api/*`,
  redirect to `/team` for pages) and audits the attempt; unauthenticated
  `/team*` and `/api/team` requests go to the team sign-in.
* Generic change audit in `after_request`: one `change:<endpoint>` line for every
  successful admin mutation (exempt list for auth/team endpoints), skipped when a
  request was stopped for re-authentication because it changed nothing.
* `/setup` grew the **👥 Team access & audit** card (`_team_access_snapshot()`):
  team link, configured/2FA pills, members seen in the log with add counts, the
  hourly limit, the 40 newest audit rows and a password-strength warning.
* `cli totp --new|--check|--uri|--secret`; the self-check has a thirteenth item
  ("Admin & team access", WARN naming the exact command to fix).

Templates: `team_login.html`, `team.html`, `login.html` (2FA field + team link),
`setup.html` (audit card).

Tests: `tests/test_team_access.py` (20) — two doors, cross-password rejection,
lockout in the database, add + stamp + audit, "mine only", existing-name refusal,
eight hand-crafted edit posts refused, admin pages/APIs refused for the team,
paste-many, quota, admin edit + audit, "stopped request logs nothing", 2FA
sign-in (wrong and right code), TOTP clock/drift, the CLI, the self-check item and
the setup card. Full suite: 500 passed.

## 14. This session, part 9: the creator inventory (export everything, paste it back)

The owner asked for the whole hub as a spreadsheet — every creator with the date
they were created, who added them, their channels and ids — and for the edited
file to come back in safely. That is `influencer_hub/inventory.py` (stdlib only):

* `COLUMNS` (29) — `creator_id, name, added_by, created_date, created_at, active,
  ready, amazon_tag, tag_source, approval_channel, main_channel, whatsapp_channel,
  wa_status, phone_number, mirror_whatsapp, allow_amazon, allow_earnkaro,
  allow_hypd, only_amazon, hypd_store_id, categories, posting_schedule,
  price_filter, notes, next_step, next_action, posts_24h, mirrored_24h,
  last_post_at`. `EDITABLE` is the subset an import may change.
* `rows(member=, query=, limit=)` / `csv_text()` — bulk queries only (channels,
  sessions, mirror counts, post activity and the hub-wide mirror default are read
  once for the whole hub), `member=` narrows to one team member's additions.
* `plan_import(text, create=)` — pure: matches by `creator_id` then name, validates
  each Amazon tag with `link_router.amazon_tag_is_usable`, and returns
  `{operations, changed, unchanged, created, issues, ok}` where every operation
  carries `changes` + `before` and `channels` + `channels_before`, so the UI can
  show `field: old → new`. `edits(operation)` renders that list for the CLI and
  the dashboard from one place.
* `apply_import(plan, actor=, ip=)` — the only writer; audits `creator_updated` /
  `creator_created` per row and returns a per-row report.
* `stash_write` / `stash_read` / `stash_prune` — the pasted file lives in a
  private server-side stash (0600, 3 h TTL); only a random token is in the
  cookie.

Datetime columns were widened earlier in the round: `readiness.creator_readiness`
returns `added_by`, `created_at`, `created_date`, and `ops.READINESS_COLUMNS`
(the checklist CSV) carries them too.

Safety rules an operator can rely on: an **empty cell never writes** (so a
half-filled spreadsheet cannot wipe a hub), a single `-` clears exactly that
field, a name that is not in the hub is only created when "add missing rows"
(`--create`) is on, an unusable tag or an unmatched row is reported with its line
number while the rest of the batch still applies, and a preview never writes.

Surfaces: `/setup` → **📦 Creator inventory** card (`id="inventory"`) with the
export buttons, the paste/file form, the preview table (`field: old → new`),
"✅ Apply the changes", the issues list and the last-import report; the console
button **⬇️ Full inventory (CSV)**; team page "Added on"; the setup directory
shows "Added <date> by <member>". Routes: `GET /api/inventory.csv`
(admin-only, `?member=`, audits `inventory_exported`),
`POST /setup/inventory/import` (preview → stash → apply; audits
`inventory_imported`). CLI: `export-inventory --out/--member`,
`import-inventory --file/--apply/--create/--actor/--json`.

Two bugs this round found and fixed (both in the round's own code):

1. `db.channel_post_activity()` was called inside the per-creator loop — the
   1001-creator export took **18 s**. One query for the hub, plus the
   hub-wide mirror default and the channel map read once (`plan_import` opened
   two connections per row): export **0.18 s**, full-file preview **0.06 s**.
2. The pasted file was kept in the Flask session cookie. A 256 KB paste produced
   a **23 349-byte cookie** (Werkzeug: *"the cookie is too large … browsers may
   silently ignore cookies larger than this (4093)"*) — a real browser would drop
   it, lose the preview and sign the owner out. The file now lives in the stash
   and the cookie is **558 bytes**; an expired stash says so instead of applying
   nothing. A regression test asserts the cookie stays under 4 KB and that the
   stashed file is mode 0600.

Tests: `tests/test_inventory.py` (25) — every column present with the date and
the member, `?member=` narrowing, Excel-safety (no secrets/BOM), a "no edit
changes nothing" round trip, exact `old → new` reporting, empty-vs-`-`, mirrors,
switch flip, bad rows with line numbers, unknown ids, missing name/id header,
`/api/inventory.csv` admin-only (+ team 403), the setup preview → apply flow,
`/setup` card, the checklist CSV columns, the CLI, failures not stopping the
batch, the team page "Added on", the 300-creator query-count gate and the stash
(cookie size, TTL, 0600, size limit). Suite: **526 passed** (see VERIFICATION §1;
`HUB_SKIP_DOTENV=1` makes the documented run ignore the VM `.env` completely,
which is what the old "blank every credential" form did not achieve: blanking
`TELEGRAM_API_ID` also disabled the documented default and broke three tests on
the VM).

## 15. This session, part 10: the money map, one-line adds, and add defaults

Three things, all aimed at the same sentence: **easy for the team, impossible to
get somebody's money wrong, advanced for the owner.**

### `influencer_hub/guard.py` — the assignment guard (new)

* `channel_key(platform, identifier)` normalises `@handle`, `t.me/handle`,
  `https://t.me/Handle/`, a trailing query and `telegram.me/`, and keeps WhatsApp
  newsletter / group / phone keys in their own namespace.
* `scan(influencers=None, channels=None)` — read-only, worst-first findings:
  `tag_collision` (⛔ two creators on one tag), `tag_invalid` (⛔ unusable tag),
  `tag_shared_default` (⚠ still on the hub-wide tag), `channel_collision`
  (⛔ one destination on two creators), `role_clash` (⚠ same channel as preview and
  main), `name_collision` (⚠ two profiles, one name). Every finding carries a
  label, a detail that names the value, the creators (id/name/url) and a fix.
* `counts`, `is_clear` (high == 0), `summary_line`, `render_report`.
* `GuardIndex` — `from_db()`, `note()`, `preflight(name, tag, ignore_id,
  approval, main, whatsapp)` → `{errors, warnings}`, `explain()`. Errors stop a
  write; warnings are honest notes (missing/shared tag, duplicate name). Used
  **while adding**, so a bad row is refused by name instead of written.
* 1000 creators + 1000 channels: `scan` 45 ms, `from_db` 15 ms, one preflight
  < 0.1 ms — cheap enough for a page and a cron.

### `influencer_hub/creators.py` — the tolerant parser (new)

`parse_line` / `parse` / `preview` / `explain`. Delimited rows keep the documented
order (`name, tag, main, approval, whatsapp, phone` then `categories, schedule,
price_filter, notes`); a header row re-maps the columns; a line with no separator
is classified token by token (name first, then tag / Telegram / WhatsApp / phone /
`key=value`). Values in the wrong column are moved with a note in `how`; a plainly
wrong tag stays wrong so the row is refused with the reason. `preview` returns
`{rows, total, ready, problems, unparsed}` with `problems` per row.

`dashboard/app.py:parse_bulk_creators` is now a two-line delegate to
`creators.parse`, so the dashboard, the team page and the CLI cannot disagree.

### `influencer_hub/presets.py` — what every new creator starts with (new)

`load/resolved/save/apply_to/describe/summary_line`, stored as JSON in
`global_settings['add_defaults']` (travels with the DB, no `.env` edit). Keys:
`allow_amazon/earnkaro/hypd`, `only_amazon`, `categories`, `posting_schedule`,
`price_filter`, `mirror_whatsapp`, `hypd_store_id`. Precedence everywhere: pasted
row → form on screen → presets → code defaults. `save` audits
`add_defaults_changed`. **An untouched hub behaves exactly as before** (the code
defaults are the same values), which is what keeps the old tests green.

Wired in: `_easy_setup_defaults()` now starts from `presets.resolved()` (a switch
the screen does not send inherits the preset; a switch the screen sends as 0/1
still wins for that import), `team_add`'s defaults come from
`_new_creator_defaults()`, and `_apply_creator` writes the row's
`phone_number/categories/posting_schedule/price_filter/notes` (on an update only
when non-empty, so a channel-only paste never wipes a saved field).

### Surfaces

* `/setup → 🛡️ Money map` (`id="money-map"`, findings table with linked creators
  and the fix, pills for high/medium/creator count, JSON link) and
  `/setup → 👥 New-creator defaults` (`id="add-defaults"`, one form that saves
  every preset, with the same one-sentence summary the team sees).
* Routes: `GET /api/assignments` (admin-only JSON), `POST /setup/add-defaults`,
  `POST /team/preview` (read-only, team-allowed, added to
  `security.TEAM_ALLOWED_ENDPOINTS`).
* Team page: the presets sentence, the "paste anything" examples, and
  **🔎 Check these lines first** (inline JS → `/team/preview`, writes nothing).
* CLI: `assignments [--json] [--fix-hint]` (exit 1 when money is mixed),
  `add-defaults --amazon/--earnkaro/--hypd/--only-amazon/--mirror-whatsapp
  on|off --categories --hours --price --store [--dry-run] [--json]`.
* Self-check: a fourteenth item, **Money map (tags & channels)** — amber with the
  value and both creators when something would pay the wrong person, green with a
  tidy-up note count otherwise.
* Inventory: `plan_import` now returns `warnings` (the money map read once), so an
  edited spreadsheet that would leave two creators on one tag is shown before
  Apply.
* `dashboard/templates/team.html`, `setup.html`; `dashboard/app.py` team +
  easy-setup add loops run the guard and audit `add_blocked_duplicate`.

Tests: `tests/test_guard.py` (33) and `tests/test_smart_add.py` (26). Live-verified
on a 60-creator demo hub: the refusal, the preview JSON, the defaults save, the
money-map API/CLI and the self-check item (VERIFICATION §0c).

## 16. This session, part 11: guided setup and the setup age (creator → channels → deals → go live)

The owner asked for the easiest possible setup: open the dashboard, paste the
channel links, connect WhatsApp, pick the categories, post — and, beside every
creator, *when* they were set up and *how long* ago. Both are now in the tree.

### `influencer_hub/age.py` (new)

Pure stdlib, no database, no query per creator.

* `parse_moment(value)` → aware `datetime` (accepts `YYYY-MM-DD HH:MM:SS`,
  `T`-separated ISO, date-only, `datetime`), `None` for anything unreadable.
* `days_since(value)`, `human_age` / `human_age_from_days` — the **short** scale:
  `today`, `1 day`, `6 days`, `1 week`, `4 weeks`, `1 month`, `11 months`,
  `1 year`, `1 year 3 months`, `2 years 6 months`. Thresholds: <7 days, <30
  weeks, <345 days → months, else years + months (`years_and_months()` rounds the
  remainder and rolls a rounded 12 months into the next year, so “12 months” and
  “0 years 11 months” can never be printed).
* `long_age` / `long_age_from_days` — the **long** scale, two units: `1 week 6
  days`, `1 month 29 days`, `1 year 3 months`.
* `setup_label(created_at, member=)` → `Set up 1 year 3 months ago · 2025-07-14 ·
  by Mohan`, `Set up today · 2026-10-13`, `Set up date unknown`.
* `summarise(values)` → `{count, oldest_days, newest_days, oldest, newest,
  this_month}`; accepts stored timestamps **or** whole profile dicts (`_moment`),
  so a caller never builds a throwaway list.
* `phrase(age)` → `today` stays, everything else gets `… ago` (registered as the
  Jinja global `age_phrase`, so no template glues “ago” on by hand).

`readiness.creator_readiness()` now also returns `age_days / age / age_long /
setup_label / created_date / added_by` on every creator card, which is what
`/`, `/setup`, `/easy-setup`, `/team` and the wizard read.

### `influencer_hub/onboarding.py` (new)

The wizard's brain, no Flask in it:

* `STEPS` — creator → channels → categories → post, with `label`/`icon`/`what`;
* `CATEGORY_CHOICES` (12, mirrors the creator page) + `CATEGORY_KEYS`,
  `selected_categories()` / `category_text()` (`''` = all categories);
* `checkpoint(profile, channels, wa_status)` → `{done, current, current_number,
  finished, total, percent, ready, review}` — `channels` is done when a main
  channel is `ready`, `post` when the creator is active **and** has that channel;
* `review_lines()` — the step-4 table: creator, their own tag, approval, main
  (+ mirror note), WhatsApp + live status, categories, hours, price, set-up line,
  each with `ok`;
* `progress_text()` → `3 of 4 steps done · next: Deals & hours`.

### Routes and page

* `GET /guided` — one page, four cards, the rail always showing all four steps.
  Works without JavaScript (plain forms; JS only moves the highlight and drives
  the optional pairing panel).
* `POST /guided/creator` — creates or resumes (a name already in the hub resumes
  that creator instead of making a twin); a duplicate tag/channel is refused
  **naming the owner** via the guard preflight; creator stamped with the actor.
* `POST /guided/channels` — approval / main / WhatsApp in any paste shape, both
  Telegram channels inheriting the one tag, mirror switch, WhatsApp invite link
  kept in the session until the phone is paired (with a flash saying so);
  pairing panel reuses the read-only `GET /api/wa/<key>/qr|status|pairing`
  passthrough and `POST /influencer/<id>/pair-wa`.
* `POST /guided/categories` — category picker + free-text field, hours, price
  filter, the four switches, mirror; never leaves a creator with no network.
* `POST /guided/finish` — activates, clears the wizard session and points at
  “Send a test deal now” (`/influencer/<id>/trigger-hourly-loot`) and Test a Deal.
* **Creator precedence**: the form's `creator` field wins over `?creator=` over
  the session, so two open tabs can never write into the wrong creator.
* **Team role**: `guided_*` endpoints are in `security.TEAM_ALLOWED_ENDPOINTS`,
  and `_guided_owned()` allows a team member only the creator **this run** created
  (`session["guided_fresh"]`, set at step 1, cleared by Go live) — walking the
  flow for anybody else's creator, or coming back later to change one that is
  already saved (even their own), is refused before any write and audited
  `team_blocked_route`. That matches `team_add`, which reports a name already in
  the hub instead of updating it. The owner is never restricted.
* Every save is audited: `guided_creator_created`, `guided_channels_saved`,
  `guided_deals_saved`, `guided_finished`.

### Everywhere the age now shows

* `/` command center: a **Set up** column (`2 years 6 months` · date · *by Mohan*)
  plus chips *oldest … · newest …* and *➕ N added in the last 30 days*, with a
  line under the chips naming those creators, their age and who added them.
* `/setup#creator-directory`: *Added 2026-08-10 by Mohan · 🕒 2 months*, a hub line
  *“Setup ages: oldest … ago · newest … · N added in the last 30 days”*, and a
  server-side **sort** (`oldest`/`newest`/`name`/`name_desc`) plus **`since`/
  `until` date filters** (a row with an unreadable date is never filtered away).
* `/easy-setup`: the hub line in the strip header, and each creator pill carries
  its age; the pill search now also matches tag, channel ids, phone and notes.
* `/team`: an age column for the member's own creators + a guided-setup link and
  a sentence stating what the team may and may not do.
* `/api/inventory.csv`: two new columns, `setup_age` and `setup_age_days`
  (header is now 31 columns) and `inventory.rows()` grew `query` / `since` /
  `until` / `sort`.

Tests: `tests/test_age.py` (13) and `tests/test_guided_setup.py` (28), plus the
scale-budget note in `tests/test_bulk_setup_scale.py`. Full suite **652 passed**
(`HUB_SKIP_DOTENV=1`) after the own-tag round (§17). Live-verified end to end
on the demo hub — the four saves, the refusal by name, the team hijack refused
with the owner's channels unchanged, and the page timings (VERIFICATION §0b).

## 17. This session, part 12: the own-tag proof and the hold

The operator's ask: "akkada tag lo em tag isthe valla channel ah channels loney
valla tag tho links cheyali … vallaki commission miss avvakunda carefulga cheyu
… ilaga 1000+ influencers ki kuda cheyali." Whatever tag is saved for a creator,
their channels post under that tag — and a link that would carry anybody else's
id is never published, at 1000+ creators too.

### The rule, in one place

* `influencer_hub/tag_proof.py` (new) resolves the tag chain — channel
  `amazon_override_tag` → creator `amazon_tag` → `config.AMAZON_ASSOCIATE_TAG` —
  and calls that last one **not an own tag**: the database fills a blank tag with
  it (the shared-default trap), so a value equal to the hub-wide default is
  reported `shared_default` and `own: false`. `signing()`, `own_tag()`,
  `tagged_link()`, `verify()`, `fleet()`, `holds()`, `describe_fix()` and
  `url_summary()` are the public surface; the pipeline, the dashboard, the CLI
  and the self-check all read the same answer. It deliberately does **not**
  import `pipeline` (the pipeline imports it): `ACTIVE_CHANNEL_STATUSES` is
  duplicated and `tests/test_tag_proof.py` fails if the two ever drift.
* `influencer_hub/pipeline.py` — the **own-tag gate** in
  `_render_and_deliver_destination`, just before the final render. When the
  channel has no own tag and the deal carries an Amazon link, the destination is
  not rendered: it warn-logs "OWN-TAG HOLD", parks the deal (`db.defer_deal`,
  reason `no_own_amazon_tag`, 300 s backoff), audits `amazon_tag_hold`
  (actor `pipeline`, target = the creator) and returns
  `skipped:no_own_amazon_tag`. The gate has **no `role != "approval"` exemption**
  on purpose: an approval channel is still that creator's channel and its link is
  the one being approved.
* `influencer_hub/worker.py` — `drain_deferred_deals` keeps that status in the
  retry queue (bounded by `max_attempts`) instead of deleting the row as
  "processed", and keeps the reason `no_own_amazon_tag` on the row so the board
  still says "paste the tag" rather than "unconverted". The first pass after the
  tag is saved posts the deal, signed with the creator's own tag.
* `config.REQUIRE_OWN_AMAZON_TAG` (default **on**) is the single escape hatch for
  a hub that really runs one shared account.

### What the owner sees

* `cli tag-proof [--creator N] [--json]` — the fleet or one creator, exit 1 while
  anything is held; `cli assignments` still covers tag/channel collisions.
* `/api/tag-proof` (admin only) — fleet counts + the blocked creators, or one
  creator with the exact link their posts will carry and a per-channel tag row.
* `/setup → 🏷️ Tag proof` card, a dashboard-home chip, the **Amazon attribution**
  self-check item (names the creators, says "held"), the ops digest line
  (`🏷️ Amazon tags: 61/62 channel(s) own · ⛔ 1 held (No Tag Yet)` plus
  "N waiting for their own Amazon tag" on the retry-queue line).
* The guided review now states the link itself (`Amazon links will read
  www.amazon.in/dp/…?tag=ravi-21`), and `/easy-setup`'s hint uses the same real
  link instead of the old `B0EXAMPLE` placeholder.

### Scale

`fleet()` reads the creators and the channels and checks every active channel in
the same pass: two queries, no per-creator round trip. The suite pins it —
1001 creators + 1001 channels, exactly two `SELECT`s, under a second — and the
self-check/digest reuse the same read instead of repeating it.

### Verified

`tests/test_tag_proof.py` (25 tests, all green in the 652-test suite): the
shared-default trap, the channel override, `verify()` catching a foreign and an
untagged link, the full hold matrix, the pipeline hold end to end (assertion on
the deferred row's reason and the audit row, no captured post), the own-tag post,
one tag across approval + main + WhatsApp mirror, the approval channel held too,
the escape hatch, the worker's bounded retry and its first-of-many post after the
tag is saved, the fleet pass, `describe_fix`, the self-check line, the CLI
(exit codes 0/1/2), the dashboard API, the setup card, the home chip, the flow
board reasons and the digest line.

## 18. This session, part 13: text fidelity, sources and duplicates

The operator's asks: "konni sarlu source lo unna text miss cheyatam and unwanted
text add avvatam jaragakudadu … text miss cheyaku … aanni list of links vachina
perfectga cheyu", "multiple posts same products asalu same channel lo cheyoddu
duplicates asalu vundoddu … 1 hours lo same products vasthe skip cheyali", and
for sources: "pakkana source channel kuda vundlai em select cheyakapothe
defaults gaa https://t.me/LootZoneIndia11 … pakka manam set chesina source
channels pakkana multiple ga select cheskunelaga vundlai". Plus: with only
Amazon switched on, only Amazon products post; with EarnKaro/HYPD on too, those
products post on their own routes.

### Text fidelity (`influencer_hub/fidelity.py`, new)

* `compare(source, rendered)` classifies every difference: `missing` (a product
  line whose words **or numbers** vanished — "₹1299" going missing is a lost
  sale), `added` (a line that was never in the source), `promo_dropped` (channel
  promotion/watermarks, meant to go), `other_dropped`, and a **link budget**: a
  post must not come out with fewer links than it went in with. A link the hub
  rewrote (own tag, EarnKaro conversion, shortener) is reported as `rewritten`,
  never as a loss — that distinction is why the check is usable in production.
* `enforce(source, rendered, fallback_render=…)` runs before delivery: the
  pipeline passes a conservative render (same links/shortener/policy, promo
  cleaning off, every source line kept). If the normal render would lose real
  text, the conservative one is posted instead, `text_fidelity_recovered` is
  audited and both the warning and the note land in the log.
* `link_router.partition_disallowed_blocks` (new) is the network filter: a block
  (one product's copy, blank-line separated) whose only store links belong to a
  switched-off network is removed **whole** — title, sizes and price included —
  while a block with an allowed link keeps its copy and loses only the disabled
  URLs and their dangling labels (`_only_labels_and_punctuation`). The approval
  renderer uses the same label rule.
* `posts.note` (new column + additive migration) records why a delivered post
  differs from its source; `/api/posts.csv` carries it as the `note` column, and
  the self-check item **Source text & duplicates** (new) turns amber with the
  creator's name while any post in 24h changed its source text — and counts the
  duplicate skips it blocked.

### Duplicates

The four guards were already in place (signature, identifier + in-run set,
24h content hash, 12h near-duplicate at 60% word overlap, plus 20/h and 5/10min
rate limits) — the change is that a skip is now **written to the delivery log**
with its reason (`skipped:duplicate_same_product`, `…:content_dedup_24h`,
`…:near_duplicate_12h`, the rate limits) via a small `_note_skip` helper, so "no
duplicates" is visible (`db.skip_reasons`, the self-check line, `/api/posts.csv`)
instead of only implied by the returned status.

### Sources (`influencer_hub/sources.py`, new)

* `DEFAULT_SOURCE_SPEC = https://t.me/LootZoneIndia11` (+ `DEFAULT_SOURCE_NAME`),
  `token_for()` (t.me URL / @name / private invite → the token a Telegram dialog
  name matches), `normalize_selection()` (case-insensitive, deduped, anything an
  operator pastes), `choice_rows()`, `default_token()`, `ensure_default()` and
  `selection_line()`.
* The guided wizard's step 2 carries the picker ("📡 Deal sources") beside the
  channels: tick many, or "post from every source" explicitly; nothing ticked
  saves the default token, and creating a guided creator calls `ensure_default()`
  so the worker can actually read it. `/setup → 📡 Source pool` shows the default
  and adds it in one click (`POST /sources/ensure-default`, audited
  `default_source_ensured`); the creator page's source inputs suggest the tokens
  that exist via a `<datalist>`, per creator and per channel.
* The self-check's **Deal sources** line now also says how many creators post
  from the default and how many from every source.
* `worker.ensure_default_source()` runs at worker startup (idempotent, logged,
  and it never raises — a locked DB must not stop the pull loop), so the default
  exists before the first deal is read on a fresh box.

### "Nothing selected" holds on every add path, not only in the wizard

The wizard was never the only door: Quick Add had no source field at all, so a
creator added there kept the old meaning of empty (`every source in the hub`).
The form now carries the same field, pre-filled with the default (and a *post
from every source* tick for the operator who wants the old behaviour), and the
route treats a blank field as "the default" while callers that do not send the
field at all stay unchanged. For creators already saved with no source, the pool
card shows the count and offers one click (`POST /sources/apply-default` ->
`sources.apply_default()` -> `db.set_allowed_sources_for_unset()`, audited
`default_source_applied`); it touches only blank rows, so an explicit `all` or a
creator pointed at `powerloot` is never rewritten. `sources.usage()` counts the
fleet by how it reads sources (default / every / specific / unset) for that card
and the self-check sentence.

### The category filter no longer costs sales

The journey test exposed a silent trap: the wizard's category tick is a
**keyword** filter, so `🔥 BOAT AIRDOPES at ₹1299 · Use code BOAT500` — a real
electronics deal whose text contains no electronics keyword — was classified
`recharge_freebies` (the word "code") and dropped for a creator who asked for
electronics. Two changes in `link_router`:

* `SECONDARY_CATEGORIES = {"other", "recharge_freebies"}` — flags that describe
  the *kind of offer* (a coupon, a cashback, "loot deal") never veto a deal: only
  a real product category the destination did not ask for does ("Banarasi Silk
  Saree" for an electronics-only creator is still dropped).
* a deal the classifier cannot place anywhere passes,
  `CATEGORY_FILTER_STRICT_UNKNOWN=on` (config) restores the strict behaviour for
  a curated feed.

### Verified

Full suite **695 passed** (`HUB_SKIP_DOTENV=1`), including
`tests/test_text_fidelity.py` (19), `tests/test_sources.py` (21) and the new
whole-journey test in `tests/test_guided_setup.py` (29). The journey itself, end
to end on a copy of the demo hub with fake sends: wizard create (name, number,
tag) → channels + WhatsApp link pasted → paired → nothing ticked for sources →
`allowed_sources == LootZoneIndia11`; a deal from that source posted to approval,
main **and** the mirrored WhatsApp channel, every link signed `tag=journeyravi-21`,
the coupon line intact (`#ad (paid link)` added on approval only); the same
product a moment later came back `skipped:duplicate_same_product` on all three
destinations with the reason in the delivery log; a source the creator does not
read was skipped; and `fidelity.compare(source, what_went_out)` reported
`True | Source text preserved: 4 deal line(s), 0 promotion line(s) removed,
nothing added.` Live on the
demo hub (62 creators, 186 channels): the wizard's picker rendered
`LootZoneIndia11` as the default and saved `powerloot, HUga1JTHwhBmNDE1` when two
were ticked; `api/self-check` showed `ok | Source text & duplicates | Every post
kept its source text …; 0 duplicate(s) were blocked` and turned **warn** with the
creator's name and "2 duplicate(s) blocked" after seeded rows; `/api/posts.csv`
carried the `note` column (`network merchant: NIRLON Shirt at ₹399 — Sizes: M, L,
XL`); and a mixed source deal posted correctly for two creators (@both_on: Amazon
with `tag=bothon-21` **plus** the `ekaro.in` conversion of the Flipkart product;
@amz_only: Amazon only, with the Flipkart block recorded as removed).
