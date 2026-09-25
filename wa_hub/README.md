# wa_hub — multi-session WhatsApp service

Node/baileys service that holds **one WhatsApp-Web socket per influencer phone
number**. The Python `influencer_hub` talks to it over HTTP; it owns the live
WA connections (matching the proven `tg-wa-bridge` stack).

## Run

```bash
npm install
WA_HUB_PORT=8088 node server.js
```

On boot it auto-resumes any previously paired sessions from `./auth/<key>/`.

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | liveness |
| GET | `/sessions` | list sessions |
| POST | `/sessions` | start a session `{key, influencer_id, label}` |
| GET | `/sessions/:key` | status + phone |
| GET | `/sessions/:key/qr` | latest QR as a PNG data URL (null if connected) |
| GET | `/sessions/:key/qr/stream` | SSE: live QR + status |
| POST | `/sessions/:key/send` | `{to, text}` |
| POST | `/sessions/:key/group` | `{subject, participant}` → creates a group the influencer owns |
| POST | `/sessions/:key/newsletter` | `{name, description}` → creates an official Channel |
| POST | `/sessions/:key/stop` | logout + forget |

Auth: if `WA_HUB_TOKEN` is set, all routes require `Authorization: Bearer <token>`.

## Notes
- Each session persists creds under `./auth/<key>/`, so a restart does not need
  a re-scan (until the phone logs the session out).
- Official WhatsApp Channel **creation** works; automated **posting** to a
  newsletter is not exposed by this baileys build — the group feed is the
  automated path. See the top-level README for the rationale.
