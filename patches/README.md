# `patches/` — verified, replayable fixes

The first five patches reproduce the earlier release on its historical base.
`private-source-fallback.patch` is the source-visibility follow-up for the
current `c6fa901` release, and `link-conversion-fixes.patch` (diffed against that
state, so apply it **second**) carries the link-conversion audit fixes and the
conversion retry queue. Each applier validates its own patch before writing.

| # | path | use it when |
| --- | --- | --- |
| 1 | `patches/*.patch` | replaying the earlier release, then applying the source-fallback patch |
| 2 | `patches/apply-*.py` | a VM that already has part of the work (exact-match appliers) |
| 3 | `patches/tests/` | the verified test files and DB-isolating conftest `--with-tests` installs |
| 4 | `patches/HANDOFF.md` | a byte-identical copy of the root handoff, so the bundle is self-contained (guarded by `tests/test_patch_bundle.py`) |

## 1. Earlier release patches + the current source fix

On a clean historical `0bf913f` checkout, replay the original five patches in
order, then apply the new source fix:

```bash
cd ~/Influencers-deals
git apply patches/commission-leaks.patch
git apply patches/password-policy.patch
git apply patches/tests.patch
git apply patches/deal-flow.patch
git apply patches/docs.patch
git apply patches/private-source-fallback.patch
git apply patches/link-conversion-fixes.patch
python3 patches/apply-private-source-fallback.py --with-tests
python3 patches/apply-link-conversion-fixes.py --with-tests
python3 -m pytest -q                         # 374 passed on this revision
```

On a VM already at the merged `c6fa901` release, only the two appliers are
needed, in this order: `apply-private-source-fallback.py` then
`apply-link-conversion-fixes.py`. Each one validates its exact patch first and
writes nothing on mismatch; the second one tells you if the first is missing.

| patch | lines | covers |
| --- | --- | --- |
| `commission-leaks.patch` | 681 | `influencer_hub/` — the three leaks **plus** the account model |
| `password-policy.patch` | 666 | `dashboard/` — password only for removals + the who-earns table |
| `tests.patch` | 1524 | `tests/` — every changed and new test file, at this revision |
| `deal-flow.patch` | 474 | `influencer_hub/` + `dashboard/` — per-source deal flow, `/api/flow`, the Easy Setup card |
| `docs.patch` | 217 | `HANDOFF.md` — the historical handoff at the previous release |
| `private-source-fallback.patch` | follow-up | named private-invite fallback, diagnostics and current handoff updates |
| `link-conversion-fixes.patch` | follow-up 2 | Amazon short coverage (`amzn.eu`/`amzn.asia`/`a.co`), conversion retry queue, approval dedup, guard/flow-board updates, README + HANDOFF |

The first five patch files are the historical replay path (`0bf913f` to the
merged pre-fallback release). The source-fallback patch is based on that merged
release (`c6fa901`); the conversion patch is based on the source-fallback state
and must be applied after it. The live repo's guard tests verify that every
patch exists, that every applier reports the branch as patched, and that every
copied test stays byte-identical.

## 2. Appliers

```bash
python3 patches/apply-private-source-fallback.py --check
python3 patches/apply-private-source-fallback.py --dry-run
python3 patches/apply-private-source-fallback.py --with-tests
python3 patches/apply-link-conversion-fixes.py --check
python3 patches/apply-link-conversion-fixes.py --dry-run
python3 patches/apply-link-conversion-fixes.py --with-tests
python3 -m influencer_hub.cli doctor --telegram-sources
python3 -m pytest -q                          # 374 passed
sudo systemctl restart influencer-deal-worker influencer-dashboard
systemctl is-active influencer-deal-worker influencer-dashboard
curl -s localhost:5000/api/flow | head -c 400
```

The four original appliers below remain useful for VMs missing those earlier
changes; do not rerun them on a release where their checks say already applied.

| applier | touches | what it fixes |
| --- | --- | --- |
| `apply-commission-fixes.py` | `influencer_hub/` | tagged Amazon pages kept, `convert_option` payload, `affExtParam2`/`id=` provenance |
| `apply-password-policy.py` | `dashboard/` | `REAUTH_REQUIRED_ENDPOINTS` 28 → 3, no `data-require-reauth` on adds/saves, one shared unlock dialog |
| `apply-account-model.py` | both | installs `influencer_hub/accounts.py` + wires it in (their Amazon id, our EarnKaro/HYPD) |
| `apply-deal-flow.py` | both | `source_activity` bookkeeping, `_flow_snapshot()`, `GET /api/flow`, the Easy Setup flow card |
| `apply-private-source-fallback.py` | puller + dashboard + docs | unmatched named private invites fall back to eligible joined dialogs; Setup and CLI diagnostics explain why |

The four original appliers support `--check`, `--dry-run`, `--with-tests` and
stop a second run. The new applier validates its bundled `git apply` patch and
also supports `--check`, `--dry-run`, and `--with-tests`:

| flag | meaning |
| --- | --- |
| *(none)* | apply, print every edit |
| `--check` | report whether the fix is already in place, write nothing |
| `--dry-run` | validate every replacement, write nothing |
| `--with-tests` | also refresh the verified test files from `patches/tests/` |

Exit codes: `0` applied / already in place with `--check`, `1` already applied
(stop — never double-edit), `2` the file did not match the verified revision
(nothing was written).

**For the VM:** the merged `c6fa901` release already contains the commission,
account, password and deal-flow work. Apply only
`apply-private-source-fallback.py` for this follow-up. It never resets files or
joins channels; if its exact patch does not match, it exits without writing.
Use `--with-tests` only when refreshing the test suite on the VM.

## 3. What each policy actually does

### Commission leaks (`influencer_hub/`)

