# VERIFICATION — what to run, and what “working perfectly” looks like

Short, exact, copy-paste. Every command below is **read-only** with respect to
production content: it uses a temporary database, blanked credentials and never
edits `.env`. Do the steps in order on the VM and the result is known before a
single real deal is posted.

```bash
cd /home/ubuntu/Influencers-deals
git fetch origin arena/3496cf5b-influencers-deals
git checkout -B arena/3496cf5b-influencers-deals FETCH_HEAD
```

## 0b. The guided flow and the setup age (previous round)

```bash
# 1. the whole wizard, in four saves (admin or team session; CSRF from the page)
curl -s -b cookies -c cookies -d "_csrf_token=$CSRF" -d "name=Ravi Loots" \
     -d "amazon_tag=ravi-21" http://127.0.0.1:5000/guided/creator -o /dev/null -w "%{redirect_url}\n"
#   http://127.0.0.1:5000/guided?creator=62&step=channels
curl -s -b cookies -c cookies -d "_csrf_token=$CSRF" -d "creator=62" \
     -d "approval_channel=@ravi_amazon" -d "main_channel=@ravi_loots" \
     -d "whatsapp_channel=120363012345678901@newsletter" -d "mirror_whatsapp=1" \
     http://127.0.0.1:5000/guided/channels -o /dev/null -w "%{redirect_url}\n"
curl -s -b cookies -c cookies -d "_csrf_token=$CSRF" -d "creator=62" \
     -d "categories=electronics" -d "categories=home" -d "posting_schedule=09-23" \
     -d "price_filter=budget" -d "allow_amazon=1" -d "allow_hypd=1" \
     http://127.0.0.1:5000/guided/categories -o /dev/null -w "%{redirect_url}\n"
curl -s -b cookies -c cookies -d "_csrf_token=$CSRF" -d "creator=62" \
     http://127.0.0.1:5000/guided/finish -o /dev/null -w "%{redirect_url}\n"
#   …/guided?creator=62&step=post&done=1   → page shows "4 of 4 steps done" + the review

# 2. the same tag twice is refused at the first step, naming the owner
curl -s -b cookies -d "_csrf_token=$CSRF" -d "name=Copy Cat" -d "amazon_tag=ravi-21" -L \
     http://127.0.0.1:5000/guided/creator | grep -o 'already belongs to[^<]*'

# 3. a team member cannot open or post into somebody else's creator
curl -s -o /dev/null -w "%{http_code}\n" -b team-cookies -d "_csrf_token=$TEAM_CSRF" \
     -d "creator=62" -d "main_channel=@stolen_channel" http://127.0.0.1:5000/guided/channels
#   302 → /guided, page says "added by somebody else", channels of #62 unchanged,
#   audit row team_blocked_route

# 4. the age the owner reads, and the export columns behind it
curl -s -b cookies "http://127.0.0.1:5000/setup" | grep -o 'Setup ages:[^<]*'
curl -s -b cookies "http://127.0.0.1:5000/api/inventory.csv" | head -1 | cut -d, -f1-7
#   creator_id,name,added_by,created_date,created_at,setup_age,setup_age_days
curl -s -b cookies "http://127.0.0.1:5000/setup?sort=newest&since=2026-09-01" | grep -c creator-directory-card
PYTHONPATH=. .venv/bin/python -c "from influencer_hub import age; print(age.human_age('2025-07-14'), '|', age.long_age('2025-07-14'), '|', age.setup_label('2025-07-14', member='Mohan'))"
#   1 year 3 months | 1 year 3 months | Set up 1 year 3 months ago · 2025-07-14 · by Mohan
```

**Observed on the 60-creator demo hub** (branch code, gunicorn on :5000):

