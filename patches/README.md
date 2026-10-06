# `patches/` — verified, replayable fixes

Three ways to move this work into another checkout. All three are verified to
land on **exactly** the same tree.

| # | path | use it when |
| --- | --- | --- |
| 1 | `patches/*.patch` | a clean `0bf913f` checkout: `git apply` all three, done |
| 2 | `patches/apply-*.py` | a VM that already has part of the work (surgical, exact-text) |
| 3 | `patches/tests/` | the verified test files `--with-tests` installs |

## 1. The three git patches

```bash
cd ~/Influencers-deals
git apply patches/commission-leaks.patch   # influencer_hub/
git apply patches/password-policy.patch    # dashboard/
git apply patches/tests.patch              # tests/
pytest -q                                  # 285 passed
```

| patch | lines | covers |
| --- | --- | --- |
| `commission-leaks.patch` | 681 | `influencer_hub/` — the three leaks **and** the account model |
| `password-policy.patch` | 666 | `dashboard/` — password only for removals + the who-earns table |
| `tests.patch` | 1254 | `tests/` — every changed and new test file |

They are generated with `git diff 0bf913f..HEAD -- <dir>`, so they apply on a
clean `main` (`0bf913f`) and reproduce this branch byte for byte.

## 2. The three appliers

```bash
python3 patches/apply-commission-fixes.py --with-tests
python3 patches/apply-password-policy.py  --with-tests
python3 patches/apply-account-model.py    --with-tests
sudo systemctl restart influencer-deal-worker influencer-dashboard
systemctl is-active influencer-deal-worker influencer-dashboard
```

| applier | touches | what it fixes |
| --- | --- | --- |
| `apply-commission-fixes.py` | `influencer_hub/` | tagged Amazon pages kept, `convert_option` payload, `affExtParam2`/`id=` provenance |
| `apply-password-policy.py` | `dashboard/` | `REAUTH_REQUIRED_ENDPOINTS` 28 → 3, no `data-require-reauth` on adds/saves, one shared unlock dialog |
| `apply-account-model.py` | both | installs `influencer_hub/accounts.py` + wires it in (their Amazon id, our EarnKaro/HYPD) |

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
live there; `apply-account-model.py` and `apply-password-policy.py` are the two
it still needs. Each applier touches a different set of functions, so they can
run in any order. Without `--with-tests` an applier changes only its own code
files — the tests it installs assume **all three** policies are in place.

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
account earns"** table.

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
touched (including `test_reauth_policy.py`, `test_amazon_tag_attribution.py` and
`test_account_model.py`). `patches/conftest.py` keeps pytest from collecting the
bundle, so a receiving checkout needs no extra config.

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

## Verified end-to-end

* clean `0bf913f` + the three `.patch` files → **285 passed**, byte-identical tree
  (that count includes the 11 guards in `tests/test_patch_bundle.py`)
* clean `0bf913f` + the three appliers (`--with-tests`) → **274 passed**,
  byte-identical tree. The 11 bundle guards are deliberately **not** installed by
  `--with-tests`: they only make sense on the branch that ships the bundle.
* a second run of every applier → exit `1`, nothing written
* baseline on `main` before any of this: **234 passed**
