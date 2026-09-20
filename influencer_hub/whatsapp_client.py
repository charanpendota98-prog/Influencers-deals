"""Thin async HTTP client for the wa_hub (Node/baileys) multi-session service.

The hub owns the actual WhatsApp Web sockets (one per influencer number). This
client just speaks JSON over HTTP so the Python side stays simple and the heavy
WhatsApp lifting stays in Node (matching the proven bestgaa bridge).
"""
from __future__ import annotations

import asyncio

import aiohttp

from . import config


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.WA_HUB_TOKEN:
        h["Authorization"] = f"Bearer {config.WA_HUB_TOKEN}"
    return h


async def _request(method: str, path: str, json: dict | None = None, timeout: int = 30) -> dict:
    url = f"{config.WA_HUB_URL.rstrip('/')}{path}"
    async with aiohttp.ClientSession(headers=_headers()) as s:
        async with s.request(method, url, json=json, timeout=timeout) as resp:
            try:
                return await resp.json()
            except Exception:
                return {"ok": resp.status == 200, "status": resp.status}


async def list_sessions() -> list[dict]:
    return (await _request("GET", "/sessions")).get("sessions", [])


async def list_chats(session_key: str) -> list[dict]:
    res = await _request("GET", f"/sessions/{session_key}/chats")
    return res.get("chats", [])
    return (await _request("GET", "/sessions")).get("sessions", [])


async def create_session(influencer_id: int, label: str) -> dict:
    """Start a new WA session for an influencer; returns a QR token to render."""
    return await _request("POST", "/sessions", json={
        "key": f"inf-{influencer_id}-{label}",
        "influencer_id": influencer_id,
        "label": label,
    })


async def get_qr(session_key: str) -> dict:
    return await _request("GET", f"/sessions/{session_key}/qr")


async def session_status(session_key: str) -> dict:
    return await _request("GET", f"/sessions/{session_key}")


async def send_text(session_key: str, to_jid: str, text: str) -> dict:
    return await _request("POST", f"/sessions/{session_key}/send", json={
        "to": to_jid, "text": text})


async def create_group(session_key: str, subject: str, participant: str) -> dict:
    """Create a WA group the influencer owns; we post deals there."""
    return await _request("POST", f"/sessions/{session_key}/group", json={
        "subject": subject, "participant": participant})


async def create_channel(session_key: str, name: str, description: str = "") -> dict:
    """Create an official WhatsApp Channel (newsletter) on the influencer number."""
    return await _request("POST", f"/sessions/{session_key}/newsletter", json={
        "name": name, "description": description})
