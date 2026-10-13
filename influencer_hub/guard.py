"""The assignment guard: nobody's money (or channel) may be attached to somebody else.

Two creators sharing one Amazon tag means Amazon pays *one* of them for both
people's clicks. One Telegram channel saved on two creators means every deal is
posted twice, each copy signed with a different tag. Those are the two mistakes
this hub can make that cost real money, and they are invisible until the payout
report arrives — so the guard looks for them everywhere:

* :func:`findings` — a full, read-only scan of the hub (dashboard card, CLI and
  the self-check all read it);
* :class:`GuardIndex` — a pre-read index used *while adding* creators, so a bad
  row is refused with a name and a reason instead of being written.

Everything here is pure logic over rows the caller passes in (or reads once),
stdlib only, no network, and it never writes.
"""
from __future__ import annotations

import re

from . import config, db

HIGH = "high"
MEDIUM = "medium"
INFO = "info"

_SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, INFO: 2}

TAG_COLLISION = "tag_collision"
TAG_MISSING = "tag_shared_default"
TAG_INVALID = "tag_invalid"
CHANNEL_COLLISION = "channel_collision"
ROLE_CLASH = "role_clash"
NAME_COLLISION = "name_collision"

_LABELS = {
    TAG_COLLISION: "Two creators share one Amazon tag",
    TAG_MISSING: "Creator still posts with the hub-wide tag",
    TAG_INVALID: "Saved Amazon tag is not usable",
    CHANNEL_COLLISION: "One channel is saved on two creators",
    ROLE_CLASH: "Same channel is both the preview and the main channel",
    NAME_COLLISION: "Two profiles carry the same name",
}

ROLE_NAMES = {"approval": "approval (preview)", "broadcast": "main",
              "whatsapp": "WhatsApp"}


