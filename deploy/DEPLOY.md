# Deploy (VM) — one command, with a way back

Everything in this folder installs and runs the hub on **your** VM. Nothing here
touches a creator's channel by itself: the worker keeps posting only after a
restart, and the deploy refuses to continue when a check fails.

> **Telugu gist:** మొదటిసారి `install.sh` → `install_systemd.sh` → `.env` నింపి →
> `deploy.sh`. తర్వాత ప్రతి update కి `git pull` బదులు `./deploy/deploy.sh` ఒక్కటే
> చాలు: అది ముందు **read-only check** చేస్తుంది, DB **backup** తీస్తుంది, code ను
> fast-forward మాత్రమే చేస్తుంది, migrations వేసి, services restart చేసి, `/healthz` +
> self-check (`--blocking`) + tag-proof చూస్తుంది — unsafe rendered Amazon links ఉంటేనే
> **వెంటనే rollback** (పాత code + అదే backup). Own tag ఇంకా ఇవ్వని creator deals మాత్రం
> pipeline లో held అవుతాయి; అవి report అవుతాయి కానీ మిగతా hub updates ను block చేయవు.
> ⚠️ warnings వాటి fix తో print అవుతాయి. Credentials ఎప్పుడూ print అవ్వవు (ఉన్నాయా/లేవా మాత్రమే).

---

## 0. Before the first deploy

```bash
cd ~/Influencers-deals
./deploy/install.sh                       # venv + Python deps + wa_hub node modules + DB
cp .env.example .env && chmod 600 .env    # then fill: dashboard password, secret key,
                                          # Amazon tag, Telegram, EarnKaro, HYPD
./deploy/deploy.sh --check                # read-only: exactly what is missing, if anything
```

`--check` never changes anything. It prints one line per thing it looked at:

```
  [OK] branch        on main at 5b5696f (wanted main, remote 5b5696f)
  [OK] worktree      0 modified file(s), 3 untracked (usually .env, a venv, logs)
  [OK] virtualenv    /home/ubuntu/Influencers-deals/.venv/bin/python
  [OK] python-deps   flask 3.1.3 | aiohttp 3.14.5 | telethon 1.45.0 | gunicorn 23.0.0
  [OK] node          /usr/bin/node
  [OK] whatsapp-deps wa_hub/node_modules
  [OK] dotenv        /home/ubuntu/Influencers-deals/.env
  [OK] env:DASHBOARD_ADMIN_PASSWORD  set — dashboard sign-in
  [OK] env:DASHBOARD_SECRET_KEY      set — session cookies survive a restart
  [OK] env:AMAZON_ASSOCIATE_TAG      set — hub-wide Amazon fallback tag
  [OK] env:optional  7/9 capability keys set; missing BOT_TOKEN, ADMIN_TOTP_SECRET
  [OK] database      /home/ubuntu/Influencers-deals/influencer_hub/hub.sqlite3
  [OK] schema        reachable, 62 creator(s), migrations applied
  [OK] disk          19085 MB free beside the database
  [--] backups       no backups yet
  [--] systemd       4 unit(s): influencer-dashboard.service, …
  [OK] dashboard-today   http://127.0.0.1:5000/healthz
  [--] whatsapp-hub-today  port 8088
```

`[XX]` = required, the deploy stops. `[--]` = a warning, the deploy continues
(it tells you the one command that fixes each).

Install the units once, so a reboot brings everything back:

```bash
sudo ./deploy/install_systemd.sh
sudo systemctl enable --now influencer-wa-hub influencer-deal-worker \
                            influencer-dashboard influencer-vm-watch
```

---

## 1. Every update after that

```bash
cd ~/Influencers-deals
./deploy/deploy.sh                 # update to the branch this checkout is on
./deploy/deploy.sh --branch <name> # …or to another branch
./deploy/deploy.sh --ref <sha>     # …or pin exactly one commit
./deploy/deploy.sh --dry-run       # print every command first, change nothing
./deploy/deploy.sh --no-restart    # update code + database, leave services running
```

What it does, in order — and what it refuses to do:

