"""Safe, manually composed Telegram and WhatsApp engagement polls.

Poll creation is intentionally operator-triggered rather than AI-generated or
scheduled. A durable global question history prevents exact and high-confidence
near-duplicate prompts from being published repeatedly.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from . import db, link_router

MAX_QUESTION_LENGTH = 255
MIN_OPTIONS = 2
MAX_OPTIONS = 10
MAX_OPTION_LENGTH = 100


def _clean_single_line(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    cleaned = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        cleaned.append(" " if category == "Cc" else char)
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()


def normalize_poll_question(value: str) -> str:
    """Canonicalize punctuation/case/spacing while preserving non-Latin scripts."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    chars = []
    for char in text:
        category = unicodedata.category(char)
        chars.append(char if category[0] in {"L", "M", "N"} else " ")
    return " ".join("".join(chars).split())


def validate_poll(question: str, options: Iterable[str]) -> tuple[str, list[str], str]:
    """Return clean fields or raise a readable validation error before sending."""
    clean_question = _clean_single_line(question)
    normalized_question = normalize_poll_question(clean_question)
    if not clean_question or not normalized_question:
        raise ValueError("Enter a poll question with at least one letter or number.")
    if len(clean_question) > MAX_QUESTION_LENGTH:
        raise ValueError(f"Poll questions must be {MAX_QUESTION_LENGTH} characters or fewer.")

    clean_options = [_clean_single_line(option) for option in options]
    clean_options = [option for option in clean_options if option]
    if not MIN_OPTIONS <= len(clean_options) <= MAX_OPTIONS:
        raise ValueError(f"Enter between {MIN_OPTIONS} and {MAX_OPTIONS} non-empty options.")
    if any(len(option) > MAX_OPTION_LENGTH for option in clean_options):
        raise ValueError(f"Each answer option must be {MAX_OPTION_LENGTH} characters or fewer.")

    option_keys = [normalize_poll_question(option) for option in clean_options]
    if any(not key for key in option_keys):
        raise ValueError("Each answer option must contain a letter or number.")
    if len(set(option_keys)) != len(option_keys):
        raise ValueError("This poll has duplicate answer options; make every option unique.")

    return clean_question, clean_options, normalized_question


def _enabled(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "default"}:
            return default
        if normalized in {"1", "true", "yes", "on", "enabled", "active"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled", "none", "null"}:
            return False
    return bool(value)


def _whatsapp_group(channel: dict) -> bool:
    platform = str(channel.get("platform") or "").strip().lower()
    identifier = str(channel.get("identifier") or "").strip().lower()
    # Newsletter/Channel polls are intentionally excluded: the current Baileys
    # newsletter path is text-only and poll delivery there is not verified.
    return identifier.endswith("@g.us") and platform in {"whatsapp_group", "whatsapp"}


def eligible_poll_targets(influencer: dict, channels: Iterable[dict]) -> list[dict]:
    """Ready, enabled, in-window Telegram channels and WhatsApp groups only."""
    if not _enabled(influencer.get("active"), default=True):
        return []

    targets: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for channel in channels:
        status = str(channel.get("status") or "").strip().lower()
        if status not in {"ready", "active"}:
            continue
        if str(channel.get("role") or "broadcast").strip().lower() == "approval":
            continue

        platform = str(channel.get("platform") or "").strip().lower()
        if platform == "telegram" and _enabled(influencer.get("telegram_enabled"), True):
            kind = "telegram"
        elif _whatsapp_group(channel) and _enabled(influencer.get("whatsapp_enabled"), True):
            kind = "whatsapp"
        else:
            continue

        schedule = channel.get("posting_schedule") or influencer.get("posting_schedule") or ""
        if not link_router.is_time_in_schedule(schedule):
            continue

        identifier = str(channel.get("identifier") or "").strip()
        # One visible post per destination even if the same group was linked
        # to multiple account sessions.
        dedupe_key = (kind, identifier.casefold())
        if not identifier or dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        target = dict(channel)
        target["poll_platform"] = kind
        targets.append(target)
    return targets


def enqueue_poll(
    influencer_id: int,
    question: str,
    options: list[str],
    normalized_question: str,
    allow_multiple: bool,
    targets: Iterable[dict],
) -> dict:
    """Atomically save a unique poll and queue its selected destinations."""
    target_list = list(targets)
    if not target_list:
        raise ValueError("At least one ready poll destination is required")
    poll_record = db.create_poll_question(
        influencer_id,
        question,
        normalized_question,
        options,
        allow_multiple,
        channel_ids=[int(channel["id"]) for channel in target_list],
    )
    if not poll_record.get("created"):
        return {
            "duplicate": True,
            "existing_question": poll_record.get("existing_question", ""),
            "duplicate_kind": poll_record.get("duplicate_kind", "exact"),
            "queued_count": 0,
        }
    return {
        "duplicate": False,
        "poll_id": poll_record["poll_id"],
        "queued_count": poll_record["destination_count"],
    }


async def process_pending_polls(limit: int = 1) -> dict[str, int]:
    """Drain a small durable queue in the existing worker, outside web requests."""
    from . import pipeline

    stats = {"processed": 0, "posted": 0, "failed": 0, "skipped": 0}
    for _ in range(max(1, min(20, int(limit)))):
        job = db.claim_next_poll_delivery()
        if not job:
            break

        poll_id = int(job["poll_id"])
        channel_id = int(job["channel_id"])
        channel = {
            "id": channel_id,
            "influencer_id": int(job["channel_influencer_id"]),
            "platform": job["channel_platform"],
            "identifier": job["channel_identifier"],
            "role": job["channel_role"],
            "status": job["channel_status"],
            "wa_session_key": job["wa_session_key"],
            "posting_schedule": job["channel_posting_schedule"],
        }
        influencer = {
            "id": channel["influencer_id"],
            "active": job["influencer_active"],
            "telegram_enabled": job["influencer_telegram_enabled"],
            "whatsapp_enabled": job["influencer_whatsapp_enabled"],
            "posting_schedule": job["influencer_posting_schedule"],
        }
        targets = eligible_poll_targets(influencer, [channel])
        if not targets:
            db.finish_poll_delivery(
                poll_id,
                channel_id,
                "skipped",
                error="Destination is no longer active, enabled, or inside its posting window.",
            )
            stats["processed"] += 1
            stats["skipped"] += 1
            continue

        try:
            result = await pipeline.dispatch_poll(
                targets[0],
                str(job["question"]),
                list(job.get("options") or []),
                allow_multiple=bool(job["allow_multiple"]),
            )
            message_id = ""
            if isinstance(result, dict):
                message_id = str(
                    result.get("id") or result.get("message_id")
                    or (result.get("key") or {}).get("id", "")
                )
                if result.get("ok") is False:
                    raise RuntimeError(str(result.get("error") or "platform rejected the poll"))
            db.finish_poll_delivery(
                poll_id, channel_id, "posted", platform_message_id=message_id
            )
            stats["posted"] += 1
        except Exception as exc:  # keep the remaining queued polls moving
            safe_error = str(exc).replace("\n", " ").strip()[:500] or "poll delivery failed"
            db.finish_poll_delivery(poll_id, channel_id, "failed", error=safe_error)
            stats["failed"] += 1
        stats["processed"] += 1
    return stats
