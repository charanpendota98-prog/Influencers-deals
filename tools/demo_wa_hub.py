"""A stand-in WhatsApp hub: rehearse the pairing flow without touching a phone.

The real hub (`wa_hub/server.js`) owns a real WhatsApp Web socket per number, so
every rehearsal of "pair → pick the channel where I am admin → test post →
posts flow" would burn a live WhatsApp login (and, done carelessly, risk a
ban). This stand-in speaks the same three endpoints the dashboard and the
pipeline actually call, and answers them with a pretend paired phone:

* ``GET  /sessions/<key>``              → ``{status: "connected", phone: …}``
* ``GET  /sessions/<key>/chats``        → every chat the pretend account sees
* ``GET  /sessions/<key>/admin-chats``  → only the ones it is **admin/owner** of
* ``POST /sessions/<key>/send``         → logged, always accepted

Point the dashboard at it and the whole operator journey is clickable:

    WA_HUB_URL=http://127.0.0.1:8088 WA_HUB_TOKEN=<any token> \\
        python3 tools/demo_wa_hub.py

Nothing here is ever started by any service file, and nothing here talks to
WhatsApp. Run it next to a demo database (``HUB_DB_PATH=/tmp/demo.sqlite3``) —
never next to the live one.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The pretended paired number. Deliberately obviously fake.
DEMO_PHONE = "919000011111"

#: Where the paired number is admin/owner — what the dashboard dropdown shows.
#: Two broadcast channels and one group, exactly the shape `wa_manager.js`
#: returns: ``{jid, name, kind, role, members}``.
ADMIN_CHATS: list[dict] = [
    {"jid": "120363001111111111@newsletter", "name": "Ravi Loot Deals",
     "kind": "channel", "role": "owner", "members": 4210},
    {"jid": "120363002222222222@newsletter", "name": "Loot Zone Premium",
     "kind": "channel", "role": "admin", "members": 980},
    {"jid": "120363003333333333-1600000000@g.us", "name": "Family Loot Group",
     "kind": "group", "role": "admin", "members": 42},
]

#: A chat the account merely follows. It must **not** appear in the picker:
#: posting where you are not an admin is exactly how a number gets reported.
OTHER_CHATS: list[dict] = [
    {"jid": "120363009999999999@newsletter", "name": "Somebody Else's Channel",
     "kind": "channel", "role": "subscriber", "members": 12000},
]

ALL_CHATS: list[dict] = ADMIN_CHATS + OTHER_CHATS

#: Log of accepted sends, so a rehearsal can be asserted on.
SENT: list[dict] = []


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sessions: dict[str, dict] = {}

    # -- plumbing ---------------------------------------------------------
    def _reply(self, payload: dict, code: int = 200) -> None:
        body = _json_bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        expected = getattr(self.server, "token", "")
        if not expected:
            return True
        offered = self.headers.get("Authorization", "")
        return offered in {f"Bearer {expected}", expected}

    def _session(self, key: str) -> dict:
        return self.sessions.setdefault(
            key, {"key": key, "status": "connected", "phone": DEMO_PHONE})

    def log_message(self, *_args) -> None:  # keep the rehearsal log readable
        return

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server's API
        path = self.path.split("?")[0]
        if path == "/health":
            return self._reply({"ok": True, "sessions": len(self.sessions)})
        if not self._authorised():
            return self._reply({"ok": False, "error": "unauthorised"}, 401)
        if path == "/sessions":
            return self._reply({"sessions": list(self.sessions.values())})
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "sessions":
            if len(parts) == 2:
                return self._reply(self._session(parts[1]))
            resource = parts[2]
            if resource == "chats":
                return self._reply({"ok": True, "chats": ALL_CHATS})
            if resource == "admin-chats":
                return self._reply({"ok": True, "chats": ADMIN_CHATS})
            if resource == "qr":
                # Already paired: the dashboard shows "connected" instead of a QR.
                return self._reply({"ok": True, "qr": "", "status": "connected"})
            if resource == "pairing":
                # Pairing-code (OTP-style) flow: no code needed, already paired.
                return self._reply({"ok": True, "phone": DEMO_PHONE, "code": ""})
        return self._reply({"ok": False, "error": f"no route {path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - http.server's API
        if not self._authorised():
            return self._reply({"ok": False, "error": "unauthorised"}, 401)
        length = int(self.headers.get("Content-Length") or 0)
        payload: dict = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                payload = {}
        path = self.path.split("?")[0]
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "send":
            record = {"session": parts[1], "to": payload.get("to", ""),
                      "text": str(payload.get("text", ""))}
            SENT.append(record)
            print(f"[demo-wa] SEND to={record['to']} "
                  f"text={record['text'][:70]!r}", flush=True)
            return self._reply({"ok": True, "id": "demo-message-id"})
        if len(parts) == 2 and parts[0] == "sessions":
            session = self._session(parts[1])
            session.update({k: v for k, v in payload.items() if k in {"status", "phone"}})
            return self._reply({"ok": True, **session})
        return self._reply({"ok": False, "error": f"no route {path}"}, 404)


def make_server(port: int = 8088, token: str = "") -> tuple[ThreadingHTTPServer, str]:
    """Start the stand-in in a background thread; return ``(server, base_url)``.

    Port ``0`` picks a free port (what the tests use). Stop it with
    ``server.shutdown()``.
    """
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.token = token  # type: ignore[attr-defined]
    Handler.sessions.setdefault("inf-1-demo", {"key": "inf-1-demo",
                                               "status": "connected",
                                               "phone": DEMO_PHONE})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, bound_port = server.server_address[0], server.server_address[1]
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return server, f"http://{host}:{bound_port}"


def main() -> None:
    port = int(os.getenv("DEMO_WA_PORT", "8088"))
    token = os.getenv("WA_HUB_TOKEN", "")
    server, url = make_server(port=port, token=token)
    print(f"[demo-wa] listening on {url} as +{DEMO_PHONE} — "
          f"{len(ADMIN_CHATS)} channels/groups where it is admin, "
          f"{len(OTHER_CHATS)} where it is not", flush=True)
    print(f"[demo-wa] point the dashboard at it:  WA_HUB_URL={url} "
          f"WA_HUB_TOKEN={token or '(unset)'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[demo-wa] bye", flush=True)


if __name__ == "__main__":
    main()