| Check | Result |
|---|---|
| the four wizard saves | `302` each, redirecting to `&step=channels` → `&step=categories` → `&step=post` → `&step=post&done=1` |
| what landed in the database | creator `guideddemo-21`, approval `@guideddemo_preview`, main `@guideddemo_loots` (mirror `1`), WhatsApp `120363019999999999@newsletter` (`pending`), `categories=electronics,home`, hours `09-23`, price `budget`, `allow_amazon=1`, `allow_hypd=1` |
| the review page | `4 of 4 steps done`, every line ✅, “Send a test deal now” present |
| audits | `guided_creator_created`, `guided_channels_saved`, `guided_deals_saved`, `guided_finished` |
| team member posting into the owner's creator | `302`, *“added by somebody else”*, **channels of #62 unchanged**, `team_blocked_route` recorded |
| team member returning to a creator they added earlier | refused the same way — the wizard is an add flow, so a saved creator is only changed by the owner |
| team member on `/setup` / `/api/inventory.csv` | `302` → `/team` and `403 admin_only` |
| page timings (60 creators) | `/guided` **3 ms**, `/setup` 68 ms, `/easy-setup` 61 ms, `/` 126 ms, `/api/assignments` 4 ms |

## 0c. The money map and one-line adds (round before that)

```bash
# 1. nobody's money is attached to somebody else (read-only, exit 1 when it is)
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli assignments --fix-hint
curl -s -b cookies "http://127.0.0.1:5000/api/assignments" | head -c 400

# 2. the door refuses a row that would mix (team page, with the team sign-in)
curl -s -b team-cookies -L -d "_csrf_token=$CSRF&name=Copy Cat&amazon_tag=ravi-21" \
     -d "main_channel=@copycat_loots" http://127.0.0.1:5000/team/add | grep -o 'already belongs to[^<]*'
#   ⛔ Amazon tag "ravi-21" already belongs to Ravi Loots …   (nothing was added)

# 3. the same line in any shape is understood, before it is written
curl -s -b team-cookies -d "_csrf_token=$CSRF" \
     --data-urlencode $'bulk_lines=Ravi Loots ravi-21 @ravi_loots @ravi_amazon_deals 120363012345678901@newsletter +91 98765 43210' \
     http://127.0.0.1:5000/team/preview
#   {"total":1,"ready":1,…,"rows":[{"name":"Ravi Loots","tag":"ravi-21","main":"@ravi_loots",…}]}

# 4. what every new creator starts with (set once for the team)
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli add-defaults --only-amazon on --store 93944
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli add-defaults --show
```

**Observed on a 60-creator demo hub** (branch code, gunicorn on :5000):

| Check | Result |
|---|---|
| `cli assignments` with one duplicated tag | exit `1`, `⛔ Two creators share one Amazon tag — Ravi Loots (#1), Ravi Ratnam (#60)`, the tag and both fixes printed |
| `GET /api/assignments` | `200`, `ok:false`, `counts.high:1`, the finding naming both creators |
| team member on `/team/add` with `ravi-21` | refused, page says *already belongs to Ravi Loots*; creator count unchanged; audit `add_blocked_duplicate` |
| `/team/preview` with three scrappy lines | `200` in **~4 ms**: line 1 understood (name, tag, main, approval, WhatsApp, phone), line 2 reported twice (no tag, no channel), line 3 fine |
| `add-defaults --only-amazon on --store 93944` | saved, audited (`add_defaults_changed`), team page sentence updated |
| money-map scan, 1000 creators + 1000 channels | **45 ms**; `GuardIndex.from_db` 15 ms; one preflight < 0.1 ms |
| self-check | 14 items, `Money map (tags & channels)` amber while mixed, green after the fix |

## 0. The own-tag proof (this round)

```bash
# 1. the fleet proof, in one command — read-only, no network, exit 1 if anything is held
python -m influencer_hub.cli tag-proof            # "⛔ N of M active channel(s) …" when somebody is on the shared tag
python -m influencer_hub.cli tag-proof --json | python -m json.tool | head -20

# 2. one creator: the exact link their channels will publish
python -m influencer_hub.cli tag-proof --creator 62
#   Ravi Loots — own tag: ravi-21 (stored: ravi-21)
#     Amazon links will read: www.amazon.in/dp/B0D9P2M1PB?tag=ravi-21
#     ✅ own  @ravi_loots [creator] tag=ravi-21
python -m influencer_hub.cli tag-proof --creator 41     # a creator with no own tag
#   No Tag Yet — own tag: (none) (stored: mama086-21)
#     ⛔ held  @notag [creator] tag=mama086-21 (the hub's shared id)
#   exit 1

# 3. the same picture in the dashboard (admin session)
curl -s -b cookies http://127.0.0.1:5000/api/tag-proof | python -m json.tool | head -25
curl -s -b cookies "http://127.0.0.1:5000/api/tag-proof?creator=62" | python -m json.tool | head -25
curl -s -b cookies http://127.0.0.1:5000/setup | grep -o 'id="tag-proof"'      # the card is on the page
curl -s -b cookies http://127.0.0.1:5000/ | grep -o 'held — no own Amazon tag' # the home chip, only while somebody is held

# 4. the hold itself, end to end (no real post: the pipeline logs, parks and audits)
HUB_ENV=development HUB_DB_PATH="$(mktemp /tmp/tag-hold.XXXXXX.sqlite3)" HUB_SKIP_DOTENV=1 \
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_tag_proof.py
#   25 passed — includes the pipeline hold (nothing posted, deferred row reason
#   no_own_amazon_tag, audit action amazon_tag_hold), the own-tag post, the
#   WhatsApp mirror carrying the same tag, the approval channel being held too,
#   the REQUIRE_OWN_AMAZON_TAG=off escape hatch, the worker keeping the retry
#   slot, and the 1001-creator fleet pass reading two queries
```

