#!/usr/bin/env python3
"""Account model: create `influencer_hub/accounts.py` and wire it in.

    Amazon        -> the CREATOR's own Associate tag (their id signs the post)
    EarnKaro      -> OUR central Affiliaters/EarnKaro account (vault credentials)
    Meesho / HYPD -> OUR central HYPD store id

What changes
------------
* `influencer_hub/accounts.py` is created exactly as the tested revision.
* `config.py`: `CENTRAL_NETWORK_ACCOUNTS` (default on).
* `pipeline.py` / `money_radar.py`: the tag and the HYPD store come from
  `accounts`, so posts and the money audit can never disagree.
* `dashboard/app.py` + `_routing_preview.html`: previews show the model, and a
  typed or stored `hypd_store_id` can no longer move HYPD commission away from
  our store while central accounts are on.

Usage (from the repo root)
--------------------------
    python3 patches/apply-account-model.py
    python3 patches/apply-account-model.py --check
    python3 patches/apply-account-model.py --with-tests

Exit codes: 0 applied (or `--check` says it is in place), 1 already applied,
2 the file is not the verified revision (nothing is written).

Safe to combine with `apply-password-policy.py` in either order: the two touch
different functions of `dashboard/app.py`.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_DIR = Path(__file__).resolve().parent
TESTS_DIR = PATCH_DIR / "tests"
ACCOUNTS_PATH = REPO_ROOT / "influencer_hub" / "accounts.py"


class PatchError(RuntimeError):
    """Raised when a file does not look like the revision this patch expects."""


#: Byte-identical to `influencer_hub/accounts.py` as tested on the branch.
#: `tests/test_patch_bundle.py` fails if this ever drifts from the real module.
ACCOUNTS_MODULE = """\"\"\"Who earns on which network — the account model in one place.

The operator's rule:

    Amazon        -> the CREATOR's own Associate tag. Their id signs the post.
    EarnKaro      -> OUR central Affiliaters/EarnKaro account (vault credentials).
    Meesho / HYPD -> OUR central HYPD store id.
    LehLah        -> the source's own attribution, preserved as-is.

`central_network_accounts` (a global setting, default from
``config.CENTRAL_NETWORK_ACCOUNTS``) makes the central half non-negotiable: a
`hypd_store_id` stored on a creator or a channel cannot redirect HYPD commission
away from our store, and EarnKaro always uses the vault credentials.

Amazon is deliberately **never** centralised. ``creator_amazon_tag`` prefers the
channel override and then the creator's own tag; the configured default is only
a last-resort fallback for a profile that still needs a tag, and
``amazon_tag_source`` reports when that happened so the dashboard can say so.
\"\"\"
from __future__ import annotations

from . import config

CENTRAL = "central"
CREATOR = "creator"
SOURCE = "source"

#: Which account earns on each network. This is the whole model, in one dict.
NETWORK_MODEL: dict[str, str] = {
    "amazon": CREATOR,
    "earnkaro": CENTRAL,
    "hypd": CENTRAL,
    "lehlah": SOURCE,
}

CENTRAL_ACCOUNTS_SETTING = "central_network_accounts"


def _global_setting(key: str, default=None):
    \"\"\"Read a global setting without ever exploding on a fresh database.\"\"\"
    try:
        from . import db

        return db.get_global_setting(key, default)
    except Exception:
        return default


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def central_network_accounts_enabled() -> bool:
    \"\"\"True when EarnKaro/HYPD must use OUR accounts, whatever a row says.\"\"\"
    setting = _global_setting(
        CENTRAL_ACCOUNTS_SETTING, bool(config.CENTRAL_NETWORK_ACCOUNTS)
    )
    return _truthy(setting)


def central_hypd_store_id() -> str:
    \"\"\"OUR HYPD store id: vault/global setting first, then the environment.\"\"\"
    configured = _global_setting("hypd_store_id", "") or config.HYPD_STORE_ID
    return str(configured or config.HYPD_STORE_ID).strip() or config.HYPD_STORE_ID


