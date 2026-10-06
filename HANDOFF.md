# HANDOFF — Influencers-deals

Read this first. It documents what exists where, how to get it into a new
session, and the traps that already cost a session once.

---

## 0. Reality check (do this before believing anything)

An earlier session's notes claimed a `patches/` folder and a `HANDOFF.md` were
committed to `main`. **They never were.** A new session starting from `main`
(`0bf913f`) gets none of the work below. Everything that matters therefore lives
inside the repo — and this branch is pushed, so a fetch is enough.

| item | state |
| --- | --- |
| `origin/main` | `0bf913f` "Merge PR #6" — PRs #1–#6 all merged, none open |
| PR #6 content | Password login, ⚡ Easy Setup, 💰 Money Radar, gunicorn `0.0.0.0:5000`, Telegram ingestion — **already on `main`, do not redo** |
| branch `arena/a54d6a1d-influencers-deals` | commission fixes + password policy + account model + the patch bundle |
| tests | `main` = **234 passed**, this branch = **285 passed** |
| VM | commission fixes live; password policy + account model still to apply |

Fastest path into a new session:

```bash
git fetch origin arena/a54d6a1d-influencers-deals
git checkout -b arena/a54d6a1d-influencers-deals origin/arena/a54d6a1d-influencers-deals
```

If the branch cannot be fetched, use `patches/` (upload it from the workspace).

---

## 1. Bootstrap a session

```bash
python3 -m venv .venv && .venv/bin/pip install -q pytest aiohttp flask
.venv/bin/python -m pytest -q      # 285 passed here, 234 on main
```

From a **clean `main`** checkout, either replay path reproduces this branch
byte for byte:

```bash
# A. git patches (recommended for a fresh clone)
git apply patches/commission-leaks.patch
git apply patches/password-policy.patch
git apply patches/tests.patch

# B. surgical appliers (for a VM that already has some of the work)
python3 patches/apply-commission-fixes.py --with-tests
python3 patches/apply-password-policy.py  --with-tests
python3 patches/apply-account-model.py    --with-tests

.venv/bin/python -m pytest -q      # expect 285 passed
```

`patches/conftest.py` keeps pytest out of `patches/tests/`; `pytest.ini` is not
needed. `tests/test_patch_bundle.py` fails if the bundle ever drifts from the
tree.

---

## 2. Commission leaks (done)

| # | Leak | Fix |
| --- | --- | --- |
| 1 | Amazon **search / storefront** pages carrying OUR tag were deleted as "not OUR canonical" | attribution now follows the tag: `advanced_shortener.is_our_amazon_attribution()` keeps any Amazon URL with exactly one tag equal to ours (guard, Money Radar, pipeline "still earns?" check) |
| 2 | Wrong EarnKaro body → HTTP 200 with no link → Flipkart/Myntra/Ajio/Nykaa/Croma/Shopsy posted free | payload is `{"deal": <clean url>, "convert_option": "convert_only"}` + Bearer token, in `convert_one` **and** the live verifier |
| 3 | Another publisher's EarnKaro link accepted as ours | `link_router.publisher_ids_in_url()` reads `affExtParam2` **and** numeric `id=`; a mismatch falls back to the raw URL (direct results and after redirect resolution) |

Verify on the VM (needs the configured token):

```bash
python3 -m influencer_hub.cli verify-earnkaro
# expect: ok: True, publisher_id: '5478322', publisher_provenance_verified: True
```

Tests: `tests/test_amazon_tag_attribution.py` (new), `tests/test_earnkaro_live.py`,
`tests/test_earnkaro_provenance.py`.

---

## 3. Account model (done) — their Amazon id, our EarnKaro/HYPD

`influencer_hub/accounts.py` is the single source of truth:

| network | who earns | rule |
| --- | --- | --- |
| **Amazon** | the **creator** | `creator_amazon_tag()` = channel override → the creator's own tag → configured fallback; `amazon_tag_source()` reports `fallback` so profiles missing their own id are visible (`creators_missing_own_tag()`) |
| **EarnKaro** | **us** | vault API token + publisher id (`central_earnkaro_api_key()`, `central_earnkaro_publisher_id()`) |
| **Meesho / HYPD** | **us** | `hypd_store_for()` returns OUR store; with `central_network_accounts` on (default, `CENTRAL_NETWORK_ACCOUNTS=1`) a per-creator or per-channel `hypd_store_id` cannot move HYPD commission |
| LehLah | the source | attribution preserved |

Pipeline, Money Radar and both dashboard previews resolve the pair through this
module, so what we post and what we audit are the same accounts. Easy Setup's
routing table now shows a **"whose account earns"** section.

Turn the central half off only if a creator must keep their own HYPD store:

```bash
python3 -c "from influencer_hub import db; db.init(); db.set_global_setting('central_network_accounts','0')"
```

---

## 4. Password policy (done) — first login + removals only

* `REAUTH_REQUIRED_ENDPOINTS`: 28 → 3 (`delete_channel`, `delete_influencer`,
  `delete_deal_source`).
* `data-require-reauth` removed from the 20 add / save / toggle / poll /
  Easy-Setup / money-switch forms; kept on the four removal tags.
* Delete-Influencer lost its separate `prompt()` + hidden `admin_password` and
  uses the shared unlock dialog. Copy says **Removals** everywhere.
* The Vault tab keeps its own password gate (it stores credentials) — unchanged.
* `tests/test_reauth_policy.py` pins the gated set, the tags, the copy and that
  add/toggle/save never ask.

---

## 5. Apply on the VM (what is still missing there)

```bash
cd ~/Influencers-deals
python3 patches/apply-password-policy.py --with-tests
python3 patches/apply-account-model.py   --with-tests
sudo systemctl restart influencer-dashboard
systemctl is-active influencer-dashboard
```

* The commission fixes are **already live** on the VM, so
  `apply-commission-fixes.py` and the `.patch` files are not needed there.
* Each applier stops with exit `1` on a second run and exit `2` (writing nothing)
  if a file does not match the verified revision.
* `--check` reports; `--dry-run` validates.

---

## 6. Next decisions, in order

1. **EarnKaro / Affiliaters token vault — biggest earning gap.** Until a valid
   token + publisher id sit in the Vault tab, every non-Amazon link earns
   nothing. Confirm with `verify-earnkaro`.
2. Then open **`/money`** and decide whether to turn on `ONLY_EARNING_DEALS`
   (config default `False`) so no post is spent on a link that pays zero.
3. **Give every creator their own Amazon tag** — check
   `python3 -c "from influencer_hub import accounts; print(accounts.creators_missing_own_tag())"`.
   Those profiles currently post Amazon under the fallback tag.
4. VM hygiene: remove `dashboard/app.py.save`; decide on the legacy
   `new-deals-bot-zip-main (1).zip` (1.2 MB) in the repo root.

---

## 7. Traps — do not relearn these

* **Never** `git checkout -- <file>` on the VM: it reverts fixes that are not in
  `origin/main`.
* **`patch -U0` will not apply these bundles** — use `git apply` or the appliers.
* **Keep artifacts inside the repo.** `/home/user/*.patch` and anything outside
  the checkout are lost when the sandbox restarts.
* `git pull` on a new session gives you `main` (`0bf913f`) — none of sections
  2–4 unless you replay `patches/`.
* Python is externally managed (PEP 668): `pip install` needs a venv.
* Test counts in older notes (244 / 251) do not match this tree; the real
  numbers are **234 on `main`** and **285 on this branch**.
