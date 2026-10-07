# HANDOFF — Influencers-deals

**Updated:** 2026-10-07 (UTC)

**Session branch:** `arena/9056711b-influencers-deals`

**Checkout base:** `c6fa901` — PR #7 merged to `main`

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

## 2. Test and audit commands

From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
python3 -m influencer_hub.cli doctor --telegram-sources
```

The test suite on this revision is expected to report **320 passed**. The
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
python3 patches/apply-private-source-fallback.py --check
python3 patches/apply-private-source-fallback.py --dry-run
python3 patches/apply-private-source-fallback.py --with-tests
python3 -m influencer_hub.cli doctor --telegram-sources
sudo systemctl restart influencer-deal-worker influencer-dashboard
systemctl is-active influencer-deal-worker influencer-dashboard
journalctl -u influencer-deal-worker -n 100 --no-pager
```

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
3. Check the worker log for pipeline filtering or delivery errors.
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

- `patches/private-source-fallback.patch` contains the follow-up patch for the
  merged `c6fa901` release.
- `patches/apply-private-source-fallback.py` supports `--check`, `--dry-run`, and
  `--with-tests`; it uses `git apply --check` and never resets the checkout.
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
