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
| POST | `/sessions/:key/resolve-newsletter` | resolve a `whatsapp.com/channel/...` invite to its `@newsletter` JID |
| POST | `/sessions/:key/stop` | logout + forget |

All routes require `Authorization: Bearer <token>` when `WA_HUB_TOKEN` is set;
production refuses to start without it. The server binds to `127.0.0.1` by
default (`WA_HUB_HOST` can override this only for an intentional private setup).

## Notes
- Session identities and labels are saved in `sessions.json`; Baileys credentials
  persist under `./auth/<key>/`. On restart the hub resumes those credentials and
  derives `status` and `phone` from the live socket instead of restoring a stale
  `connected` claim. Transient disconnects retry with a new socket; a logged-out
  session clears its invalid auth files so an operator can pair again.
- Dashboard QR/status polling uses GET-only endpoints and does not start sessions,
  send messages, or activate destinations. Pairing and test posts remain explicit
  operator actions. Run the hub unit tests with `npm test`.
- The pinned Baileys build has a low-level text-message route for newsletter
  JIDs. The dashboard resolves Channel links to a real `@newsletter` JID, saves
  it as Pending, and only activates it after an operator's explicit test post
  succeeds. This tests the connected number's posting access; it cannot promise
  future delivery or prevent WhatsApp account restrictions.
- WhatsApp Channels are not exposed by Meta's official Cloud API. Baileys is an
  unofficial WhatsApp-Web integration, so this Channel path is best-effort and
  may break when WhatsApp changes its protocol. The deal pipeline sends text
  with links; do not assume media posts work for newsletters.
- A WhatsApp group invite link resolves metadata but does not silently join the
  group. The paired account must already be a member with posting permission
  before the test succeeds.
