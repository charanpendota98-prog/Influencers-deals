#!/usr/bin/env python3
"""Install the private-Telegram-source fallback from the verified patch.

The patch never checks or joins invite links. It repairs named invite labels
that do not match Telegram dialog titles, records each source's real joined
dialog title, adds live source diagnostics to Setup, the CLI and the deal-flow
board, and documents the explicit broad fallback to already-joined dialogs.

Usage (repo root):
    python3 patches/apply-private-source-fallback.py --check
    python3 patches/apply-private-source-fallback.py --dry-run
    python3 patches/apply-private-source-fallback.py --with-tests

Exit codes: 0 applied / dry-run / check / test-refresh success; 1 plain repeat
apply (no edits); 2 mismatch (nothing is applied when validation fails).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_DIR = Path(__file__).resolve().parent
PATCH_FILE = PATCH_DIR / "private-source-fallback.patch"
TESTS_DIR = PATCH_DIR / "tests"


def patch_in_place() -> bool:
    """Return True only when the complete source-selection fix is installed."""
    targets = {
        "puller": REPO_ROOT / "influencer_hub" / "puller.py",
        "dashboard": REPO_ROOT / "dashboard" / "app.py",
        "setup": REPO_ROOT / "dashboard" / "templates" / "setup.html",
        "easy_setup": REPO_ROOT / "dashboard" / "templates" / "easy_setup.html",
        "cli": REPO_ROOT / "influencer_hub" / "cli.py",
        "db": REPO_ROOT / "influencer_hub" / "db.py",
        "worker": REPO_ROOT / "influencer_hub" / "worker.py",
    }
    if any(not path.is_file() for path in targets.values()):
        return False
    text = {name: path.read_text(encoding="utf-8") for name, path in targets.items()}
    return all((marker in text[name]) for name, marker in (
        ("puller", "def _source_selection("),
        ("puller", '"private_invite_label_unmatched"'),
        ("puller", "def _dialog_summary("),
        ("dashboard", "def _live_source_selection("),
        ("dashboard", "private invite label(s)"),
        ("setup", "Friendly label (optional)"),
        ("easy_setup", "Live Telegram check"),
        ("cli", '"--telegram-sources"'),
        ("db", "source_name       TEXT NOT NULL"),
        ("worker", "source_name=source_name"),
    ))


def bundle_is_current() -> bool:
    """True when the copied ``patches/`` folder is the current bundle.

    A stale ``patches/tests/`` would install older tests over newer sources, so
    ``--with-tests`` refuses to run until the operator copies the whole
    ``patches/`` folder from the branch (the newest copy is checked for).
    """
    marker = TESTS_DIR / "test_patch_bundle.py"
    if not marker.is_file():
        return False
    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return False
    return "link-conversion-fixes.patch" in text


def copy_test_updates() -> list[str]:
    """Refresh only the verified tests/conftest files carried by this bundle."""
    target_dir = REPO_ROOT / "tests"
    if not target_dir.is_dir():
        return ["skip  tests/ not present on this checkout"]
    if not TESTS_DIR.is_dir():
        raise FileNotFoundError("patches/tests/ is missing; re-download the patch bundle")

    updated = []
    for source in sorted(TESTS_DIR.glob("test_*.py")):
        target = target_dir / source.name
        shutil.copyfile(source, target)
        updated.append(f"update tests/{source.name}")
    conftest = TESTS_DIR / "conftest.py"
    if conftest.is_file():
        shutil.copyfile(conftest, target_dir / "conftest.py")
        updated.append("update tests/conftest.py (temporary DB isolation)")
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report whether the full fix is installed")
    parser.add_argument("--dry-run", action="store_true", help="verify the patch applies, write nothing")
    parser.add_argument("--with-tests", action="store_true", help="refresh verified tests and DB-isolating conftest")
    args = parser.parse_args(argv)

    if patch_in_place():
        if args.with_tests:
            if not bundle_is_current():
                print(
                    "Refusing to refresh tests: patches/tests/ is not the current "
                    "bundle. Copy the whole patches/ folder from the branch first.",
                    file=sys.stderr,
                )
                return 2
            try:
                for item in copy_test_updates():
                    print("  ", item)
            except OSError as exc:
                print(f"Test refresh failed: {exc}", file=sys.stderr)
                return 2
        print("Private-source fallback already installed; no code changes made.")
        return 0 if (args.check or args.dry_run or args.with_tests) else 1

    if not PATCH_FILE.is_file():
        print(f"Patch file is missing: {PATCH_FILE}", file=sys.stderr)
        return 2

    patch_arg = str(PATCH_FILE.relative_to(REPO_ROOT))
    try:
        check = subprocess.run(
            ["git", "apply", "--check", patch_arg],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        print(f"Could not validate patch: {exc}", file=sys.stderr)
        return 2
    if check.returncode:
        detail = check.stderr.strip() or check.stdout.strip() or "source files differ from the verified revision"
        print(f"Aborted; nothing was written. {detail}", file=sys.stderr)
        return 2
    if args.check:
        print("Fix is not installed; verified patch can be applied. (No files changed.)")
        return 0
    if args.dry_run:
        print("Dry run passed; verified patch can be applied. (No files changed.)")
        return 0

    applied = subprocess.run(
        ["git", "apply", patch_arg],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if applied.returncode:
        detail = applied.stderr.strip() or applied.stdout.strip() or "git apply failed"
        print(f"Patch application failed: {detail}", file=sys.stderr)
        return 2
    if not patch_in_place():
        print("Patch applied but its markers are incomplete; inspect git diff before restarting.", file=sys.stderr)
        return 2

    print("Private-source fallback, Setup diagnostics, CLI source doctor, and docs applied.")
    if args.with_tests:
        if not bundle_is_current():
            print(
                "Code is applied, but tests were not refreshed: patches/tests/ is "
                "not the current bundle. Copy the whole patches/ folder from the "
                "branch and re-run with --with-tests.",
                file=sys.stderr,
            )
            return 2
        try:
            for item in copy_test_updates():
                print("  ", item)
        except OSError as exc:
            print(f"Code is applied, but tests could not be refreshed: {exc}", file=sys.stderr)
            return 2
    print(
        "Next: run `python3 -m influencer_hub.cli doctor --telegram-sources`, "
        "check Setup Center → Run live checks, then restart the worker/dashboard "
        "during a safe maintenance window. The fallback reads already-joined eligible dialogs only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
