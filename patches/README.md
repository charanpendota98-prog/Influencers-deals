# `patches/` — verified, replayable fixes

Four ways to move this work into another checkout, verified to land on
**exactly** the same code, tests and handoff.

| # | path | use it when |
| --- | --- | --- |
| 1 | `patches/*.patch` | a clean `0bf913f` checkout: `git apply` all five, done |
| 2 | `patches/apply-*.py` | a VM that already has part of the work (surgical, exact-text) |
| 3 | `patches/tests/` | the verified test files `--with-tests` installs |
| 4 | `patches/HANDOFF.md` | a byte-identical copy of the root handoff, so the bundle is self-contained (guarded by `tests/test_patch_bundle.py`) |

## 1. The five git patches

```bash
cd ~/Influencers-deals
git apply patches/commission-leaks.patch   # influencer_hub/
git apply patches/password-policy.patch    # dashboard/
git apply patches/tests.patch              # tests/
git apply patches/deal-flow.patch          # influencer_hub/ + dashboard/ (flow board)
git apply patches/docs.patch               # HANDOFF.md
pytest -q                                  # 302 passed
```

| patch | lines | covers |
| --- | --- | --- |
| `commission-leaks.patch` | 681 | `influencer_hub/` — the three leaks **plus** the account model |
| `password-policy.patch` | 666 | `dashboard/` — password only for removals + the who-earns table |
| `tests.patch` | 1524 | `tests/` — every changed and new test file, at this revision |
| `deal-flow.patch` | 474 | `influencer_hub/` + `dashboard/` — per-source deal flow, `/api/flow`, the Easy Setup card |
| `docs.patch` | 217 | `HANDOFF.md` — the handoff itself, so a patched checkout is complete |

`commission-leaks.patch`, `password-policy.patch` and `tests.patch` cover
`0bf913f..77acbe7`; `deal-flow.patch` covers `77acbe7..HEAD` for the two code
directories only, so the tests always come from the single `tests.patch`, and
`docs.patch` adds `HANDOFF.md`. Apply them in the order above on a clean `main`
(`0bf913f`) and the tree matches this branch down to the byte — nothing is
missing.

## 2. The four appliers

```bash
python3 patches/apply-commission-fixes.py --with-tests
python3 patches/apply-password-policy.py  --with-tests
python3 patches/apply-account-model.py    --with-tests
python3 patches/apply-deal-flow.py        --with-tests
sudo systemctl restart influencer-deal-worker influencer-dashboard
systemctl is-active influencer-deal-worker influencer-dashboard
curl -s localhost:5000/api/flow | head -c 400
```

| applier | touches | what it fixes |
| --- | --- | --- |
| `apply-commission-fixes.py` | `influencer_hub/` | tagged Amazon pages kept, `convert_option` payload, `affExtParam2`/`id=` provenance |
| `apply-password-policy.py` | `dashboard/` | `REAUTH_REQUIRED_ENDPOINTS` 28 → 3, no `data-require-reauth` on adds/saves, one shared unlock dialog |
| `apply-account-model.py` | both | installs `influencer_hub/accounts.py` + wires it in (their Amazon id, our EarnKaro/HYPD) |
| `apply-deal-flow.py` | both | `source_activity` bookkeeping, `_flow_snapshot()`, `GET /api/flow`, the Easy Setup flow card |

Each supports `--check`, `--dry-run`, `--with-tests` and stops a second run:

| flag | meaning |
| --- | --- |
| *(none)* | apply, print every edit |
| `--check` | report whether the fix is already in place, write nothing |
| `--dry-run` | validate every replacement, write nothing |
| `--with-tests` | also refresh the verified test files from `patches/tests/` |

Exit codes: `0` applied / already in place with `--check`, `1` already applied
(stop — never double-edit), `2` the file did not match the verified revision
(nothing was written).

**On the VM, apply only what is missing.** The commission fixes are already
live there; `apply-password-policy.py`, `apply-account-model.py` and
`apply-deal-flow.py` are the ones it still needs. The appliers touch different
functions, so they can run in any order. Without `--with-tests` an applier
changes only its own code files — the tests it installs assume **all four**
policies are in place.

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

### Password policy (`dashboard/`)

* `REAUTH_REQUIRED_ENDPOINTS`: 28 endpoints → `delete_channel`,
  `delete_influencer`, `delete_deal_source`.
* `data-require-reauth` removed from the 20 add / save / toggle / poll /
  Easy-Setup / money-switch forms (kept on the four removal tags).
* Delete-Influencer drops its own `prompt()`/`admin_password` and uses the shared
  unlock dialog; copy says **Removals** everywhere.
* The Vault tab keeps its separate password gate (it stores credentials).

## `patches/tests/` + `conftest.py`

`patches/tests/` holds byte-identical copies of the test files the policies
touched (including `test_reauth_policy.py`, `test_amazon_tag_attribution.py`,
`test_account_model.py` and `test_deal_flow.py`). `patches/conftest.py` keeps
pytest from collecting the bundle, so a receiving checkout needs no extra
config.

`tests/test_patch_bundle.py` is the drift guard for all of this: it fails if a
`.patch` is malformed, if `patches/tests/` copies diverge from `tests/`, or if an
applier no longer reports the branch state.

## Traps (learned the hard way)

* **Never** `git checkout -- <file>` on the VM — the VM holds fixes that are not
  in `origin/main`, so a checkout silently reverts them.
* `patch -U0` mis-applies these bundles. Use `git apply` or the appliers.
* Keep artifacts **inside** the repo: a sandbox restart wipes `/home/user/*.patch`
  and anything else outside the checkout.
* Don't mix paths casually — a `.patch` expects a clean `0bf913f`; an applier
  expects the mix of fixes the VM actually has.
* `patches/tests/` copies go stale the moment a test changes. Re-copy, then let
  `tests/test_patch_bundle.py` prove they match.

## Verified end-to-end

* clean `0bf913f` + the five `.patch` files → **302 passed**, every file
  byte-identical (15 of those tests are the bundle guards in
  `tests/test_patch_bundle.py`)
* clean `0bf913f` + the four appliers (`--with-tests`) → **287 passed**, every
  code and test file byte-identical. `--with-tests` never installs
  `tests/test_patch_bundle.py` (15 guards): they only make sense on the branch
  that ships the bundle. The appliers also do not write the root `HANDOFF.md`;
  the bundle's own `patches/HANDOFF.md` is its copy.
* a second run of every applier → exit `1`, nothing written
* baseline on `main` before any of this: **234 passed**
