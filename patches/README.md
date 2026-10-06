# `patches/` — verified, replayable fixes

Two self-contained appliers. Both are **idempotent, exact-text and validated
before writing**, and both refuse to run a second time (exit `1`) so they can
never double-edit a tree.

```bash
# on the VM, from the repo root
python3 patches/apply-commission-fixes.py      # section 2: influencer_hub/
python3 patches/apply-password-policy.py       # section 3: dashboard/
sudo systemctl restart influencer-deal-worker influencer-dashboard
systemctl is-active influencer-deal-worker influencer-dashboard
```

Each script supports:

| flag | meaning |
| --- | --- |
| *(none)* | apply, print every edit |
| `--check` | report whether the fix is already in place, write nothing |
| `--dry-run` | validate every replacement, write nothing |
| `--with-tests` | also refresh the verified test files from `patches/tests/` |

Exit codes: `0` applied / already in place with `--check`, `1` already applied
(stop — never double-edit), `2` the file did not match the verified revision
(nothing was written).

## What each patch does

### `apply-commission-fixes.py` — the three commission leaks

1. **Amazon attribution is decided by the `tag`, not by the `/dp/` path.**
   A search page / storefront / `amzn.to` short link carrying OUR Associate tag
   used to be flagged "not OUR canonical" and deleted by the commission guard —
   throwing away real commission. `advanced_shortener.is_our_amazon_attribution`
   now keeps any Amazon URL with exactly one tag equal to ours, in the guard,
   in Money Radar and in the pipeline's "does anything still earn?" check.
   Files: `link_router.py`, `advanced_shortener.py`, `commission_guard.py`,
   `money_radar.py`, `pipeline.py`.
2. **EarnKaro converter payload.** The verified contract is
   `{"deal": <clean merchant url>, "convert_option": "convert_only"}`. A wrong
   body answers HTTP 200 with no link, so Flipkart / Myntra / Ajio / Nykaa /
   Croma / Shopsy links posted for free. Fixed in `convert_one` and in the live
   verifier.
3. **Publisher provenance.** The publisher id is read from `affExtParam2` **and**
   from a plain numeric `id=` (`link_router.publisher_ids_in_url`). A link that
   carries somebody else's id falls back to the raw merchant URL instead of
   being accepted as ours.

### `apply-password-policy.py` — password only for removals

* `REAUTH_REQUIRED_ENDPOINTS`: 28 endpoints → exactly
  `delete_channel`, `delete_influencer`, `delete_deal_source`.
* `data-require-reauth` removed from the 20 add / save / toggle / poll /
  Easy-Setup / money-switch forms; kept on the 3 removal forms.
* Delete-Influencer loses its own inline `prompt()`/`admin_password` field and
  uses the same shared unlock dialog as every other removal.
* Copy says **Removals**, not "setup changes", everywhere (app.py flash, nav
  lock button, no-JS banner, unlock dialog, Easy Setup / Setup / Money hints).

## `patches/tests/`

Byte-identical copies of the branch's `tests/` versions for the files touched by
the two policies above (`test_reauth_policy.py` is new):

```
test_advanced_features.py        test_amazon_tag_attribution.py
test_channel_add_delete_unlock.py test_dashboard_security.py
test_earnkaro_live.py            test_easy_setup_and_routing.py
test_reauth_policy.py
```

`--with-tests` copies them over `tests/`. They assume **both** patches have been
applied (that is the state of this branch), so run it only when both are in
place. Without `--with-tests` the scripts touch nothing outside
`influencer_hub/` and `dashboard/` respectively.

`patches/conftest.py` keeps pytest from collecting this folder, so a checkout
that receives `patches/` can run its suite without any extra config — the bundle
is self-contained.

**On the VM, run `apply-password-policy.py` only.** The commission fixes are
already live there, and the commission applier would either report "already
applied" or abort with exit `2` (writing nothing) because the VM files no longer
match the verified `main` revision.

## Traps (learned the hard way)

* **Never** `git checkout -- <file>` on the VM — the VM holds fixes that are not
  in `origin/main`, so a checkout silently reverts them.
* `patch -U0` mis-applies these bundles (context-free hunks land in the wrong
  place). Always use these scripts.
* Keep artifacts **inside** the repo. A sandbox restart wipes `/home/user/*.patch`
  and any other file outside the checkout; that is why this bundle lives here.

## Verified end-to-end

A fresh clone of `main` + both appliers + `--with-tests` reproduces this
branch's `influencer_hub/`, `dashboard/` and the seven test files byte for byte,
and the suite reports **263 passed** (baseline on `main` before these patches:
**234 passed**).
