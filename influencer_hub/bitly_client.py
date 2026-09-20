"""Bitly shortener client for multi-link clean formatting.

Used when posts contain multiple links (2+ links) or excessively long URLs,
to keep posts tidy, clean and legible across Telegram & WhatsApp channels.
Note: For the Amazon approval channel, native amazon.in links are always
preserved (Bitly is bypassed) to comply with Amazon associate policies.
"""
from __future__ import annotations

import asyncio
from typing import Iterable
import aiohttp

from . import config

BITLY_API_URL = "https://api-ssl.bitly.com/v4/shorten"
BITLY_CACHE: dict[str, str] = {}


async def shorten_one(session: aiohttp.ClientSession, long_url: str, token: str) -> str:
    if not token or not long_url:
        return long_url
    if long_url in BITLY_CACHE:
        return BITLY_CACHE[long_url]

    try:
        async with session.post(
            BITLY_API_URL,
            json={"long_url": long_url},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=8.0),
        ) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                short_link = data.get("link")
                if short_link:
                    BITLY_CACHE[long_url] = short_link
                    return short_link
    except Exception:
        pass
    return long_url


async def shorten_urls(urls: Iterable[str], token: str | None = None) -> dict[str, str]:
    api_token = token or config.BITLY_API_KEY
    urls_list = list(urls)
    if not api_token or not urls_list:
        return {u: u for u in urls_list}

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(shorten_one(session, u, api_token) for u in urls_list))
    return dict(zip(urls_list, results))
