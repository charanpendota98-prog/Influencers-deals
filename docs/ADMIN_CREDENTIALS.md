# Admin, team, and service credentials

Keep credentials in the private server environment (`.env` or your secret manager), never in Git, screenshots, support messages, or chat. On a single VM, lock the file down with `chmod 600 .env`; do not share it with workers.

## What to configure

| Variable | Required? | What it does |
|---|---|---|
| `DASHBOARD_ADMIN_PASSWORD` | Required for dashboard sign-in; production requires 16+ characters with varied characters | Your owner/admin login. It can approve channels and manage settings. Use a unique password-manager value; prefer 20+ characters. Never give it to the team. |
| `DASHBOARD_SECRET_KEY` | Required in production unless the app has a valid private key file | Stable random Flask-session signing key. This is **not** a login password. Production requires at least 32 characters; keep it identical across workers and restarts. |
| `ADMIN_DELETE_PASSWORD` | Optional | Separate confirmation password for destructive actions and protected-vault changes. If omitted, it reuses the admin password. If set, make it unique and strong. |
| `ADMIN_TOTP_SECRET` | Strongly recommended | Enables authenticator-app two-factor verification for admin sign-in. Generate it once, add it to the authenticator, save a recovery copy privately, then put it in the server environment. |
| `DASHBOARD_TEAM_PASSWORD` | Optional; use only when team access is needed | Separate team sign-in at `/team`. It is add-only: team members cannot edit existing creators, approve destinations, or administer settings. Use a different 16+ character password and share it privately. |
| `WA_HUB_TOKEN` | Required in production and for a non-loopback WhatsApp Hub | Service-to-service credential between Flask and the WhatsApp Hub. Use at least 32 random bytes. It is not a user login and must never be exposed in browser code. |

### Generate random service secrets

Run these commands **on the server**, copy each result directly into the private environment, and do not paste the outputs into a chat or ticket:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'  # DASHBOARD_SECRET_KEY
python -c 'import secrets; print(secrets.token_urlsafe(48))'  # WA_HUB_TOKEN
PYTHONPATH=. python -m influencer_hub.cli totp --new                 # ADMIN_TOTP_SECRET
```

Use a password manager to create the admin, team, and optional delete-confirmation passwords; shell history and terminal logs are poor places to create or store human passwords. Set `HUB_ENV=production`, serve the dashboard through HTTPS/private access, and restart the relevant services after changing environment values.

## Other secrets (not dashboard passwords)

- `TELEGRAM_API_HASH` is a Telegram application secret. `TELEGRAM_API_ID` is the corresponding identifier. Telegram account authorization is a separate Telethon session; it is not a dashboard password.
- `BOT_TOKEN` is only for bot-specific Telegram features; it is separate from the Telegram user session.
- `AMAZON_CREATORS_API_CLIENT_SECRET` is for the optional Creators API. It is not an Amazon Associates tag.
- `EARNKARO_API_KEY` is an affiliate conversion credential; it may be configured in the protected affiliate vault instead of `.env`.
- Bitly and other affiliate-provider keys are service credentials. Set them only for the feature/profile that needs them.
- WhatsApp QR or phone-code pairing links a WhatsApp account; it does not replace the private `WA_HUB_TOKEN`.

The Setup Center shows whether credentials are configured and whether the dashboard password/key meet the policy, but it never displays their values. A “configured” status does not prove a provider account is authorized, has posting permission, or will deliver every message; verify through the provider's live checks and an explicit destination test post.
