#!/usr/bin/env python3
"""Password policy patch: the dashboard password is asked only at sign-in and
before a REMOVAL (delete a channel, a creator, or a deal source).

Why this patch exists
---------------------
The dashboard used to demand the password for 28 endpoints (add, save, toggle,
poll, Easy Setup, money switches...). That made everyday work painful. The new
rule:

    1. sign in once with the password, and
    2. only removals ask for the password again — once per unlock window.

`REAUTH_REQUIRED_ENDPOINTS` therefore becomes exactly three endpoints:
`delete_channel`, `delete_influencer`, `delete_deal_source`. Every other form
(add / save / toggle / poll / Easy Setup / money switches) saves immediately,
and the separate inline `prompt()` password on Delete-Influencer is removed so
all removals share the one unlock dialog.

Usage (on the VM, from the repo root)
-------------------------------------
    python3 patches/apply-password-policy.py            # apply
    python3 patches/apply-password-policy.py --check    # report only, no writes
    python3 patches/apply-password-policy.py --with-tests

Exit codes
----------
    0  policy applied (or `--check` confirmed it is already in place)
    1  policy already applied -> the script stops, it never double-edits
    2  a file did not match the expected revision -> nothing was written

Safety notes
------------
* This patch only touches `dashboard/` (app.py, static/app.js, templates/*.html).
  It never edits `influencer_hub/`, so the EarnKaro / commission fixes stay
  exactly as they are on the VM.
* All replacements are exact-text and validated before a single byte is
  written, so the tree can never end up half-patched.
* Do NOT apply this with `git checkout --` or `patch -U0`; those either revert
  the VM-only fixes or silently mis-apply.

After applying: `sudo systemctl restart influencer-dashboard`.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_DIR = Path(__file__).resolve().parent
TESTS_DIR = PATCH_DIR / "tests"

REMOVAL_ENDPOINTS = ("delete_channel", "delete_influencer", "delete_deal_source")

# Endpoints that KEEP the shared unlock dialog. Everything else loses it.
FORM_KEEP = {"delete_channel", "delete_influencer", "delete_deal_source"}

TAG_RE = re.compile(r"<(form|button)\b[^>]*\bdata-require-reauth\b[^>]*>", re.I | re.S)
FORM_OPEN_RE = re.compile(r"<form\b", re.I)
ACTION_RE = re.compile(r"url_for\(\s*'(?P<name>[a-z_]+)'", re.I)
ATTR_RE = re.compile(r"\s*\bdata-require-reauth\b", re.I)


class PatchError(RuntimeError):
    """Raised when a file does not look like the revision this patch expects."""


# ---------------------------------------------------------------------------
# 1. Literal, verified replacements
#    (relative path, exact old text, new text, what it fixes)
# ---------------------------------------------------------------------------
EDITS: list[tuple[str, str, str, str]] = [
    # ---------------- dashboard/app.py ----------------
    (
        "dashboard/app.py",
        "# One password confirmation unlocks every sensitive setup change below for a\n"
        "# rolling idle window (see config.DASHBOARD_SETUP_UNLOCK_SECONDS). The first\n"
        "# change asks for the password; the rest of the session does not ask again\n"
        "# until the dashboard has been idle (or is locked manually).",
        "# Password policy: the password is asked only at sign-in and before a REMOVAL\n"
        "# (delete a channel, a creator, or a deal source) for a rolling idle window\n"
        "# (see config.DASHBOARD_SETUP_UNLOCK_SECONDS). Adding, saving, toggling and\n"
        "# polling cost no extra confirmation; a removal asks once per window.",
        "app.py: policy comment",
    ),
    (
        "dashboard/app.py",
        'REAUTH_REQUIRED_ENDPOINTS = frozenset({\n'
        '    "seed_default_sources", "add_deal_source", "delete_deal_source",\n'
        '    "toggle_deal_source", "update_global_settings", "quick_add",\n'
        '    "update_profile", "update_channel_route", "add_manual_channel",\n'
        '    "bulk_import", "delete_channel", "undo_channel_delete", "easy_setup",\n'
        '    "toggle_influencer_active",\n'
        '    "toggle_channel_status", "onboard", "set_flags", "onboard_tg",\n'
        '    "toggle_money_switch",\n'
        '    "onboard_wa", "create_tg", "pair_wa", "create_group",\n'
        '    "wa_connect_chat", "create_newsletter", "send_test_message", "send_poll",\n'
        '})',
        'REAUTH_REQUIRED_ENDPOINTS = frozenset({\n'
        '    "delete_channel", "delete_influencer", "delete_deal_source",\n'
        '})',
        "app.py: gate only removals (28 -> 3 endpoints)",
    ),
    (
        "dashboard/app.py",
        '            flash(\n'
        '                "Setup changes are locked. Confirm the dashboard password once, "\n'
        '                "then every add / save / delete stays unlocked for "\n'
        '                f"{_setup_unlock_window_seconds() // 60} minutes.",\n'
        '                "warning",\n'
        '            )',
        '            flash(\n'
        '                "Removals are locked. Confirm the dashboard password once, "\n'
        '                "then every delete stays unlocked for "\n'
        '                f"{_setup_unlock_window_seconds() // 60} minutes. "\n'
        '                "Adding and saving never ask.",\n'
        '                "warning",\n'
        '            )',
        "app.py: locked banner copy -> removals",
    ),
    (
        "dashboard/app.py",
        'def delete_influencer(inf_id):\n'
        '    pwd = request.form.get("admin_password", "").strip()\n'
        '    if not _password_matches(pwd, config.ADMIN_DELETE_PASSWORD):\n'
        '        return redirect(url_for("influencer_detail", inf_id=inf_id, err="invalid_password"))\n'
        '    db.delete_influencer(inf_id)',
        'def delete_influencer(inf_id):\n'
        '    # The shared unlock window already confirmed the operator\'s password for\n'
        '    # this removal (see REAUTH_REQUIRED_ENDPOINTS), so there is no second,\n'
        '    # separate password prompt here.\n'
        '    db.delete_influencer(inf_id)',
        "app.py: drop the extra Delete-Influencer password check",
    ),
    (
        "dashboard/app.py",
        '    """Confirm the admin password once and unlock sensitive setup changes.\n'
        '\n'
        '    The unlock is a rolling idle window (default 30 minutes): the operator is\n'
        '    asked at the start of a work session and not again for every add / save /\n'
        '    delete, unless the dashboard goes idle, is locked manually, or signs out.\n'
        '    """',
        '    """Confirm the admin password once and unlock removals.\n'
        '\n'
        '    The unlock is a rolling idle window (default 30 minutes): the first delete\n'
        '    asks and later deletes do not, unless the dashboard goes idle, is locked\n'
        '    manually, or signs out. Adding and saving never ask at all.\n'
        '    """',
        "app.py: /reauth docstring",
    ),
    (
        "dashboard/app.py",
        '            flash("Password did not match. Setup is still locked.", "error")',
        '            flash("Password did not match. Removals are still locked.", "error")',
        "app.py: wrong-password flash",
    ),
    (
        "dashboard/app.py",
        '        flash(f"Setup unlocked for {minutes} minutes of work.", "success")',
        '        flash(f"Removals unlocked for {minutes} minutes of work.", "success")',
        "app.py: unlocked flash",
    ),
    (
        "dashboard/app.py",
        '    """Tell the page whether the setup password will be asked right now."""',
        '    """Tell the page whether a removal will ask for the password right now."""',
        "app.py: /reauth/status docstring",
    ),
    (
        "dashboard/app.py",
        '    """Lock setup changes immediately so the next change asks again."""',
        '    """Lock removals immediately so the next delete asks again."""',
        "app.py: lock docstring",
    ),
    (
        "dashboard/app.py",
        '    flash("Locked. The next setup change will ask for the password again.", "success")',
        '    flash("Locked. The next removal will ask for the password again.", "success")',
        "app.py: lock flash",
    ),
    (
        "dashboard/app.py",
        "# Setup unlock: one password confirmation, then a rolling idle window.",
        "# Removal unlock: one password confirmation, then a rolling idle window.",
        "app.py: section comment",
    ),
    (
        "dashboard/app.py",
        '    """Sliding window: each confirmed change restarts the idle timer."""',
        '    """Sliding window: each confirmed removal restarts the idle timer."""',
        "app.py: renew docstring",
    ),
    # ---------------- dashboard/static/app.js ----------------
    (
        "dashboard/static/app.js",
        "// Setup mutations need one password confirmation per unlock window: the first\n"
        "// add / save / delete asks, the rest of the session does not. Passwords travel\n"
        "// only in the same-origin request body and are never stored in the page.",
        "// Removals (delete a channel, a creator, a deal source) need one password\n"
        "// confirmation per unlock window: the first delete asks, later deletes in the\n"
        "// window do not. Adding and saving are never intercepted. Passwords travel\n"
        "// only in the same-origin request body and are never stored in the page.",
        "app.js: header comment",
    ),
    (
        "dashboard/static/app.js",
        "      lockBadge.title = 'Setup changes are unlocked. Click to lock now.';",
        "      lockBadge.title = 'Removals are unlocked. Click to lock now.';",
        "app.js: unlocked badge title",
    ),
    (
        "dashboard/static/app.js",
        "      const password = window.prompt('Enter the dashboard password to unlock setup changes');",
        "      const password = window.prompt('Enter the dashboard password to confirm this removal');",
        "app.js: no-dialog fallback prompt",
    ),
    (
        "dashboard/static/app.js",
        "      restored\n"
        "        ? 'Setup was locked, so nothing was saved. Your details were restored — confirm the password, then press the button again.'\n"
        "        : 'Setup was locked, so nothing was saved. Confirm the password once to unlock setup changes.'",
        "      restored\n"
        "        ? 'The removal was locked, so nothing was deleted. Your details were restored — confirm the password, then press the button again.'\n"
        "        : 'The removal was locked, so nothing was deleted. Confirm the password once to unlock removals.'",
        "app.js: locked-after-redirect message",
    ),
    (
        "dashboard/static/app.js",
        "        'Enter the dashboard password to unlock setup changes'",
        "        'Enter the dashboard password to confirm this removal'",
        "app.js: standalone unlock copy (if present)",
    ),
    # ---------------- dashboard/templates/influencer.html ----------------
    (
        "dashboard/templates/influencer.html",
        "    <form method=\"post\" action=\"{{ url_for('delete_influencer', inf_id=inf.id) }}\" onsubmit=\"var p = prompt('Enter Admin Password to delete this influencer:'); if (!p) return false; this.admin_password.value = p; return true;\">\n"
        "<input type=\"hidden\" name=\"_csrf_token\" value=\"{{ csrf_token() }}\" />\n"
        "      <input type=\"hidden\" name=\"admin_password\" value=\"\" />\n"
        "      <button type=\"submit\" style=\"background: #dc2626; color: white; padding: 8px 14px; border-radius: 6px; font-size: 13px; font-weight:bold; cursor:pointer;\">🔒 Delete Influencer (Password Protected)</button>",
        "    <form method=\"post\" action=\"{{ url_for('delete_influencer', inf_id=inf.id) }}\" data-require-reauth data-confirm=\"Delete this creator and every channel attached? Deal posting stops immediately.\">\n"
        "<input type=\"hidden\" name=\"_csrf_token\" value=\"{{ csrf_token() }}\" />\n"
        "      <button type=\"submit\" style=\"background: #dc2626; color: white; padding: 8px 14px; border-radius: 6px; font-size: 13px; font-weight:bold; cursor:pointer;\">🗑️ Delete Influencer (one password removal)</button>",
        "influencer.html: one shared unlock dialog for Delete Influencer",
    ),
    (
        "dashboard/templates/influencer.html",
        "      {% if setup_unlocked %}\n"
        "        🔓 <b style=\"color:#6ee7b7;\">Setup unlocked</b> — add, save and delete channels freely for another {{ setup_unlock_minutes }} minute(s); the password is not asked again until it locks.\n"
        "      {% else %}\n"
        "        🔒 <b style=\"color:#fde68a;\">One password confirmation opens this up.</b> Enter the admin password once on the next Connect / Save / Remove and every other channel change goes through without asking again.\n"
        "      {% endif %}",
        "      {% if setup_unlocked %}\n"
        "        🔓 <b style=\"color:#6ee7b7;\">Removals unlocked</b> — deleting a channel, a creator or a deal source will not ask again for another {{ setup_unlock_minutes }} minute(s). Adding and saving never ask.\n"
        "      {% else %}\n"
        "        🔒 <b style=\"color:#fde68a;\">Adding and saving need no password.</b> Only a removal (delete a channel, a creator or a deal source) asks for the admin password once; every later removal in this window goes through without asking again.\n"
        "      {% endif %}",
        "influencer.html: lock-state hint copy -> removals",
    ),
    # ---------------- dashboard/templates/layout.html ----------------
    (
        "dashboard/templates/layout.html",
        "                    title=\"Setup changes are unlocked. Every add / save / delete goes through without another password prompt until the dashboard is idle for {{ setup_unlock_window_minutes }} minutes. Click to lock now.\">",
        "                    title=\"Removals are unlocked. Deleting a channel, a creator or a deal source goes through without another password prompt until the dashboard is idle for {{ setup_unlock_window_minutes }} minutes. Click to lock now.\">",
        "layout.html: unlocked nav title",
    ),
    (
        "dashboard/templates/layout.html",
        "                  title=\"Setup changes are locked. The next add / save / delete asks for the admin password once, then stays unlocked.\">\n"
        "            🔒 Setup locked · Unlock",
        "                  title=\"Removals are locked. The next delete asks for the admin password once, then stays unlocked. Adding and saving never ask.\">\n"
        "            🔒 Removals locked · Unlock",
        "layout.html: locked nav button copy",
    ),
    (
        "dashboard/templates/layout.html",
        "            <strong>🔒 Setup changes are locked — nothing was saved.</strong>\n"
        "            <p class=\"hint\" style=\"margin:4px 0 0;\">\n"
        "              Enter the admin password once. Setup then stays unlocked for\n"
        "              {{ setup_unlock_window_minutes }} minutes, so the next add / save /\n"
        "              delete will not ask again.\n"
        "            </p>",
        "            <strong>🔒 The removal is locked — nothing was deleted.</strong>\n"
        "            <p class=\"hint\" style=\"margin:4px 0 0;\">\n"
        "              Enter the admin password once. Removals then stay unlocked for\n"
        "              {{ setup_unlock_window_minutes }} minutes, so the next delete will\n"
        "              not ask again. Adding and saving never ask.\n"
        "            </p>",
        "layout.html: no-JS unlock banner copy",
    ),
    (
        "dashboard/templates/layout.html",
        "          <button type=\"submit\" class=\"btn btn-primary\">Unlock &amp; continue</button>\n"
        "        </form>\n"
        "      </div>\n"
        "      </noscript>",
        "          <button type=\"submit\" class=\"btn btn-primary\">Unlock &amp; delete</button>\n"
        "        </form>\n"
        "      </div>\n"
        "      </noscript>",
        "layout.html: no-JS banner button label",
    ),
    (
        "dashboard/templates/layout.html",
        "        <h2 id=\"reauth-title\">Confirm setup changes once</h2>\n"
        "        <p id=\"reauth-copy\">\n"
        "          Enter the dashboard password to continue. Setup stays unlocked for\n"
        "          <b>{{ setup_unlock_window_minutes }} minutes</b> of work, so adding,\n"
        "          saving and deleting channels after this will not ask again.\n"
        "        </p>",
        "        <h2 id=\"reauth-title\">Confirm this removal once</h2>\n"
        "        <p id=\"reauth-copy\">\n"
        "          Enter the dashboard password to delete. Removals stay unlocked for\n"
        "          <b>{{ setup_unlock_window_minutes }} minutes</b> of work, so later\n"
        "          deletes after this will not ask again. Adding and saving never ask.\n"
        "        </p>",
        "layout.html: unlock dialog copy -> removals",
    ),
    (
        "dashboard/templates/layout.html",
        "          <button type=\"button\" class=\"btn btn-primary\" id=\"reauth-confirm\">Unlock &amp; continue</button>",
        "          <button type=\"button\" class=\"btn btn-primary\" id=\"reauth-confirm\">Confirm &amp; delete</button>",
        "layout.html: unlock dialog button label",
    ),
    # ---------------- dashboard/templates/easy_setup.html ----------------
    (
        "dashboard/templates/easy_setup.html",
        "    <span class=\"hint\">One password confirmation unlocks setup for {{ setup_unlock_window_minutes }} minutes.</span>",
        "    <span class=\"hint\">Easy Setup saves immediately — no password. Only removals (delete a channel, a creator or a deal source) ask for the password, once per unlock window.</span>",
        "easy_setup.html: hint copy",
    ),
    # ---------------- dashboard/templates/setup.html ----------------
    (
        "dashboard/templates/setup.html",
        "    <div class=\"dashboard-status\"><strong>{{ influencers|length }} creator profiles</strong><p>Profile and channel changes require password confirmation.</p></div>",
        "    <div class=\"dashboard-status\"><strong>{{ influencers|length }} creator profiles</strong><p>Adding and saving need no password. A removal (creator, channel or deal source) asks once.</p></div>",
        "setup.html: status copy",
    ),
    # ---------------- dashboard/templates/money.html ----------------
    (
        "dashboard/templates/money.html",
        "    <span class=\"hint\">These change what the pipeline posts — password confirmed.</span>",
        "    <span class=\"hint\">These change what the pipeline posts — saved immediately, no password.</span>",
        "money.html: switches hint copy",
    ),
]


