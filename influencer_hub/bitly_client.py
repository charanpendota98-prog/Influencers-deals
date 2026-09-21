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
    
    # Support multiple comma-separated keys: pick the first valid token or fallback
    keys = [k.strip() for k in token.split(",") if k.strip()]
    if not keys:
        return long_url

    cache_key = f"{keys[0]}:{long_url}"
    if cache_key in BITLY_CACHE:
        return BITLY_CACHE[cache_key]

    for api_tok in keys:
        try:
            async with session.post(
                BITLY_API_URL,
                json={"long_url": long_url},
                headers={"Authorization": f"Bearer {api_tok}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8.0),
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    short_link = data.get("link")
                    if short_link:
                        BITLY_CACHE[cache_key] = short_link
                        return short_link
                elif resp.status == 429:
                    # Rate limit exceeded on this key, try next key in the pool!
                    continue
        except Exception:
            continue
    return long_url


async def shorten_urls(urls: Iterable[str], token: str | None = None) -> dict[str, str]:
    api_token = token or config.BITLY_API_KEY
    urls_list = list(urls)
    if not api_token or not urls_list:
        return {u: u for u in urls_list}

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(shorten_one(session, u, api_token) for u in urls_list))
    return dict(zip(urls_list, results))
