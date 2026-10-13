# Rehearsing the WhatsApp flow with no phone (`tools/demo_wa_hub.py`)

The whole "creator pairs their number → pick the channel where they are admin →
test → deals keep flowing" journey is clickable **without** logging in a real
WhatsApp number. `tools/demo_wa_hub.py` is a stand-in for the Node hub: same
three endpoints the dashboard and the pipeline call, same JSON keys
(`{jid, name, kind, role, members}`), a pretend paired number, and three
channels it is an admin of — plus one it only follows, to prove the picker
hides what you cannot post to.

Real number paranoia is the point: pairing burns a live WhatsApp device, and a
rehearsal that posts to a wrong chat is exactly how a number gets reported.
Nothing here talks to WhatsApp, and no service file starts it.

> **Telugu gist:** ఇది **నకిలీ WhatsApp hub** — అసలు ఫోన్ లేకుండా డాష్‌బోర్డ్‌లో
> "pair అయింది → admin గా ఉన్న channel ని పేరు చూసి select చేయి → Test → deals పోతున్నాయి"
> అనే మొత్తం flow ను ప్రాక్టీస్ చేయడానికి. ఇది ఎప్పుడూ WhatsApp కి connect అవ్వదు,
> tests వేరే database (`/tmp/demo.sqlite3`) వాడతాయి — మీ అసలు DB కి తగలదు.

---

## 1. Start the stand-in

```bash
cd ~/Influencers-deals
WA_HUB_TOKEN=demo-token-not-real python3 tools/demo_wa_hub.py
# [demo-wa] listening on http://127.0.0.1:8088 as +919000011111 — 3 channels/groups
#           where it is admin, 1 where it is not
```

`DEMO_WA_PORT=8088` picks another port. Any token works; pass the same one to
the dashboard so the "wrong token" path is not what you are testing.

## 2. Point a scratch dashboard at it

Use a **throwaway database** — never the live one:

```bash
cd ~/Influencers-deals
HUB_ENV=development HUB_DB_PATH=/tmp/demo.sqlite3 HUB_SKIP_DOTENV=1 \
WA_HUB_URL=http://127.0.0.1:8088 WA_HUB_TOKEN=demo-token-not-real \
DASHBOARD_ADMIN_PASSWORD='demo-admin-password-16chars' \
.venv/bin/python -m influencer_hub.cli init
HUB_ENV=development HUB_DB_PATH=/tmp/demo.sqlite3 HUB_SKIP_DOTENV=1 \
WA_HUB_URL=http://127.0.0.1:8088 WA_HUB_TOKEN=demo-token-not-real \
DASHBOARD_ADMIN_PASSWORD='demo-admin-password-16chars' \
.venv/bin/python -m gunicorn --bind 0.0.0.0:5000 dashboard.wsgi:app
```

Open `http://<host>:5000`, sign in with `demo-admin-password-16chars`.

## 3. Walk the journey (what to look at)

| Step | Where | What proves it |
|---|---|---|
| Pair | creator page → *💬 Dedicated WhatsApp Account & Channel Linker* | status shows **Connected: +919000011111** (the stand-in is always paired; the QR/code paths behave the same as the real hub) |
| Pick by name | *⭐ Channels / Groups where +919000011111 is ADMIN* | `Ravi Loot Deals · channel · owner · 4.2k`, `Loot Zone Premium · channel · admin · 980`, `Family Loot Group · group · admin · 42` — **names, not JIDs**; `Somebody Else's Channel` is only in the collapsed *see all chats* list |
| Save | tick *Mirror main-channel posts here* → **Connect** | the creator page now lists the channel with that name (editable), status **Pending** |
| Activate | **Test** on the channel row | log line `[demo-wa] SEND to=120363002222222222@newsletter …` and the row turns **ready** |
| Post | let the worker run (or trigger a deal on the scratch DB) | the same log shows the deal going to that JID, signed with **that creator's own** Amazon tag |

Two negative cases are worth clicking too: stop the stand-in (Ctrl-C) and reload
— the page still renders, with an empty picker and an admin-list error note
(the picker is advisory; a pasted `https://whatsapp.com/channel/…` link still
works). And a channel you picked but never Tested stays **Pending**, so nothing
half-configured goes live.

## 4. Switch back to the real thing

Kill the stand-in and drop `WA_HUB_URL`/`WA_HUB_TOKEN` from the environment (or
set them to the real hub's values in `.env`). Everything the rehearsal wrote
lives in `/tmp/demo.sqlite3` — delete it; the live database was never opened.

## 5. What the tests pin

`tests/test_demo_wa_hub.py` drives the **real** client
(`influencer_hub.whatsapp_client`) against the stand-in over HTTP: the admin
list is exactly the admin chats, the plain chat list is unchanged, a send is
accepted and logged, a wrong token or a dead hub yields an **empty picker**
rather than an error page, and a deal reaches a channel saved from the picker.
If the stand-in ever drifts from `wa_hub/wa_manager.js`, that suite fails here
instead of during a live demo.