| # | Step | Detail |
|---|---|---|
| 1 | preflight | the check above; **no** required failure is allowed through |
| 2 | backup | `cli backup` — SQLite's online backup, safe while the worker is posting |
| 3 | fetch | `git fetch --prune origin <branch>` |
| 4 | fast-forward | `git merge --ff-only` — **never** rewrites history, **never** discards a modified file (that is what the worktree check is for) |
| 5 | python deps | `pip install -r requirements.txt`, only when `requirements.txt` changed |
| 6 | WhatsApp deps | `npm ci --no-audit --no-fund`, only when `package-lock.json` changed. **`npm audit fix` is never run** — your verified lock stays exactly as it is |
| 7 | migrate | `cli init` adds the new columns; safe to re-run |
| 8 | restart | systemd units when they are installed, otherwise `deploy/start_services.sh` (which supervises and restarts every service) |
| 9 | health | `/healthz`, then `cli self-check --blocking`, then `cli tag-proof --allow-held`. Broken/mis-signed rendered links roll back; creators with no own tag are reported and safely held by the pipeline, without blocking unrelated deploys |
| 10 | rollback | if 9 fails: previous commit + the backup from step 2 + restart, then a non-zero exit |

Step 9 is what gates the deploy, and it gates on **failures only**: a check in the
⛔ state rolls the code and the database back; a check in the ⚠️ state — a worker
that is still starting, a creator whose phone is not paired yet, a snapshot not
taken yet — is printed with the one command that clears it and never rolls a
deploy back. That is why the script calls `cli self-check --blocking` and not the
plain form: on a hub that has not finished onboarding, the plain form would undo
every deploy. The tag proof uses `--allow-held` only for deployment: a creator
without an own tag still fails the strict `tag-proof` command and stays held at
send time, but that setup gap does not block unrelated safe updates. A malformed
or mis-signed sample link still fails the deployment gate.

The last lines are the receipt you can paste into Telegram:

```
[2026-10-14T09:12:33+00:00] DEPLOY OK · branch main · commit 5b5696f
  started:    2026-10-14T09:11:02+00:00
  backup:     /home/ubuntu/Influencers-deals/backups/hub-20261014-091102.sqlite3
  health:     http://127.0.0.1:5000/healthz answered · self-check: no failures · tag-proof safe (held creators reported)
  rollback:   ./deploy/deploy.sh --from-backup …   (database only)
              git reset --hard 5b5696f && ./deploy/deploy.sh --no-restart  (code)
```

---

## 2. When something looks wrong

```bash
./deploy/deploy.sh --check                     # what changed on the box?
python -m influencer_hub.cli self-check        # the hub's own verdict
curl -s http://127.0.0.1:5000/healthz          # is the dashboard up?
journalctl -u influencer-dashboard -n 50       # dashboard log (systemd installs)
tail -f logs/deal-worker.log                   # worker log (supervised installs)
```

**Database only** (a bad batch went out, the code is fine):

```bash
python -m influencer_hub.cli backup --out backups      # keep the current state first
./deploy/deploy.sh --from-backup backups/hub-20261014-091102.sqlite3
```

**Code only** (the new version misbehaves):

```bash
git reset --hard <previous sha> && ./deploy/deploy.sh --no-restart
sudo systemctl restart influencer-dashboard influencer-deal-worker   # or start_services.sh
```

**Full rollback** (both): restore the backup first, then reset the code, then
restart — in that order, so the database the old code runs on is the one it wrote.

---

## 3. Ports, firewall and access

The dashboard listens on `0.0.0.0:5000` so it answers on the VM's IP.

```bash
sudo ufw allow 5000/tcp          # only if you need direct access
sudo ufw allow 5001/tcp          # Telegram source polling UI, if used
```

Better than exposing 5000: keep the port closed and reach it with Tailscale
(`./deploy/enable_tailscale_serve.sh`) or an HTTPS reverse proxy
(then set `DASHBOARD_TRUST_PROXY=1`). The team page has its own password and is
add-only; the admin sign-in can require a 6-digit code
(`python -m influencer_hub.cli totp --new`).

---

## 4. What a deploy never does

* never starts posting on its own — the worker posts as soon as it runs, and it
  is the restart at step 8 that decides when that happens;
* never touches `.env` (it only reads which keys exist, never a value);
* never runs `npm audit fix`, `git reset --hard` or `git checkout -f` in the
  normal path (reset appears only inside the rollback, with the SHA printed);
* never deploys without a backup of the live database;
* never leaves you without the exact two commands that undo it.