def _apply_literal_edits(
    pending: dict[Path, str], stats: list[str], *, dry_run: bool
) -> None:
    """Validate every literal replacement, then stage the new file contents."""
    for relative, old, new, label in EDITS:
        path = REPO_ROOT / relative
        text = pending.get(path)
        if text is None:
            if not path.is_file():
                raise PatchError(f"{label}: missing file {relative}")
            text = path.read_text(encoding="utf-8")
            pending[path] = text
        occurrences = text.count(old)
        if occurrences == 0:
            # Optional edits are marked by ending their label with "(if present)".
            if label.endswith("(if present)"):
                stats.append(f"skip  {label}")
                continue
            raise PatchError(
                f"{label}: expected text not found in {relative}. "
                "This file is not the revision the patch was verified against."
            )
        if occurrences > 1:
            raise PatchError(
                f"{label}: expected exactly 1 occurrence in {relative}, found {occurrences}."
            )
        pending[path] = text.replace(old, new, 1)
        stats.append(f"edit  {label}")
        if dry_run:
            continue


def _endpoint_for_tag(text: str, match: re.Match[str]) -> str:
    """Return the Flask endpoint of the form/button carrying data-require-reauth."""
    tag = match.group(0)
    if match.group(1).lower() == "form":
        found = ACTION_RE.search(tag)
        return found.group("name") if found else ""
    # A button: resolve the form it belongs to.
    form_start = None
    for candidate in FORM_OPEN_RE.finditer(text, 0, match.start()):
        form_start = candidate.start()
    if form_start is None:
        return ""
    found = ACTION_RE.search(text, form_start, match.start())
    return found.group("name") if found else ""