def central_earnkaro_publisher_id() -> str:
    \"\"\"OUR EarnKaro/Affiliaters publisher id.\"\"\"
    configured = (
        _global_setting("earnkaro_publisher_id", "")
        or config.EARNKARO_PUBLISHER_ID
        or ""
    )
    return str(configured).strip()


def central_earnkaro_api_key() -> str:
    \"\"\"OUR EarnKaro/Affiliaters API token (never logged, never cached).\"\"\"
    configured = _global_setting("earnkaro_api_key", "") or config.EARNKARO_API_KEY
    return str(configured).strip()


def creator_amazon_tag(
    influencer: dict | None = None, channel: dict | None = None
) -> str:
    \"\"\"The tag an Amazon link is posted with: the creator's own id.

    Priority: the channel's explicit override (still an operator-entered id for
    that creator), then the creator's own tag, then the configured fallback so a
    deal is never dropped for a missing profile value.
    \"\"\"
    influencer = influencer or {}
    channel = channel or {}
    for candidate in (
        channel.get("amazon_override_tag"),
        influencer.get("amazon_tag"),
        config.AMAZON_ASSOCIATE_TAG,
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def amazon_tag_source(
    influencer: dict | None = None, channel: dict | None = None
) -> str:
    \"\"\"Where :func:`creator_amazon_tag` got its value: channel/creator/fallback.\"\"\"
    influencer = influencer or {}
    channel = channel or {}
    if str(channel.get("amazon_override_tag") or "").strip():
        return "channel"
    if str(influencer.get("amazon_tag") or "").strip():
        return "creator"
    return "fallback"


def amazon_tag_is_creators_own(
    influencer: dict | None = None, channel: dict | None = None
) -> bool:
    \"\"\"True when the post is signed with the creator's own id, not our fallback.\"\"\"
    return amazon_tag_source(influencer, channel) in {"channel", "creator"}


def hypd_store_for(
    influencer: dict | None = None, channel: dict | None = None
) -> str:
    \"\"\"The HYPD store id that will appear in posts.

    With central accounts on (the default) this is always OUR store, so a stale
    or copied per-creator value cannot take HYPD commission elsewhere. Turning
    the setting off restores the legacy channel -> creator -> central priority.
    \"\"\"
    if central_network_accounts_enabled():
        return central_hypd_store_id()
    influencer = influencer or {}
    channel = channel or {}
    for candidate in (
        channel.get("hypd_store_id"),
        influencer.get("hypd_store_id"),
        central_hypd_store_id(),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return central_hypd_store_id()


def routing_for(
    influencer: dict | None = None, channel: dict | None = None
) -> tuple[str, str]:
    \"\"\"The (amazon_tag, hypd_store) pair the pipeline will actually use.\"\"\"
    return (
        creator_amazon_tag(influencer, channel),
        hypd_store_for(influencer, channel),
    )


def model_rows(
    amazon_tag: str = "", hypd_store: str = "", earnkaro_pubid: str = ""
) -> list[dict]:
    \"\"\"Human-readable rows of the model, for the dashboard and the preview.\"\"\"
    tag = str(amazon_tag or "").strip() or str(config.AMAZON_ASSOCIATE_TAG or "").strip()
    store = str(hypd_store or "").strip() or central_hypd_store_id()
    pubid = str(earnkaro_pubid or "").strip() or central_earnkaro_publisher_id()
    central = central_network_accounts_enabled()
    return [
        {
            "network": "Amazon",
            "earns": "creator",
            "account": tag or "the creator's own tag",
            "detail": (
                f"Every Amazon link is posted with this creator's own Associate "
                f"tag ({tag})." if tag else
                "This creator still needs their own Associate tag."
            ),
        },
        {
            "network": "EarnKaro",
            "earns": "central",
            "account": pubid or "our publisher id (not configured)",
            "detail": (
                "Flipkart / Myntra / Ajio / Nykaa / Croma / Shopsy links are "
                f"converted on OUR EarnKaro account (publisher {pubid or '—'})."
            ),
        },
        {
            "network": "Meesho (HYPD)",
            "earns": "central",
            "account": store,
            "detail": (
                f"Meesho / HYPD links carry OUR store {store}"
                + ("." if central else " (central accounts are off: per-creator stores apply).")
            ),
        },
    ]


def creators_missing_own_tag() -> list[dict]:
    \"\"\"Creators whose Amazon posts are not signed with an id of their own.

    A profile counts as missing when it has no tag, or when its tag is still the
    configured fallback (`config.AMAZON_ASSOCIATE_TAG`) — which is what a profile
    created without a tag ends up storing. Operator checklist material: for these
    creators the Amazon post earns under the fallback tag, not theirs.
    \"\"\"
    try:
        from . import db

        profiles = db.list_influencers()
    except Exception:
        return []
    fallback = str(config.AMAZON_ASSOCIATE_TAG or "").strip().casefold()
    missing: list[dict] = []
    for profile in profiles:
        tag = str(profile.get("amazon_tag") or "").strip()
        if not tag or (fallback and tag.casefold() == fallback):
            missing.append(
                {"id": profile.get("id"), "name": profile.get("name") or ""}
            )
    return missing
"""


EDITS: list[tuple[str, str, str, str]] = [
    # ---------------- influencer_hub/config.py ----------------
    (
        "influencer_hub/config.py",
        'HYPD_STORE_ID = _env("HYPD_STORE_ID", "93944")',
        'HYPD_STORE_ID = _env("HYPD_STORE_ID", "93944")\n'
        '# Account model: Amazon uses each creator\'s own Associate tag, while\n'
        '# EarnKaro and Meesho/HYPD use OUR central accounts. On (the default) a\n'
        '# per-creator or per-channel hypd_store_id cannot move HYPD commission\n'
        '# away from our store. See influencer_hub/accounts.py.\n'
        'CENTRAL_NETWORK_ACCOUNTS = _bool("CENTRAL_NETWORK_ACCOUNTS", True)',
        "config.py: CENTRAL_NETWORK_ACCOUNTS switch",
    ),
    # ---------------- influencer_hub/pipeline.py ----------------
    (
        "influencer_hub/pipeline.py",
        "from . import (\n    amazon_shortlinks,",
        "from . import (\n    accounts,\n    amazon_shortlinks,",
        "pipeline.py: import the account model",
    ),
    (
        "influencer_hub/pipeline.py",
        '    for inf in influencers:\n'
        '        amazon_tag = inf.get("amazon_tag") or config.AMAZON_ASSOCIATE_TAG\n'
        '        channels = ',
        '    for inf in influencers:\n'
        '        channels = ',
        "pipeline.py: drop the duplicated tag lookup",
    ),
    (
        "influencer_hub/pipeline.py",
        '            effective_amz_tag = (\n'
        '                ch.get("amazon_override_tag") or amazon_tag or config.AMAZON_ASSOCIATE_TAG\n'
        '            )',
        '            # Account model (influencer_hub/accounts.py): Amazon posts are\n'
        '            # signed with THIS creator\'s own Associate tag.\n'
        '            effective_amz_tag = accounts.creator_amazon_tag(inf, ch)',
        "pipeline.py: Amazon tag from the creator",
    ),
    (
        "influencer_hub/pipeline.py",
        '            # Resolve HYPD Store ID priority: channel -> profile -> central setting -> configured default.\n'
        '            global_hypd_store = db.get_global_setting("hypd_store_id", config.HYPD_STORE_ID)\n'
        '            effective_hypd_store = (\n'
        '                ch.get("hypd_store_id") or inf.get("hypd_store_id") or global_hypd_store or config.HYPD_STORE_ID\n'
        '            ).strip()',
        '            # EarnKaro and Meesho/HYPD are OUR accounts: with central accounts\n'
        '            # on (default) the HYPD store is always ours, whatever the row says.\n'
        '            effective_hypd_store = accounts.hypd_store_for(inf, ch)',
        "pipeline.py: our central HYPD store",
    ),
    # ---------------- influencer_hub/money_radar.py ----------------
    (
        "influencer_hub/money_radar.py",
        '    tag = (\n'
        '        str(channel.get("amazon_override_tag") or "").strip()\n'
        '        or str(influencer.get("amazon_tag") or "").strip()\n'
        '        or config.AMAZON_ASSOCIATE_TAG\n'
        '    )\n'
        '    store = (\n'
        '        str(channel.get("hypd_store_id") or "").strip()\n'
        '        or str(influencer.get("hypd_store_id") or "").strip()\n'
        '        or config.HYPD_STORE_ID\n'
        '    )\n'
        '    return tag, store',
        '    from . import accounts\n'
        '\n'
        '    # Audit with exactly the accounts the pipeline posts with: the creator\'s own\n'
        '    # Amazon tag, and OUR central HYPD store (see influencer_hub/accounts.py).\n'
        '    return accounts.routing_for(influencer, channel)',
        "money_radar.py: audit with the posted accounts",
    ),
    # ---------------- dashboard/app.py ----------------
    (
        "dashboard/app.py",
        'def _effective_hypd_store_id() -> str:\n'
        '    """Return the live central HYPD Store ID used for new profiles/channels."""\n'
        '    configured = db.get_global_setting("hypd_store_id", config.HYPD_STORE_ID)\n'
        '    return str(configured or config.HYPD_STORE_ID).strip() or config.HYPD_STORE_ID',
        'def _effective_hypd_store_id() -> str:\n'
        '    """OUR central HYPD Store ID (vault/global setting, then environment).\n'
        '\n'
        '    Resolved through influencer_hub.accounts so the dashboard and the pipeline\n'
        '    can never disagree about which store earns.\n'
        '    """\n'
        '    try:\n'
        '        from influencer_hub import accounts\n'
        '\n'
        '        return accounts.central_hypd_store_id()\n'
        '    except Exception:  # pragma: no cover - defensive\n'
        '        configured = db.get_global_setting("hypd_store_id", config.HYPD_STORE_ID)\n'
        '        return str(configured or config.HYPD_STORE_ID).strip() or config.HYPD_STORE_ID\n'
        '\n'
        '\n'
        'def _routing_hypd_store(requested: str = "") -> str:\n'
        '    """The HYPD store a post will really carry.\n'
        '\n'
        '    With central accounts on (the default) that is always OUR store: a value\n'
        '    typed for one creator cannot move HYPD commission elsewhere. With the\n'
        '    setting off the requested/profile value is used, so the legacy per-creator\n'
        '    behaviour stays reachable.\n'
        '    """\n'
        '    try:\n'
        '        from influencer_hub import accounts, config as hub_config\n'
        '\n'
        '        if accounts.central_network_accounts_enabled():\n'
        '            return accounts.central_hypd_store_id()\n'
        '        requested = str(requested or "").strip()\n'
        '        if requested:\n'
        '            return requested\n'
        '        setting = db.get_global_setting("hypd_store_id", hub_config.HYPD_STORE_ID)\n'
        '        return str(setting or hub_config.HYPD_STORE_ID).strip() or hub_config.HYPD_STORE_ID\n'
        '    except Exception:  # pragma: no cover - defensive\n'
        '        return str(requested or "").strip() or _effective_hypd_store_id()',
        "app.py: central store + routing resolver",
    ),
    (
        "dashboard/app.py",
        '        "hypd_store_id": request.args.get("hypd_store_id", "").strip() or _effective_hypd_store_id(),\n'
        '    })\n'
        '    inf_id = request.args.get("inf_id", "").strip()\n'
        '    if inf_id.isdigit():\n'
        '        profile = db.get_influencer(int(inf_id))\n'
        '        if profile:\n'
        '            values["amazon_tag"] = str(profile.get("amazon_tag") or values["amazon_tag"])\n'
        '            values["hypd_store_id"] = (\n'
        '                str(profile.get("hypd_store_id") or "").strip() or values["hypd_store_id"]\n'
        '            )',
        '        "hypd_store_id": _routing_hypd_store(request.args.get("hypd_store_id", "")),\n'
        '    })\n'
        '    inf_id = request.args.get("inf_id", "").strip()\n'
        '    if inf_id.isdigit():\n'
        '        profile = db.get_influencer(int(inf_id))\n'
        '        if profile:\n'
        '            values["amazon_tag"] = str(profile.get("amazon_tag") or values["amazon_tag"])\n'
        '            # Our store stays ours: a creator\'s stored store id is not used for\n'
        '            # routing while central accounts are on (the default).\n'
        '            values["hypd_store_id"] = _routing_hypd_store(profile.get("hypd_store_id"))',
        "app.py: routing preview ignores a foreign store",
    ),
    (
        "dashboard/app.py",
        '    hypd_store = request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()',
        '    hypd_store = _routing_hypd_store(request.form.get("hypd_store_id", ""))',
        "app.py: test-render preview uses our store",
    ),
    (
        "dashboard/app.py",
        '    store = str(hypd_store_id or "").strip() or _effective_hypd_store_id()\n'
        '    ek_ready = _earnkaro_ready()',
        '    store = str(hypd_store_id or "").strip() or _effective_hypd_store_id()\n'
        '    ek_ready = _earnkaro_ready()\n'
        '    try:\n'
        '        from influencer_hub import accounts\n'
        '\n'
        '        model = accounts.model_rows(\n'
        '            amazon_tag=tag, hypd_store=store,\n'
        '            earnkaro_pubid=_effective_earnkaro_publisher_id(),\n'
        '        )\n'
        '    except Exception:  # pragma: no cover - defensive\n'
        '        model = []',
        "app.py: preview carries the account model",
    ),
    (
        "dashboard/app.py",
        '                row["note"] = f"Posted with this creator\'s tag ({tag}). Never shortened away from Amazon."',
        '                row["note"] = (\n'
        '                    f"Posted with this creator\'s OWN tag ({tag}) — Amazon commission is theirs. "\n'
        '                    "Never shortened away from Amazon."\n'
        '                )',
        "app.py: Amazon row copy",
    ),
    (
        "dashboard/app.py",
        '                row["note"] = "Converted to this creator\'s EarnKaro link at send time."',
        '                row["note"] = (\n'
        '                    "Converted at send time on OUR EarnKaro account — this link earns for us."\n'
        '                )',
        "app.py: EarnKaro row copy",
    ),
    (
        "dashboard/app.py",
        '                row["note"] = f"Retagged to this creator\'s HYPD store {store}."',
        '                row["note"] = f"Retagged to OUR HYPD store {store} (our account, not the creator\'s)."',
        "app.py: HYPD row copy",
    ),
    (
        "dashboard/app.py",
        '    summary = " · ".join(\n'
        '        part for part in (\n'
        '            f"Amazon → tag {tag}" if amazon_on else "Amazon off",\n'
        '            "Other merchants → EarnKaro" if ek_on else "Other merchants off",\n'
        '            f"Meesho → HYPD store {store}" if hypd_on else "Meesho off",\n'
        '        )\n'
        '    )\n'
        '    return {\n'
        '        "rows": rows,\n'
        '        "summary": summary,',
        '    summary = " · ".join(\n'
        '        part for part in (\n'
        '            f"Amazon → creator\'s tag {tag}" if amazon_on else "Amazon off",\n'
        '            "Other merchants → our EarnKaro" if ek_on else "Other merchants off",\n'
        '            f"Meesho → our HYPD store {store}" if hypd_on else "Meesho off",\n'
        '        )\n'
        '    )\n'
        '    return {\n'
        '        "rows": rows,\n'
        '        "model": model,\n'
        '        "summary": summary,',
        "app.py: preview summary says who earns",
    ),
    # ---------------- dashboard/templates/_routing_preview.html ----------------
    (
        "dashboard/templates/_routing_preview.html",
        '<p class="routing-summary"><strong>{{ preview.summary }}</strong></p>',
        '<p class="routing-summary"><strong>{{ preview.summary }}</strong></p>\n'
        '{% if preview.model %}\n'
        '  <table class="routing-table">\n'
        '    <thead>\n'
        '      <tr>\n'
        '        <th>Network</th>\n'
        '        <th>Whose account earns</th>\n'
        '      </tr>\n'
        '    </thead>\n'
        '    <tbody>\n'
        '      {% for entry in preview.model %}\n'
        '        <tr class="routing-row">\n'
        '          <td><strong>{{ entry.network }}</strong><code>{{ entry.account }}</code></td>\n'
        '          <td><span class="routing-note">{{ entry.detail }}</span></td>\n'
        '        </tr>\n'
        '      {% endfor %}\n'
        '    </tbody>\n'
        '  </table>\n'
        '{% endif %}',
        "_routing_preview.html: who-earns table",
    ),
]


