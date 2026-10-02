"""Background scheduler for periodic deal highlights.

Responsibilities:
1. Hourly highlight dispatcher: asks the pipeline to rank recent deals and
   dispatches the selected banner to eligible destinations.
2. Send pacing is handled by the pipeline; it reduces frequency but does not
   guarantee account safety or platform acceptance.
3. Database cleanup is handled by the relevant pipeline/DB maintenance tasks.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
import zoneinfo

from . import db, pipeline, config

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
_SCHEDULER_TASK: asyncio.Task | None = None
_RUNNING = False


async def _scheduler_loop():
    global _RUNNING
    _RUNNING = True
    print("[scheduler] Periodic deal-highlight scheduler started.")
    last_hourly_check = -1

    while _RUNNING:
        try:
            now_ist = datetime.now(IST)
            current_hour = now_ist.hour
            current_minute = now_ist.minute

            # Trigger hourly highlight around the top of the hour (minute 0-2) once per hour
            if current_hour != last_hourly_check and current_minute <= 2:
                print(f"[scheduler] Triggering hourly loot highlight for hour {current_hour}:00 IST")
                try:
                    await pipeline.run_hourly_loot_highlight()
                    last_hourly_check = current_hour
                except Exception as exc:
                    print(f"[scheduler] Hourly loot highlight error: {exc}")

            # Sleep 30 seconds before next check
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[scheduler] Unexpected loop error: {e}")
            await asyncio.sleep(10)


def start_scheduler():
    """Start background scheduler in current running asyncio event loop if available."""
    global _SCHEDULER_TASK
    try:
        loop = asyncio.get_running_loop()
        if _SCHEDULER_TASK is None or _SCHEDULER_TASK.done():
            _SCHEDULER_TASK = loop.create_task(_scheduler_loop())
    except RuntimeError:
        pass


def stop_scheduler():
    global _RUNNING, _SCHEDULER_TASK
    _RUNNING = False
    if _SCHEDULER_TASK and not _SCHEDULER_TASK.done():
        _SCHEDULER_TASK.cancel()