def _strip_non_removal_reauth(
    pending: dict[Path, str], stats: list[str]
) -> None:
    """Drop data-require-reauth from add / save / toggle forms; keep removals."""
    templates_dir = REPO_ROOT / "dashboard" / "templates"
    removed = 0
    kept: set[str] = set()
    for path in sorted(templates_dir.glob("*.html")):
        text = pending.get(path)
        if text is None:
            text = path.read_text(encoding="utf-8")

        matches = list(TAG_RE.finditer(text))
        if not matches:
            continue
        for match in reversed(matches):
            endpoint = _endpoint_for_tag(text, match)
            if endpoint in FORM_KEEP:
                kept.add(endpoint)
                continue
            tag = match.group(0)
            cleaned = ATTR_RE.sub("", tag, count=1)
            text = text[: match.start()] + cleaned + text[match.end():]
            removed += 1
            stats.append(
                f"edit  {path.name}: data-require-reauth off "
                f"{endpoint or 'unknown'}"
            )
        pending[path] = text

    if removed != 20:
        raise PatchError(
            "expected to free the 20 add / save / toggle forms, freed "
            f"{removed}. The templates are not the revision the patch was "
            "verified against."
        )
    missing = FORM_KEEP - kept
    if missing:
        raise PatchError(
            "these removal forms must keep data-require-reauth: "
            + ", ".join(sorted(missing))
        )
    stats.append(
        "keep  data-require-reauth on: " + ", ".join(sorted(FORM_KEEP))
    )