**What is expected:** `tag-proof` exits `0` on a clean hub and `1` while any
active channel would post under the shared id; `/api/tag-proof` lists exactly the
creators named by the CLI and the self-check **Amazon attribution** item; a held
creator's Amazon deal never reaches a channel — it waits in the retry queue with
the reason `no_own_amazon_tag` and posts (signed with their own tag) on the first
worker pass after the tag is saved. A creator *without* an Amazon link in a deal
is unaffected, so EarnKaro/HYPD/Meesho posts keep flowing.

## 1. Code tests (services stopped, no credential read at all)

```bash
TEST_DB="$(mktemp /tmp/influencer-hub-deploy-test.XXXXXX.sqlite3)"
HUB_ENV=development HUB_DB_PATH="$TEST_DB" HUB_SKIP_DOTENV=1 \
PYTHONPATH=. .venv/bin/python -m pytest -q
RC=$?; rm -f "$TEST_DB"; echo "pytest exit code: $RC"

cd wa_hub && node --test
```

**Expected:** `652 passed` and `# pass 8 / # fail 0`. `HUB_SKIP_DOTENV=1` makes
the run ignore the VM's private `.env` entirely (`config._load_dotenv` returns
before reading the file), so no real Telegram session, affiliate key, shortener
token or bot credential exists for the duration of the run: no real post, no
Telegram login and no affiliate call can happen. The alternative “blank every
credential” form works too, but blanking variables that carry a documented
default (`TELEGRAM_API_ID`, `AMAZON_CREATORS_API_CLIENT_ID`) also disables those
defaults, so tests that assert them would fail for the wrong reason.

## 2. Schema (additive, safe on the live DB)

```bash
PYTHONPATH=. .venv/bin/python -c "from influencer_hub import db; db.init(); print('schema ok')"
```

Adds, if missing: `channels.mirror_whatsapp`, `posts.mirror_source_id`,
`wa_sessions.pairing_phone/pairing_code/pairing_error`, and the index
`idx_posts_channel_status`. Re-running is a no-op.

## 3. Start the stack, then read the self-check

```bash
sudo systemctl start influencer-wa-hub          # WhatsApp hub (loopback only)
sudo systemctl start influencer-dashboard       # Flask dashboard on :5000
# leave the worker stopped for now
curl -s -H "Authorization: Bearer $WA_HUB_TOKEN" http://127.0.0.1:8088/health
```

Open the dashboard → **🧪 Self-check — does everything really work?** (or
`/api/self-check?live=1`). It prints one line per subsystem and, for anything
not green, the exact fix:

| Check | Green means |
|---|---|
| Database & schema | every column/index this build needs exists |
| Telegram account | API id/hash set **and** an authorized session file |
| Dashboard access | admin password set, production cookie flags |
| Background worker | a heartbeat exists and is not degraded |
| Deal sources | at least one source is switched on |
| Amazon attribution | fallback tag set and **no creator on the shared id** |
| Central EarnKaro / HYPD | our key, publisher id and store are configured |
| Creator readiness | every creator has tag/main/WhatsApp/phone/live |
| Main → WhatsApp mirror | at least one creator is mirroring |
| WhatsApp hub | the hub answered on the loopback port |

The worker check is expected to be red until step 5 — that is the point of the
check, not a failure of the deploy.