# --------------------------------------------------------------------------
# Normalising a channel identifier so "@Ravi_Loots", "t.me/ravi_loots" and
# "https://t.me/Ravi_Loots/" all count as the same destination.
# --------------------------------------------------------------------------
def channel_key(platform: str, identifier: str) -> str:
    """A stable key for one destination ("" when there is nothing to compare)."""
    raw = str(identifier or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            low = low[len(prefix):]
    low = low.split("?", 1)[0].split("#", 1)[0]
    if low.startswith(("t.me/", "telegram.me/", "telegram.dog/")):
        low = low.split("/", 1)[1]
        platform = "telegram"
    plat = str(platform or "").strip().lower()
    if low.endswith("@newsletter"):
        return "whatsapp:channel:" + low
    if low.endswith("@g.us"):
        return "whatsapp:group:" + low
    if low.endswith("@s.whatsapp.net"):
        digits = re.sub(r"\D", "", low)
        return f"whatsapp:phone:{digits[-10:]}" if digits else "whatsapp:id:" + low
    if plat.startswith("whatsapp"):
        digits = re.sub(r"\D", "", low)
        if digits and not re.search(r"[a-z]", low):
            return f"whatsapp:phone:{digits[-10:]}"
        return "whatsapp:id:" + low
    return "telegram:" + low.strip().strip("/").lstrip("@")


def _creator_label(profile: dict) -> str:
    return str(profile.get("name") or f"creator {profile.get('id')}")


def _creator_row(profile: dict) -> dict:
    return {"id": int(profile.get("id") or 0), "name": _creator_label(profile),
            "url": f"/influencer/{int(profile.get('id') or 0)}"}


def _finding(kind: str, severity: str, detail: str, creators: list[dict],
             fix: str, *, url: str = "", extra: dict | None = None) -> dict:
    finding = {
        "kind": kind,
        "label": _LABELS.get(kind, kind),
        "severity": severity,
        "detail": detail,
        "creators": [_creator_row(profile) for profile in creators],
        "fix": fix,
        "url": url or (f"/influencer/{int(creators[0].get('id') or 0)}" if creators else "/setup"),
    }
    if extra:
        finding.update(extra)
    return finding


def _effective_tags(influencers: list[dict]) -> dict[int, str]:
    return {int(inf.get("id") or 0): str(inf.get("amazon_tag") or "").strip()
            for inf in influencers}


def scan(influencers: list[dict] | None = None,
         channels: list[dict] | None = None) -> list[dict]:
    """Every assignment problem in the hub, worst first (read-only)."""
    from . import link_router

    influencers = list(db.list_influencers() if influencers is None else influencers)
    channels = list(db.list_channels() if channels is None else channels)
    shared_default = str(config.AMAZON_ASSOCIATE_TAG or "").strip()
    findings: list[dict] = []

    # --- Amazon tags -----------------------------------------------------
    by_tag: dict[str, list[dict]] = {}
    for inf in influencers:
        tag = str(inf.get("amazon_tag") or "").strip()
        if not tag or tag == shared_default:
            continue
        by_tag.setdefault(tag, []).append(inf)
    for tag, owners in sorted(by_tag.items()):
        if len(owners) < 2:
            continue
        names = " and ".join(_creator_label(profile) for profile in owners[:4])
        findings.append(_finding(
            TAG_COLLISION, HIGH,
            f"{names} all post with the Amazon tag “{tag}”. Amazon pays one account "
            "for those clicks, so two creators' earnings land in one place.",
            owners,
            "Save each creator's own Associate tag on their profile.",
        ))

    invalid = [inf for inf in influencers
               if str(inf.get("amazon_tag") or "").strip()
               and not link_router.amazon_tag_is_usable(str(inf.get("amazon_tag") or ""))]
    for inf in invalid:
        findings.append(_finding(
            TAG_INVALID, HIGH,
            f"{_creator_label(inf)} has “{inf.get('amazon_tag')}” saved, which is not a "
            "usable Amazon tag — their links would fall back to the hub-wide tag and "
            "pay somebody else.",
            [inf],
            "Open the profile and save this creator's real Associates tag (like ravi-21).",
        ))

    shared = [inf for inf in influencers
              if shared_default
              and str(inf.get("amazon_tag") or "").strip() == shared_default]
    if shared:
        names = ", ".join(_creator_label(profile) for profile in shared[:5])
        more = f" (+{len(shared) - 5} more)" if len(shared) > 5 else ""
        findings.append(_finding(
            TAG_MISSING, MEDIUM,
            f"{len(shared)} creator(s) still post with the hub-wide tag "
            f"“{shared_default}”: {names}{more}. Their deals earn, but under the shared "
            "id instead of their own.",
            shared,
            "Add each creator's own Associate tag (dashboard → Configured creators, "
            "or ⬇️ inventory export → edit the amazon_tag column → paste it back).",
            url="/setup#creator-directory",
        ))

    # --- channels --------------------------------------------------------
    by_channel: dict[str, list[dict]] = {}
    owners_by_id = {int(inf.get("id") or 0): inf for inf in influencers}
    for channel in channels:
        key = channel_key(channel.get("platform"), channel.get("identifier"))
        if not key:
            continue
        inf_id = int(channel.get("influencer_id") or 0)
        entry = {"id": inf_id, "name": _creator_label(owners_by_id.get(inf_id) or
                                                     {"id": inf_id, "name": None}),
                 "role": str(channel.get("role") or "broadcast"),
                 "identifier": str(channel.get("identifier") or "")}
        by_channel.setdefault(key, []).append(entry)
    for key, entries in sorted(by_channel.items()):
        creator_ids = sorted({entry["id"] for entry in entries})
        if len(creator_ids) < 2:
            continue
        profiles = [owners_by_id.get(inf_id) or {"id": inf_id, "name": None}
                    for inf_id in creator_ids]
        who = " and ".join(f"{entry['name']} ({ROLE_NAMES.get(entry['role'], entry['role'])})"
                           for entry in entries[:4])
        findings.append(_finding(
            CHANNEL_COLLISION, HIGH,
            f"The destination “{entries[0]['identifier']}” is saved on more than one "
            f"creator: {who}. Every deal would be posted there once per creator, each "
            "copy signed with a different Amazon tag.",
            profiles,
            "Keep it on the creator who owns that channel; delete it from the other "
            "profile (their page → Channels → Delete).",
        ))

    per_creator_roles: dict[int, dict[str, str]] = {}
    for channel in channels:
        inf_id = int(channel.get("influencer_id") or 0)
        role = str(channel.get("role") or "broadcast")
        ident = str(channel.get("identifier") or "").strip()
        if not inf_id or not ident or role not in {"approval", "broadcast"}:
            continue
        per_creator_roles.setdefault(inf_id, {})[role] = ident
    for inf_id, roles in sorted(per_creator_roles.items()):
        approval = roles.get("approval")
        main = roles.get("broadcast")
        if approval and main and channel_key("telegram", approval) == channel_key("telegram", main):
            profile = owners_by_id.get(inf_id) or {"id": inf_id, "name": None}
            findings.append(_finding(
                ROLE_CLASH, MEDIUM,
                f"{_creator_label(profile)} has “{main}” saved as both the approval "
                "(preview) channel and the main channel, so every deal would be reviewed "
                "in the same place it is broadcast.",
                [profile],
                "Save the real approval channel (usually a small private one) on the profile.",
            ))

    # --- names -----------------------------------------------------------
    by_name: dict[str, list[dict]] = {}
    for inf in influencers:
        name = str(inf.get("name") or "").strip().lower()
        if name:
            by_name.setdefault(name, []).append(inf)
    for name, owners in sorted(by_name.items()):
        if len(owners) < 2:
            continue
        ids = ", ".join(f"#{int(profile.get('id') or 0)}" for profile in owners)
        findings.append(_finding(
            NAME_COLLISION, MEDIUM,
            f"{len(owners)} profiles are called “{owners[0].get('name')}” ({ids}). Adds "
            "match by name, so a paste could land on the wrong one.",
            owners,
            "Rename or remove the duplicate profile; the inventory export lists both "
            "creator_id values.",
        ))

    findings.sort(key=lambda item: (_SEVERITY_ORDER.get(item["severity"], 9),
                                    item["kind"], item["creators"][0]["id"]))
    return findings


def counts(findings: list[dict] | None = None) -> dict:
    rows = scan() if findings is None else findings
    return {
        "high": sum(1 for item in rows if item["severity"] == HIGH),
        "medium": sum(1 for item in rows if item["severity"] == MEDIUM),
        "info": sum(1 for item in rows if item["severity"] == INFO),
        "total": len(rows),
    }


def is_clear(findings: list[dict] | None = None) -> bool:
    """True when nothing would send a creator's money to somebody else."""
    return counts(findings)["high"] == 0


def summary_line(findings: list[dict] | None = None) -> str:
    """One sentence for a card or a terminal."""
    rows = scan() if findings is None else findings
    counted = counts(rows)
    if not rows:
        return "Every creator owns their tag and their channels — nothing mixes."
    if counted["high"]:
        return (f"⛔ {counted['high']} assignment problem(s) that would pay the wrong "
                f"person, {counted['medium']} more to tidy.")
    return f"✅ Nothing that pays the wrong person · {counted['medium']} tidy-up note(s)."


def render_report(findings: list[dict] | None = None) -> str:
    """Plain text, one problem per line (CLI and flash messages)."""
    rows = scan() if findings is None else findings
    if not rows:
        return summary_line(rows)
    lines = [summary_line(rows)]
    for item in rows:
        icon = {"high": "⛔", "medium": "⚠️", "info": "ℹ️"}.get(item["severity"], "•")
        who = ", ".join(f"{creator['name']} (#{creator['id']})" for creator in item["creators"])
        lines.append(f"  {icon} {item['label']} — {who}")
        detail = " ".join(str(item.get("detail") or "").split())
        lines.append(f"      {detail[:220]}")
        lines.append(f"      fix: {item['fix']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# While adding: refuse the row that would mix two people's money.
# --------------------------------------------------------------------------
class GuardIndex:
    """An in-memory index of who owns which tag / channel right now.

    Read once (``from_db``), then reused for every row of a paste — a
    thousand-row import must not re-read the hub a thousand times. ``note``
    keeps it honest inside one batch, so the same paste cannot create two
    creators with one tag.
    """

    def __init__(self, influencers: list[dict] | None = None,
                 channels: list[dict] | None = None):
        self.tag_owners: dict[str, dict] = {}
        self.channel_owners: dict[str, list[dict]] = {}
        self.name_owners: dict[str, dict] = {}
        self.creator_names: dict[int, str] = {}
        self.shared_default = str(config.AMAZON_ASSOCIATE_TAG or "").strip()
        if influencers is None and channels is None:
            influencers, channels = db.list_influencers(), db.list_channels()
        self._load(list(influencers or []), list(channels or []))

    def _load(self, influencers: list[dict], channels: list[dict]) -> None:
        for inf in influencers:
            self.note(inf.get("id"), inf.get("amazon_tag"), [], name=inf.get("name"))
        for channel in channels:
            inf_id = int(channel.get("influencer_id") or 0)
            key = channel_key(channel.get("platform"), channel.get("identifier"))
            if inf_id and key:
                self.channel_owners.setdefault(key, []).append(
                    {"id": inf_id, "name": str(channel.get("identifier") or "")})

    @classmethod
    def from_db(cls) -> "GuardIndex":
        """The live index: who owns which tag and channel right now."""
        return cls()

    def note(self, creator_id, tag: str = "", channels: list[dict] | None = None,
             *, name: str = "") -> None:
        """Record a creator that is about to exist / has just been written."""
        inf_id = int(creator_id or 0)
        clean_tag = str(tag or "").strip()
        if inf_id and str(name or "").strip():
            self.creator_names.setdefault(inf_id, str(name).strip())
        label = self.creator_names.get(inf_id) or f"creator #{inf_id}"
        if inf_id and clean_tag and clean_tag != self.shared_default:
            self.tag_owners.setdefault(clean_tag, {"id": inf_id, "name": label})
        if inf_id and str(name or "").strip():
            self.name_owners.setdefault(str(name).strip().lower(), {"id": inf_id})
        for channel in channels or []:
            key = channel_key(channel.get("platform"), channel.get("identifier"))
            if not key or not inf_id:
                continue
            owners = self.channel_owners.setdefault(key, [])
            if not any(owner["id"] == inf_id for owner in owners):
                owners.append({"id": inf_id,
                               "name": str(channel.get("identifier") or ""),
                               "creator_name": str(name or "")})

    def preflight(self, *, name: str = "", tag: str = "", ignore_id: int = 0,
                  approval: str = "", main: str = "", whatsapp: str = "",
                  extra_channels: list[dict] | None = None) -> dict:
        """``{errors, warnings}`` for one creator before it is written.

        Errors stop the write (they would pay or post for the wrong person);
        warnings are honest notes the operator can accept deliberately.
        """
        errors: list[dict] = []
        warnings: list[dict] = []
        ignore = int(ignore_id or 0)

        clean_tag = str(tag or "").strip()
        owner = self.tag_owners.get(clean_tag) if clean_tag else None
        if owner and int(owner["id"]) != ignore:
            errors.append({"field": "amazon_tag",
                           "detail": f"Amazon tag “{clean_tag}” already belongs to "
                                     f"{owner.get('name') or 'creator #' + str(owner['id'])}. "
                                     "Each creator must post with their own Associates id.",
                           "owner_id": int(owner["id"])})
        elif not clean_tag:
            warnings.append({"field": "amazon_tag",
                             "detail": "No own tag given: links will fall back to the "
                                       "hub-wide tag, so the earnings are not theirs.",
                             "owner_id": 0})
        elif clean_tag == self.shared_default:
            warnings.append({"field": "amazon_tag",
                             "detail": f"“{clean_tag}” is the hub-wide tag: this creator's "
                                       "deals would earn under the shared id.",
                             "owner_id": 0})

        wanted: list[tuple[str, str]] = [("approval", approval), ("broadcast", main)]
        for role, identifier in wanted:
            key = channel_key("telegram", identifier)
            if not key:
                continue
            for candidate in self.channel_owners.get(key, []):
                if int(candidate["id"]) == ignore:
                    continue
                errors.append({"field": f"{role}_channel",
                               "detail": f"“{identifier}” is already the "
                                         f"{ROLE_NAMES.get(role, role)} channel of "
                                         f"{self._owner_name(int(candidate['id']))}.",
                               "owner_id": int(candidate["id"])})
                break
        if approval and main and channel_key("telegram", approval) == channel_key("telegram", main):
            errors.append({"field": "approval_channel",
                           "detail": "The approval (preview) channel and the main channel "
                                     "are the same channel — save the real approval channel, "
                                     "or leave it empty.",
                           "owner_id": ignore})

        wa_key = channel_key("whatsapp", whatsapp)
        if wa_key:
            for candidate in self.channel_owners.get(wa_key, []):
                if int(candidate["id"]) == ignore:
                    continue
                errors.append({"field": "whatsapp_channel",
                               "detail": f"The WhatsApp destination “{whatsapp}” is already "
                                         f"saved on {self._owner_name(int(candidate['id']))}.",
                               "owner_id": int(candidate["id"])})
                break
        for channel in extra_channels or []:
            key = channel_key(channel.get("platform"), channel.get("identifier"))
            if not key:
                continue
            for candidate in self.channel_owners.get(key, []):
                if int(candidate["id"]) == ignore:
                    continue
                errors.append({"field": str(channel.get("role") or "channel"),
                               "detail": f"“{channel.get('identifier')}” is already saved on "
                                         f"{self._owner_name(int(candidate['id']))}.",
                               "owner_id": int(candidate["id"])})
                break

        clean_name = str(name or "").strip().lower()
        if clean_name and clean_name in self.name_owners:
            owner_id = int(self.name_owners[clean_name]["id"])
            if owner_id != ignore:
                warnings.append({"field": "name",
                                 "detail": f"A profile called “{name}” already exists "
                                           f"(#{owner_id}) — rename one so a paste cannot "
                                           "match the wrong creator.",
                                 "owner_id": owner_id})
        return {"errors": errors, "warnings": warnings,
                "ok": not errors, "clear": not errors and not warnings}

    def _owner_name(self, creator_id: int) -> str:
        name = self.creator_names.get(int(creator_id))
        return f"{name} (#{int(creator_id)})" if name else f"creator #{int(creator_id)}"

    def explain(self, result: dict) -> str:
        """One readable line for a refused row."""
        parts = [str(item["detail"]) for item in result.get("errors", [])]
        return " · ".join(parts)