def _policy_in_place() -> bool:
    """True when app.py already carries the removals-only policy."""
    app_py = REPO_ROOT / "dashboard" / "app.py"
    if not app_py.is_file():
        return False
    text = app_py.read_text(encoding="utf-8")
    return (
        '"delete_channel", "delete_influencer", "delete_deal_source",'
        in text
        and "REAUTH_REQUIRED_ENDPOINTS = frozenset({\n    \"delete_channel\", "
        in text
    )


def _copy_test_updates(stats: list[str]) -> None:
    """Optionally refresh tests/ with the policy's verified test versions."""
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
        description="Apply the removals-only dashboard password policy."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the policy is in place; write nothing",
    )
    parser.add_argument(
        "--with-tests",
        action="store_true",
        help="also refresh tests/ from patches/tests/ (optional)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate every edit, write nothing"
    )
    args = parser.parse_args(argv)

    if _policy_in_place():
        if args.with_tests:
            # The runtime policy is already in place; still let the operator
            # refresh the verified test files on this checkout.
            try:
                test_stats: list[str] = []
                _copy_test_updates(test_stats)
            except PatchError as error:
                print(f"⚠️  Test refresh skipped: {error}", file=sys.stderr)
            else:
                for line in test_stats:
                    print(f"   {line}")
        print(
            "✅ Password policy already applied — nothing to do.\n"
            "   (REAUTH_REQUIRED_ENDPOINTS is already the 3-endpoint removals set.)\n"
            "   Double-applying is refused on purpose: this patch never edits a\n"
            "   file twice. Restart the service if you have not already:\n"
            "   sudo systemctl restart influencer-dashboard"
        )
        return 0 if args.check else 1

    if args.check:
        print(
            "❌ Password policy NOT applied yet.\n"
            "   Run: python3 patches/apply-password-policy.py"
        )
        return 1

    pending: dict[Path, str] = {}
    stats: list[str] = []
    try:
        _apply_literal_edits(pending, stats, dry_run=args.dry_run)
        _strip_non_removal_reauth(pending, stats)
    except PatchError as error:
        print(f"❌ Aborted, nothing was written:\n   {error}", file=sys.stderr)
        return 2

    for line in stats:
        print(f"   {line}")

    if args.dry_run:
        print("🔎 Dry run complete — all edits validated, nothing written.")
        return 0

    for path, text in pending.items():
        path.write_text(text, encoding="utf-8")
    print(f"✅ Password policy applied to {len(pending)} files.")

    if args.with_tests:
        try:
            test_stats: list[str] = []
            _copy_test_updates(test_stats)
        except PatchError as error:
            print(f"⚠️  Test refresh skipped: {error}", file=sys.stderr)
        else:
            for line in test_stats:
                print(f"   {line}")

    print(
        "\nNext:\n"
        "   sudo systemctl restart influencer-dashboard\n"
        "   systemctl is-active influencer-dashboard\n"
        "\n"
        "Verify (should list exactly the 3 removal endpoints):\n"
        "   grep -n 'REAUTH_REQUIRED_ENDPOINTS' -A 3 dashboard/app.py\n"
        "   grep -rn 'data-require-reauth' dashboard/templates/ | wc -l   # -> 4\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