## 4. Per-creator proof (no real deal needed)

For each creator that must post:

1. **Own tag** — creator page → *Influencer Profile Settings* → their Associate
   tag (e.g. `ravi-21`). The dashboard's `🏷️ Shared Amazon tag` filter must be
   empty when you are done: nobody may still post under the shared fallback.
2. **Phone** — creator page → *💬 Dedicated WhatsApp Account & Channel Linker* →
   scan the QR **or** type the number and get the 8-character login code. Status
   turns green (read-only polling; the browser never creates a session).
3. **WhatsApp destination** — paste the Channel link → saved **Pending**.
4. **Test** — press *🚀 Send Test Alert* once. Only a visible success flips it to
   **ready**; then the mirror badge shows **ACTIVE**.
5. **Dry run first** — Setup Center → *Live Deal Conversion Simulator* shows the
   exact text and the tagged link for a pasted deal, and sends nothing.

## 5. Start posting

```bash
sudo systemctl start influencer-deal-worker
sudo journalctl -u influencer-deal-worker -n 50 --no-pager
```

Watch the dashboard: `/api/flow` (worker, sources, channels), the command
center (per-creator state), and Money Radar (every posted link carries the
expected attribution). Then treat the self-check as the daily glance: all ten
checks green is the definition of "everything works".

## 5b. Let it watch itself (optional, recommended)

```bash
# every 15 minutes: alert the ops channel only when the verdict got worse,
# when it is still broken after the 3h cooldown, or once when it recovers
*/15 * * * * cd /home/ubuntu/Influencers-deals && PYTHONPATH=. .venv/bin/python -m influencer_hub.cli self-check --alert >> /var/log/influencer-selfcheck.log 2>&1

# daily 09:00 IST card to the ops channel (needs OPS_TELEGRAM_CHANNEL in .env)
0 9 * * * cd /home/ubuntu/Influencers-deals && PYTHONPATH=. .venv/bin/python -m influencer_hub.cli digest --send >> /var/log/influencer-digest.log 2>&1
```

Both commands are read-only apart from the optional ops message. Hand the
offline work to the exports: **Creator checklist (CSV)**, **Bulk template
(CSV)** and **Posts log (CSV)** are on the dashboard's 📈 Ops digest card
(`/api/readiness.csv`, `/api/easy-setup/template.csv`, `/api/posts.csv?hours=24`).

```bash
# history: one row per day, so the card can show a trend (and the self-check
# has something to read). `digest --send` stores the same row automatically.
5 0 * * * cd /home/ubuntu/Influencers-deals && PYTHONPATH=. .venv/bin/python -m influencer_hub.cli snapshot --note nightly >> /var/log/influencer-snapshot.log 2>&1

# backup: consistent copy (online backup API, safe while the worker posts),
# 14 kept, sidecar JSON next to each file. Secrets stay in .env, not in a backup.
30 2 * * * cd /home/ubuntu/Influencers-deals && PYTHONPATH=. .venv/bin/python -m influencer_hub.cli backup --out /var/backups/hub --keep 14 >> /var/log/influencer-backup.log 2>&1
```

Monitoring already on the VM can scrape `GET /api/metrics` (Prometheus text:
posts by status, worker up, creator readiness, self-check verdict, green streak).
A restore is one command and is reversible:

```bash
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli restore /var/backups/hub/hub-20261011-023000.sqlite3        # asks first
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli restore /var/backups/hub/hub-20261011-023000.sqlite3 --yes    # keeps the old file as *.pre-restore-*
```

## 5c. Prove each creator's Amazon tag (the promise to them)

```bash
# 1. what the sources carry: paste/point at a real source post
cat > /tmp/post.txt <<'EOF'
boAt Airdopes 141 at ₹999
Buy: https://www.amazon.in/dp/B09N3ZNHTY/ref=x?tag=someoneelse-21
Short: https://amzn.to/3xYzAbC
EOF
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli amazon-scan --file /tmp/post.txt

# 2. every creator, in one pass: the fleet must be ALL own tags (exit 1 otherwise)
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli amazon-scan --file /tmp/post.txt --fleet --check

# 3. one creator, exactly what their channel would receive
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli amazon-scan --file /tmp/post.txt --creator 1

# 4. after the worker has posted: read the messages we really sent
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli amazon-scan --report --hours 48 --check
```

