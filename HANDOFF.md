# HANDOFF — Influencers-deals

**Updated:** 2026-10-07 (UTC)

**Session branch:** `arena/9056711b-influencers-deals`

**Checkout base:** `c6fa901` — PR #7 merged to `main`

**Branch commits:** `fb70433` (source fallback, PR #8) and `f46f165` (link
conversion audit — see section 1b).

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
python3 -m influencer_hub.cli doctor --telegram-sources
```

The test suite on this revision is expected to report **374 passed**. The
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
python3 -m influencer_hub.cli doctor --telegram-sources
python3 -m influencer_hub.cli audit-links --offline --text "Deal https://www.amazon.in/dp/B08XYZ1234?tag=old-21"   # expect exit 0 + [OUR] amazon
python3 -m pytest -q            # expect 374 passed
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