def _stage(path: Path, pending: dict[Path, str]) -> str:
    text = pending.get(path)
    if text is None:
        if not path.is_file():
            raise PatchError(f"missing file {path.relative_to(REPO_ROOT)}")
        text = path.read_text(encoding="utf-8")
        pending[path] = text
    return text


def apply_edits(pending: dict[Path, str], stats: list[str]) -> None:
    for relative, old, new, label in EDITS:
        path = REPO_ROOT / relative
        text = _stage(path, pending)
        count = text.count(old)
        if count != 1:
            raise PatchError(
                f"{label}: expected 1 match in {relative}, found {count}. "
                "This file is not the revision the patch was verified against."
            )
        pending[path] = text.replace(old, new, 1)
        stats.append(f"edit  {label}")


def policy_in_place() -> bool:
    """True when the account model is already installed."""
    if not ACCOUNTS_PATH.is_file():
        return False
    pipeline_text = (REPO_ROOT / "influencer_hub" / "pipeline.py").read_text(
        encoding="utf-8"
    )
    return "accounts.hypd_store_for(inf, ch)" in pipeline_text


def copy_test_updates(stats: list[str]) -> None:
    tests_dir = REPO_ROOT / "tests"
    if not tests_dir.is_dir():
        stats.append("skip  tests/ not present on this checkout")
        return
    if not TESTS_DIR.is_dir():
        raise PatchError("patches/tests/ is missing; re-download the patch bundle")
    for source in sorted(TESTS_DIR.glob("test_*.py")):
        target = tests_dir / source.name
        action = "update" if target.exists() else "add"
        shutil.copyfile(source, target)
        stats.append(f"{action}  tests/{source.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the account model (their Amazon id, our EarnKaro/HYPD)."
    )
    parser.add_argument("--check", action="store_true",
                        help="report whether the model is in place; write nothing")
    parser.add_argument("--with-tests", action="store_true",
                        help="also refresh tests/ from patches/tests/ (optional)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate every edit, write nothing")
    args = parser.parse_args(argv)

    if policy_in_place():
        if args.with_tests:
            try:
                test_stats: list[str] = []
                copy_test_updates(test_stats)
            except PatchError as error:
                print(f"⚠️  Test refresh skipped: {error}", file=sys.stderr)
            else:
                for line in test_stats:
                    print(f"   {line}")
        print(
            "✅ Account model already applied — nothing to do.\n"
            "   (Amazon uses the creator's tag; EarnKaro and HYPD are ours.)\n"
            "   Double-applying is refused on purpose."
        )
        return 0 if args.check else 1

    pending: dict[Path, str] = {}
    stats: list[str] = []
    try:
        apply_edits(pending, stats)
    except PatchError as error:
        print(f"❌ Aborted, nothing was written:\n   {error}", file=sys.stderr)
        return 2

    for line in stats:
        print(f"   {line}")

    if args.dry_run:
        print("🔎 Dry run complete — all edits validated, nothing written.")
        return 0

    ACCOUNTS_PATH.write_text(ACCOUNTS_MODULE, encoding="utf-8")
    print(f"   create  influencer_hub/accounts.py ({len(ACCOUNTS_MODULE)} chars)")
    for path, text in pending.items():
        path.write_text(text, encoding="utf-8")
    print(f"✅ Account model applied ({len(pending)} files edited).")

    if args.with_tests:
        try:
            test_stats: list[str] = []
            copy_test_updates(test_stats)
        except PatchError as error:
            print(f"⚠️  Test refresh skipped: {error}", file=sys.stderr)
        else:
            for line in test_stats:
                print(f"   {line}")

    print(
        "\nNext:\n"
        "   sudo systemctl restart influencer-deal-worker influencer-dashboard\n"
        "   pytest -q tests/test_account_model.py\n"
        "\n"
        "Verify on the dashboard:\n"
        "   Easy Setup → the routing table now shows the \\\"Whose account earns\\\" rows\n"
        "   (Amazon → the creator's tag, EarnKaro/HYPD → ours).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