Step 4 must print every Amazon link with its creator's own tag, and `--check`
must exit 0. A link carrying someone else's tag fails the check *and* the
dashboard self-check ("Amazon links in real posts"), naming the creator, the
tag found and the tag expected. The same numbers are at `GET /api/amazon-report`
and on the 📈 Ops digest card.

## 5d. Team access and the owner's two-factor

```bash
# 1. on the server: give the team its own password and switch on admin 2FA
python -c "import secrets; print('DASHBOARD_TEAM_PASSWORD=' + secrets.token_urlsafe(18))"
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli totp --new     # copy ADMIN_TOTP_SECRET=
# add BOTH lines to .env, then restart the dashboard
sudo systemctl restart influencer-dashboard

# 2. the admin door now asks for a code
curl -s http://127.0.0.1:5000/login | grep -c 'name="totp"'      # 1 means it is on
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli totp          # prints the current code
```

3. Open `http://<vm>:5000/team` in a browser (or on a phone) and sign in with a
   member name + the team password. Add one creator — it appears immediately with
   its next step. Try to open `http://<vm>:5000/setup`: it must bounce back to the
   team page, and `/setup → 👥 Team access & audit` (as admin) must show
   `team_blocked_route` for that attempt.

4. The audit card is the record: `creator_added` lines name the member, the
   creator, their tag and the channels; `change:<route>` lines name every admin
   edit. If a member cannot sign in, five wrong tries block their IP for 15
   minutes by design — wait, or clear it with the audit card's next attempt.

## 5e. Creator inventory: export everything, edit it, paste it back

```bash
# 1. one click: every creator as one Excel-ready row (admin only)
curl -s -b cookies "http://127.0.0.1:5000/api/inventory.csv" -o inventory.csv
head -1 inventory.csv | tr ',' '\n' | head -8
#   creator_id name added_by created_date created_at active ready amazon_tag

# 2. the same file on the command line, and a preview of a changed file
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli export-inventory --out inventory.csv
$EDITOR inventory.csv                     # change one amazon_tag or one notes cell
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli import-inventory --file inventory.csv
#   rows read: 1001 · would change: 1 · unchanged: 1000 · would create: 0 · cannot apply: 0
#     line   501 Sneha Loots 50:
#             amazon_tag: creator0503-21 → brand-new-21

# 3. in the browser: /setup → 📦 Creator inventory → paste/upload → “Preview the
#    changes” (nothing is written) → “✅ Apply the changes” (writes, audits, and
#    leaves the per-row result table on the page)
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli import-inventory --file inventory.csv --apply
```

**Observed on a 1001-creator hub** (branch code, gunicorn on :5000):

| Step | Result |
|---|---|
| `GET /api/inventory.csv` | `200`, 258 649 bytes, **0.23 s** (was 18 s before the batch fix) |
| paste of the edited 256 KB file | preview rendered in **0.15 s**; session cookie **558 bytes** |
| the same paste before the stash fix | cookie **23 349 bytes** — Werkzeug: *“the cookie is too large … browsers may silently ignore cookies larger than this (4093)”* |
| “✅ Apply the changes” | `inventory_imported: 1 updated, 0 created, 0 failed, 1000 unchanged` + `creator_updated: inventory import: notes` |
| a cell set to `-` | clears that field only (`notes` back to empty) |
| team member on `/api/inventory.csv` | `403 admin_only` (export is the owner's door) |

Nothing in this flow sends a message, touches a channel or changes a tag
without the owner pressing Apply: the preview is a pure plan
(`inventory.plan_import` writes nothing).

## 6. If a check is not green

The panel names the fix. The short versions:

* **Telegram** red → `python -m influencer_hub.cli doctor` (and `authorize` once).
* **Worker** red → `sudo systemctl status influencer-deal-worker` +
  `journalctl -u influencer-deal-worker -n 50`.
* **Amazon** amber → that creator still uses the shared tag; save their own.
* **Creators** amber → dashboard → filter `⚠️ Blocked` → do the listed action.
* **Mirror** amber → the destination has to pass one visible Test post first
  (by design: nothing half-configured goes live).
* **WhatsApp hub** red → hub service, `WA_HUB_TOKEN` match, then retry.
