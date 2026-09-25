"""VM / bot health watcher.

Collects host metrics (cpu, memory, disk) and whether the bot process is alive,
persists them, and (optionally) pushes a compact report to the ops Telegram
channel. Designed to run forever on the VM as a systemd service.

Gracefully degrades: if psutil is missing it reads /proc; if Telegram creds are
missing it only writes to the DB.
"""
from __future__ import annotations

import asyncio
import os
import time

from . import config, db


def _read_proc_stat() -> tuple[float, float]:
    """Return (cpu_idle_fraction baseline, total) by parsing /proc/stat."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = [float(x) for x in line.split()[1:]]
        idle = parts[3]
        total = sum(parts)
        return idle, total
    except Exception:
        return 0.0, 0.0


def cpu_percent(sample: float = 0.3) -> float:
    try:
        import psutil
        return psutil.cpu_percent(interval=sample)
    except Exception:
        i1, t1 = _read_proc_stat()
        time.sleep(sample)
        i2, t2 = _read_proc_stat()
        if t2 == t1:
            return 0.0
        return round(100.0 * (1 - (i2 - i1) / (t2 - t1)), 1)


def mem_percent() -> float:
    try:
        import psutil
        return psutil.virtual_memory().percent
    except Exception:
        try:
            with open("/proc/meminfo") as f:
                info = dict(line.split(":") for line in f if ":" in line)
            total = float(info["MemTotal"].split()[0])
            avail = float(info.get("MemAvailable", info["MemFree"]).split()[0])
            return round(100.0 * (1 - avail / total), 1)
        except Exception:
            return 0.0


def disk_percent(path: str = "/") -> float:
    try:
        import psutil
        return psutil.disk_usage(path).percent
    except Exception:
        try:
            st = os.statvfs(path)
            return round(100.0 * (1 - st.f_bavail / st.f_blocks), 1)
        except Exception:
            return 0.0


def bot_running() -> bool:
    """Best-effort check that the bestgaa bot process is alive."""
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            cl = " ".join(p.info.get("cmdline") or [])
            if "main_bot_new.py" in cl or "bestgaa" in cl:
                return True
        return False
    except Exception:
        return False


async def snapshot_and_report() -> dict:
    snap = {
        "cpu_pct": cpu_percent(),
        "mem_pct": mem_percent(),
        "disk_pct": disk_percent("/"),
        "bot_running": bot_running(),
    }
    db.record_vm(**snap)

    if config.OPS_TELEGRAM_CHANNEL:
        try:
            from . import telegram_ops
            icon = "🟢" if snap["bot_running"] else "🔴"
            msg = (
                f"{icon} VM Health\n"
                f"CPU: {snap['cpu_pct']}%\n"
                f"MEM: {snap['mem_pct']}%\n"
                f"DISK: {snap['disk_pct']}%\n"
                f"Bot: {'running' if snap['bot_running'] else 'DOWN'}"
            )
            await telegram_ops.post_to_channel(config.OPS_TELEGRAM_CHANNEL, msg)
        except Exception as exc:  # pragma: no cover
            print(f"[vm-watch] report send failed: {exc}")
    return snap


async def watch_loop() -> None:  # pragma: no cover - long running
    while True:
        await snapshot_and_report()
        await asyncio.sleep(config.VM_WATCH_INTERVAL)