1. **Amazon attribution follows the `tag`, not the `/dp/` path.** A search page /
   storefront / `amzn.to` short link carrying OUR tag is kept instead of being
   deleted by the commission guard
   (`advanced_shortener.is_our_amazon_attribution`, used by the guard, Money
   Radar and the pipeline's "does anything still earn?" check).
2. **EarnKaro payload.** `{"deal": <clean url>, "convert_option": "convert_only"}`
   in `convert_one` *and* the live verifier. The old body answered HTTP 200 with
   no link, which posted unpaid Flipkart / Myntra / Ajio / Nykaa / Croma /
   Shopsy links.
3. **Provenance.** `link_router.publisher_ids_in_url()` reads `affExtParam2`
   **and** a numeric `id=`; a link carrying somebody else's id falls back to the
   raw merchant URL.

### Account model (`influencer_hub/accounts.py`)

| network | who earns | enforced by |
| --- | --- | --- |
| Amazon | the **creator** (their own Associate tag) | `accounts.creator_amazon_tag()` — channel override → creator tag → flagged fallback |
| EarnKaro | **us** (vault credentials + publisher id) | `accounts.central_earnkaro_*()` |
| Meesho / HYPD | **us** (our store id) | `accounts.hypd_store_for()` — with `central_network_accounts` on (default) a stored per-creator store cannot take the commission |
| LehLah | the source's attribution | preserved as-is |

Pipeline, Money Radar and both dashboard previews resolve through the module, so
posts and the money audit can never disagree; Easy Setup shows a **"whose
account earns"** table. `accounts.creators_missing_own_tag()` lists any profile
that would post on the fallback tag.

### Deal flow (`influencer_hub/` + `dashboard/`)

* The worker keeps its self-healing properties (cursor held on a failed
  delivery, heartbeat, capped backoff, separate poll queue) and now records per
  source: deals seen, posts dispatched, failures and the **reason**.
* `db.source_activity` + `db.channel_post_activity()` are the readers;
  `posts.posted_at` only exists for successful posts, so failures are reported
  all-time rather than pretending they are dated.
* `GET /api/flow` (pure reads, no network) returns worker state, per-source
  last-seen, per-channel posted/failed counts, `hourly_loot_enabled` and
  `only_earning_deals`, plus plain-language notes — e.g. a source that delivered
  before but has been silent for 3h, or an idle day that is idle because the
  hourly loot sweep is off.
* Easy Setup ends with a **"Deal flow — sources → posts"** card; the per-source
  table and the channel table live in its Advanced section.

### Private Telegram source fallback (`influencer_hub/puller.py`)

* A private invite is never checked or joined. Its friendly name is only a hint
  for an **exact normalized joined-dialog title** match.
* If one or more private invite hints do not match, selection falls back to all
  eligible dialogs already joined by the Telegram account. Configured output
  channels and unconfigured account-owned channels are excluded. The worker
  logs one warning per source configuration; invite hashes are not logged.
* This fallback may read many joined groups/channels. Use public usernames or
  the exact private-dialog title to narrow selection. The Setup Center and
  `doctor --telegram-sources` show when fallback is active.
* The flow board proves the selection: `/api/flow` gains `live` (mode, counts
  and a capped list of the dialogs the worker would read now) and per-source
  rows gain `source_name`, the joined dialog's title recorded by the worker.
  The probe is read-only, cached ~45 s, and skippable with `?live=0`.
* `tests/conftest.py` redirects every test to a fresh temporary SQLite DB, so
  tests without a local DB fixture cannot touch a VM/operator database.

### Password policy (`dashboard/`)

* `REAUTH_REQUIRED_ENDPOINTS`: 28 endpoints → `delete_channel`,
  `delete_influencer`, `delete_deal_source`.
* `data-require-reauth` removed from the 20 add / save / toggle / poll /
  Easy-Setup / money-switch forms (kept on the four removal tags).
* Delete-Influencer drops its own `prompt()`/`admin_password` and uses the shared
  unlock dialog; copy says **Removals** everywhere.
* The Vault tab keeps its separate password gate (it stores credentials).

## `patches/tests/` + `conftest.py`

`patches/tests/` carries byte-identical copies for the patched test files and a
copy of `tests/conftest.py`. The root conftest gives each pytest case a fresh
`tmp_path` SQLite DB; `patches/conftest.py` separately prevents pytest from
collecting the bundle copies twice.

`tests/test_patch_bundle.py` is the drift guard: it checks patch presence and
format, copied-test parity, root DB isolation, and that every applier recognizes
the current tree.

## Traps (learned the hard way)

* The current follow-up expects the merged `c6fa901` release. The original five
  patches are only for the historical `0bf913f` replay path.
* Do not use `git checkout -- <file>` or force a patch on a VM with a mismatch.
  The new applier validates first and exits without writing when context differs.
* Private-invite fallback scans eligible joined dialogs; it does not join the
  invite. Narrow with usernames or exact dialog titles if a broad joined set is
  not intended.
* Keep artifacts inside the repo. Test copies go stale when tests change; run
  the full suite so `test_patch_bundle.py` catches drift.

## Verified in this session

* Clean checkout baseline `c6fa901`: **302 passed** before the follow-up.
* Working tree after the source-selection fix, live flow-board source
  visibility, DB-isolated tests, docs and bundle guard: **320 passed**.
* `tests/test_private_source_fallback.py` simulates 236 joined dialogs, checks
  warning behavior, exclusions and capped dialog summaries, confirms live
  diagnostics make no invite or history requests, and exercises the worker pull
  path; `tests/test_deal_flow.py` pins the live `/api/flow` selection view and
  the recorded joined-dialog title per source.
* The full suite has not been run against a live Telegram account or deployed VM.
