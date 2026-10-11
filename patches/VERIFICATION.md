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

## 1. Code tests (services stopped, credentials blanked)

```bash
TEST_DB="$(mktemp /tmp/influencer-hub-deploy-test.XXXXXX.sqlite3)"
HUB_ENV=development HUB_DB_PATH="$TEST_DB" \
TELEGRAM_API_ID="" TELEGRAM_API_HASH="" TELEGRAM_SESSION="" \
EARNKARO_API_KEY="" BITLY_API_KEY="" AMAZON_CREATORS_API_CLIENT_SECRET="" \
WA_HUB_TOKEN="" PYTHONPATH=. .venv/bin/python -m pytest -q
RC=$?; rm -f "$TEST_DB"; echo "pytest exit code: $RC"

cd wa_hub && node --test
```

**Expected:** `424 passed` and `# pass 8 / # fail 0`. Nothing in `.env` is read
for these runs, so no real post, no Telegram login and no affiliate call happens.

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
