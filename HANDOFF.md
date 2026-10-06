# HANDOFF — Influencers-deals

Read this first. It is the complete checklist for continuing the work in a new
session, and it also documents what is on the VM but **not** in `origin/main`.

---

## 0. Reality check (do this before believing anything)

A previous session's notes claimed a `patches/` folder and a `HANDOFF.md` were
committed to the repo. **They never made it into git.** A new session starting
from `main` at `0bf913f` gets neither the commission fixes nor the password
policy. That is exactly why this branch ships both fixes as replayable scripts
plus this file.

Facts as of this handoff:

| item | state |
| --- | --- |
| `origin/main` | `0bf913f` "Merge PR #6" — PRs #1–#6 all merged, none open |
| PR #6 content | Password login, ⚡ Easy Setup, 💰 Money Radar, gunicorn on `0.0.0.0:5000`, Telegram ingestion — **already on `main`, do not redo** |
| Commission-leak fixes | only in this branch (`influencer_hub/`) + on the VM |
| Password policy ("only removals") | only in this branch (`dashboard/`) + on the VM |
| Baselines | `main` = **234 passed**; this branch = **263 passed** |

The sandbox is wiped between sessions **outside** the checkout: `/home/user/*.patch`
and similar scratch files do not survive. Everything that matters is inside the
repo, in `patches/` — and this branch was pushed to
`origin/arena/a54d6a1d-influencers-deals`, so a new session that can fetch it
gets sections 2 and 3 without replaying anything:

```bash
git fetch origin arena/a54d6a1d-influencers-deals
git checkout -b arena/a54d6a1d-influencers-deals origin/arena/a54d6a1d-influencers-deals
```

`main` stays untouched either way; the two appliers remain the fallback path.

---

## 1. Bootstrap a new session

```bash
# system Python is PEP 668 managed — use a venv
python3 -m venv .venv && .venv/bin/pip install -q pytest aiohttp flask
.venv/bin/python -m pytest -q          # 263 passed on this branch, 234 on main
```

If you start from `main` instead of this branch, replay the work with the two
appliers (they are byte-for-byte equivalent to what this branch contains):

```bash
python3 patches/apply-commission-fixes.py --with-tests
python3 patches/apply-password-policy.py  --with-tests
.venv/bin/python -m pytest -q          # expect 263 passed
```

`patches/conftest.py` keeps pytest out of `patches/tests/` (verified copies of a
few test files), so the suite is never collected twice — no config needed in the
receiving checkout.

---

## 2. Section 2 — commission leaks (done in this branch)

| # | Leak | Fix |
| --- | --- | --- |
| 1 | Amazon **search / storefront** pages carrying OUR tag were flagged "not OUR canonical" and deleted, throwing away commission | attribution now follows the `tag`: `advanced_shortener.is_our_amazon_attribution()` keeps any Amazon URL with exactly one tag equal to ours (guard, Money Radar, pipeline "still earns?" check) |
| 2 | Wrong EarnKaro converter body → HTTP 200 with no link → Flipkart/Myntra/Ajio/Nykaa/Croma/Shopsy posted for free | payload is now `{"deal": <clean url>, "convert_option": "convert_only"}` + Bearer token, in `convert_one` **and** the live verifier |
| 3 | Another publisher's EarnKaro link accepted as ours | `link_router.publisher_ids_in_url()` reads `affExtParam2` **and** numeric `id=`; a mismatch falls back to the raw URL (direct results and after following short-link redirects) |

Verify on the VM (needs the configured token):

```bash
python3 -m influencer_hub.cli verify-earnkaro
# expect: ok: True, publisher_id: '5478322', publisher_provenance_verified: True
```

New/updated tests: `tests/test_amazon_tag_attribution.py` (new),
`tests/test_earnkaro_live.py`, `tests/test_earnkaro_provenance.py`.

---

## 3. Section 3 — password policy: first login + removals only

Rule: the password is asked **once at sign-in** and again only for a **removal**.

* `REAUTH_REQUIRED_ENDPOINTS`: 28 → 3 (`delete_channel`, `delete_influencer`,
  `delete_deal_source`).
* `data-require-reauth` removed from the 20 add / save / toggle / poll /
  Easy-Setup / money-switch forms; kept on the 3 removal forms (4 tags total:
  `delete_influencer`, `delete_channel`, two `delete_deal_source`).
* Delete-Influencer lost its separate `prompt()` password and hidden
  `admin_password` field — all removals share the one unlock dialog.
* Copy says **Removals** everywhere instead of "setup changes" (flash, nav lock
  badge, no-JS banner, dialog, Easy Setup / Setup / Money hints).
* The Vault tab keeps its own separate password gate (it stores credentials) —
  unchanged on purpose.
* New test file `tests/test_reauth_policy.py` pins the gated set, the four
  `data-require-reauth` tags, the copy, and that add/toggle/save never ask.

---

## 4. Apply on the VM

```bash
cd ~/Influencers-deals
python3 patches/apply-password-policy.py       # prints "already applied" + exit 1 on a rerun
sudo systemctl restart influencer-dashboard
systemctl is-active influencer-dashboard
```

On the VM run **only** the password-policy applier: the commission fixes are
already live there. `apply-commission-fixes.py` is for a checkout (a new
session, a fresh clone) that still lacks section 2 — it never edits
`dashboard/`, and it aborts with exit `2` and writes nothing when the
`influencer_hub/` files do not match the verified `main` revision.

* `influencer_hub/` and `dashboard/` are edited by different scripts, so one can
  be applied without touching the other's fixes.
* A second run of either script stops with exit `1` on purpose — it never
  double-edits.
* A file that does not match the verified revision aborts with exit `2` and
  writes **nothing** (no half-applied tree).
* `--check` reports without writing; `--dry-run` validates all replacements.

---

## 5. Next decisions, in order

1. **EarnKaro / Affiliaters token vault — biggest earning gap.** Until a valid
   API token + publisher id are stored, every non-Amazon link earns nothing.
   Store them in the Vault tab (`earnkaro_api_key`, `earnkaro_publisher_id`) and
   confirm with `verify-earnkaro`.
2. Then open **`/money`** and decide whether to turn on `ONLY_EARNING_DEALS`
   (config default `False`) so a post is never spent on a link that pays zero.
3. VM hygiene: clean up `dashboard/app.py.save` once and for all.
4. Optional: the deposit of `new-deals-bot-zip-main (1).zip` (1.2 MB) in the repo
   root is legacy — decide whether to keep or drop it.

---

## 6. Traps — do not relearn these

* **Never** run `git checkout -- <file>` on the VM: the VM carries fixes that are
  not in `origin/main`, so a checkout silently reverts them.
* **`patch -U0` will not apply these bundles.** Use the scripts in `patches/`.
* **Keep artifacts inside the repo.** `/home/user/*.patch` and anything outside
  the checkout are lost when the sandbox restarts.
* `git pull` on a new session gives you `main` — that is `0bf913f`, i.e. **none**
  of sections 2 and 3 unless you replay `patches/`.
* Python is externally managed (PEP 668): `pip install` without a venv fails.
