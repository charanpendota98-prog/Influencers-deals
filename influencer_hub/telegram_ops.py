"""Telegram channel operations.

Per the chosen design we CREATE the influencer's channel under OUR bot account
(reusing the existing bestgaa Telethon session) and add the posting bot as
admin. This keeps every channel fully under our control.

Requires: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION (and BOT_USERNAME
for the admin grant). These are only needed at call time, not import time.
"""
from __future__ import annotations

from . import config
from . import db

_TG_CLIENT = None


def _client():
    global _TG_CLIENT
    if _TG_CLIENT is not None:
        return _TG_CLIENT
    from telethon import TelegramClient
    if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
        raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set")
    _TG_CLIENT = TelegramClient(
        str(config.BASE_DIR / "influencer_hub" / config.TELEGRAM_SESSION),
        int(config.TELEGRAM_API_ID),
        config.TELEGRAM_API_HASH,
    )
    return _TG_CLIENT


async def create_channel_for_influencer(influencer_id: int, title: str,
                                        about: str = "", role: str = "broadcast") -> dict:
    """Create a broadcast channel, add the posting bot as admin, persist it.

    `role` is 'approval' (Amazon-native posts for associate review) or
    'broadcast' (full deals). Persisted so the pipeline knows how to render.
    """
    from telethon.tl.functions.channels import (
        CreateChannelRequest,
        EditAdminRequest,
        ExportInviteLinkRequest,
    )
    from telethon.tl.types import ChatAdminRights

    client = _client()
    if not client.is_connected():
        await client.connect()

    result = await client(CreateChannelRequest(title=title, about=about))
    channel = result.chats[0]

    admin_granted = False
    if config.BOT_USERNAME:
        try:
            bot = await client.get_input_entity(config.BOT_USERNAME)
            await client(EditAdminRequest(
                channel=channel,
                user_id=bot,
                admin_rights=ChatAdminRights(
                    post_messages=True, edit_messages=True, delete_messages=True,
                    invite_users=True, manage_call=False, other=True,
                ),
                rank="Poster",
            ))
            admin_granted = True
        except Exception as exc:  # pragma: no cover - depends on live API
            print(f"[tg] admin grant failed: {exc}")

    invite = ""
    try:
        invite = str(await client(ExportInviteLinkRequest(channel=channel)))
    except Exception:
        pass

    ident = getattr(channel, "username") or str(channel.id)
    db.add_channel(influencer_id, "telegram", ident,
                   invite_link=invite, status="ready" if admin_granted else "pending",
                   role=role)
    return {"identifier": ident, "invite": invite, "admin_granted": admin_granted, "role": role}


async def post_to_channel(identifier: str, text: str, media_path: str | None = None) -> None:
    client = _client()
    if not client.is_connected():
        await client.connect()
    entity = await client.get_input_entity(identifier)
    if media_path:
        await client.send_file(entity, media_path, caption=text)
    else:
        await client.send_message(entity, text)


async def disconnect() -> None:
    if _TG_CLIENT is not None and _TG_CLIENT.is_connected():
        await _TG_CLIENT.disconnect()
