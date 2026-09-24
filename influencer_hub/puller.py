"""Deal puller — reads the shared source channels via Telethon.

The influencer hub is a SEPARATE redistribution layer: it reads the same shared
deal pool (config.SHARED_SOURCES, plus config.DUMMY_SOURCES for influencers
flagged use_dummy_sources) and renders each deal per influencer. This module
only needs credentials when actually called.
"""
from __future__ import annotations

from typing import Iterable

from . import config, db, telegram_ops


# Top Tier Priority Channels (First Preference Ingestion Engine)
PRIORITY_SOURCE_SPECS = [
    "https://t.me/+O3j4ghbtJzhjZjJl",
    "https://t.me/+8KzU3P58MJ9jN2M1",
    "https://t.me/+6LA1ljXGlbNmMjA1",
]


def _source_list(use_dummy: bool) -> list[str]:
    srcs = list(config.SHARED_SOURCES)
    if use_dummy:
        srcs = list(config.DUMMY_SOURCES) + srcs
    # Include all active sources stored in the database
    try:
        db_sources = [s["spec"] for s in db.list_sources(active_only=True) if s.get("spec")]
        for s in db_sources:
            if s not in srcs:
                srcs.append(s)
    except Exception:
        pass

    # Ensure valid specs
    valid_srcs = [s for s in srcs if s]

    # Sort so that the 3 Main Priority Channels ALWAYS come first:
    # 1. https://t.me/+O3j4ghbtJzhjZjJl (Shopsy Loots Official)
    # 2. https://t.me/+8KzU3P58MJ9jN2M1 (Mega Loot Deals)
    # 3. https://t.me/+6LA1ljXGlbNmMjA1 (Meesho Deals Official)
    priority_order = {spec: idx for idx, spec in enumerate(PRIORITY_SOURCE_SPECS)}
    sorted_sources = sorted(valid_srcs, key=lambda s: priority_order.get(s, 999))
    return sorted_sources


async def pull_recent_deals(limit: int = 10, use_dummy: bool = False) -> list[str]:
    """Return raw message texts from the configured source channels."""
    sources = _source_list(use_dummy)
    if not sources:
        return []
    client = telegram_ops._client()
    if not client.is_connected():
        await client.connect()
    out: list[str] = []
    for src in sources:
        try:
            async for msg in client.iter_messages(src, limit=limit):
                if msg and msg.message:
                    out.append(msg.message)
        except Exception as exc:  # pragma: no cover - network/deps
            print(f"[puller] source {src} failed: {exc}")
    return out
