"""Self-healing 24/7 deal ingestion and dispatch daemon.

The worker owns one asyncio loop and one isolated Telethon session. It reads
already-joined dialogs, persists per-dialog message cursors, retries failed
deliveries without advancing past them, and backs off on unexpected failures.
"""
from __future__ import annotations

import asyncio
import logging
import random
import signal
from collections import defaultdict

from . import config, db, pipeline, puller, scheduler

logger = logging.getLogger("influencer_hub.worker")


def _failed_delivery(results: dict) -> bool:
    return any(
        str(status).strip().lower().startswith("failed")
        for per_channel in results.values()
        for status in per_channel.values()
    )


async def process_pending_batch(limit: int | None = None, use_dummy: bool = False) -> dict[str, int]:
    """Process one pull batch and advance each source cursor only when safe."""
    records = await puller.pull_new_deals(limit=limit, use_dummy=use_dummy)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        source_key = str(record.get("source_id") or "").strip()
        if source_key:
            grouped[source_key].append(record)

    handled = retried = 0
    for source_key, messages in grouped.items():
        messages.sort(key=lambda item: int(item.get("message_id") or 0))
        for record in messages:
            message_id = int(record.get("message_id") or 0)
            if message_id <= 0:
                logger.warning("Skipping message with invalid Telegram message ID for source %s", source_key)
                continue

            text = str(record.get("text") or "").strip()
            if text:
                deal = {"text": text, "source": str(record.get("source") or "")}
                try:
                    result = await pipeline.run_once([deal])
                except Exception:
                    logger.exception("Pipeline failed for source %s message %s", source_key, message_id)
                    retried += 1
                    break
                if _failed_delivery(result):
                    # Some target(s) may have succeeded; pipeline dedup makes
                    # their retry idempotent. Keep this source cursor unchanged
                    # so the failed target gets another attempt next poll.
                    logger.warning(
                        "Delivery failure for source %s message %s; cursor held for retry",
                        source_key, message_id,
                    )
                    retried += 1
                    break

            # Empty/media-only Telegram posts have no text parser input; mark
            # them handled so they cannot block later deals forever.
            db.set_worker_offset(source_key, message_id)
            handled += 1

    return {"pulled": len(records), "handled": handled, "retry_sources": retried}


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Run continuously; recover from transient failures with capped backoff."""
    stop_event = stop_event or asyncio.Event()
    db.init()
    scheduler.start_scheduler()
    poll_seconds = max(5, int(config.DEAL_WORKER_POLL_INTERVAL))
    max_backoff = max(poll_seconds, int(config.DEAL_WORKER_MAX_BACKOFF))
    failure_streak = 0
    logger.info(
        "Deal worker started (poll=%ss, batch=%s, initial=%s)",
        poll_seconds, config.DEAL_WORKER_BATCH_SIZE, config.DEAL_WORKER_INITIAL_BATCH_SIZE,
    )

    try:
        while not stop_event.is_set():
            try:
                stats = await process_pending_batch(limit=config.DEAL_WORKER_BATCH_SIZE)
                failure_streak = 0
                if stats["pulled"]:
                    logger.info(
                        "Poll complete: pulled=%s handled=%s retry_sources=%s",
                        stats["pulled"], stats["handled"], stats["retry_sources"],
                    )
                delay = poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                failure_streak += 1
                base_delay = min(max_backoff, poll_seconds * (2 ** min(failure_streak, 8)))
                delay = min(
                    max_backoff,
                    max(poll_seconds, base_delay + random.randint(0, min(10, poll_seconds))),
                )
                logger.exception(
                    "Unexpected worker error; recovering in %ss (failure streak %s)",
                    delay, failure_streak,
                )

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
    finally:
        scheduler.stop_scheduler()
        try:
            await pipeline.close()
        except Exception:
            logger.exception("Failed to close worker clients cleanly")
        logger.info("Deal worker stopped")


async def _main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            pass
    await run_forever(stop_event)


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, config._env("HUB_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:  # pragma: no cover - signal fallback
        logger.info("Worker interrupted")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by service process
    raise SystemExit(main())
