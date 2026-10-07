#!/usr/bin/env python3
"""Install the link-conversion fixes from the verified patch.

This bundle is the second half of the current work and must be applied *after*
``apply-private-source-fallback.py`` (its patch is diffed against that state).

Both follow-up patches carry source files and the root docs only: ``tests/`` is
installed from the byte-identical copies in ``patches/tests/`` by
``--with-tests``, and the whole ``patches/`` folder is meant to be copied from
the branch, so no patch rewrites it and a wholesale copy can never conflict.

It makes every link shape a source deal can carry end up as OUR affiliate link:

* opaque Amazon shorts are recognised on every host Amazon uses
  (``amzn.to``/``amzn.in``/``amzn.eu``/``amzn.asia``/``a.co``), resolved over the
  network into ``/dp/ASIN?tag=<OURS>``, and reported when they stay unresolved;
* a deal whose only link is a merchant URL EarnKaro did not convert is parked in
  the new ``deferred_deals`` queue and retried by the worker instead of being
  posted for free, then posted anyway when the retry budget is spent
  (``ALLOW_UNCONVERTED_POSTS=1`` restores the old immediate behaviour);
* the approval renderer no longer prints the same Amazon link twice;
* the commission guard and Money Radar see opaque shorts and can answer "does
  this post contain at least one link that pays us?".

Usage (repo root):
    python3 patches/apply-link-conversion-fixes.py --check
    python3 patches/apply-link-conversion-fixes.py --dry-run
    python3 patches/apply-link-conversion-fixes.py --with-tests

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
PATCH_FILE = PATCH_DIR / "link-conversion-fixes.patch"
TESTS_DIR = PATCH_DIR / "tests"

# The patch is diffed against the state this file produces; a checkout that has
# not installed it yet cannot apply the second patch.
PREREQUISITE = PATCH_DIR / "apply-private-source-fallback.py"


def patch_in_place() -> bool:
    """Return True only when every conversion fix is installed."""
    targets = {
        "link_router": REPO_ROOT / "influencer_hub" / "link_router.py",
        "commission_guard": REPO_ROOT / "influencer_hub" / "commission_guard.py",
        "pipeline": REPO_ROOT / "influencer_hub" / "pipeline.py",
        "worker": REPO_ROOT / "influencer_hub" / "worker.py",
        "db": REPO_ROOT / "influencer_hub" / "db.py",
        "earnkaro": REPO_ROOT / "influencer_hub" / "earnkaro.py",
        "config": REPO_ROOT / "influencer_hub" / "config.py",
        "dashboard": REPO_ROOT / "dashboard" / "app.py",
    }
    if any(not path.is_file() for path in targets.values()):
        return False
    text = {name: path.read_text(encoding="utf-8") for name, path in targets.items()}
    return all((marker in text[name]) for name, marker in (
        ("link_router", "AMAZON_SHORT_HOSTS"),
        ("link_router", "def text_has_amazon_short("),
        ("link_router", "def unresolved_amazon_shorts("),
        ("commission_guard", "def our_affiliate_urls("),
        ("commission_guard", "def unmonetised_links("),
        ("pipeline", "def allow_unconverted_posts_enabled("),
        ("pipeline", "DEFERRED_MAX_ATTEMPTS"),
        ("pipeline", "db.defer_deal("),
        ("worker", "def drain_deferred_deals("),
        ("db", "CREATE TABLE IF NOT EXISTS deferred_deals"),
        ("db", "def due_deferred_deals("),
        ("earnkaro", "def credentials_configured("),
        ("config", "ALLOW_UNCONVERTED_POSTS"),
        ("dashboard", '"deferred"'),
    ))


def prerequisite_installed() -> bool:
    """True when the first patch bundle is already in the checkout."""
    puller = REPO_ROOT / "influencer_hub" / "puller.py"
    if not puller.is_file():
        return False
    try:
        return "def _source_selection(" in puller.read_text(encoding="utf-8")
    except OSError:
        return False


def bundle_is_current() -> bool:
    """True when the copied ``patches/`` folder is the current bundle.

    A stale ``patches/tests/`` would install older tests over newer sources, so
    the applier checks for the newest copy it ships with before touching tests.
    """
    marker = TESTS_DIR / "test_patch_bundle.py"
    if not marker.is_file():
        return False
    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return False
    return "link-conversion-fixes.patch" in text and (
        TESTS_DIR / "test_perfect_link_conversion.py"
    ).is_file()


def sync_handoff_copy() -> str:
    """Keep the bundle's handoff byte-identical to the root one (guarded test).

    An operator who updates only the patch/applier files (instead of the whole
    ``patches/`` folder) would otherwise leave ``patches/HANDOFF.md`` behind,
    which the bundle guard test reports as drift. The two files are required to
    be identical, so syncing them is always safe and writes nothing new.
    """
    root = REPO_ROOT / "HANDOFF.md"
    copy = PATCH_DIR / "HANDOFF.md"
    if not root.is_file():
        return "skip  HANDOFF.md not present on this checkout"
    try:
        text = root.read_text(encoding="utf-8")
        if copy.is_file() and copy.read_text(encoding="utf-8") == text:
            return "patches/HANDOFF.md already matches HANDOFF.md"
        copy.write_text(text, encoding="utf-8")
    except OSError as exc:
        return f"could not refresh patches/HANDOFF.md: {exc}"
    return "refresh patches/HANDOFF.md to match HANDOFF.md"


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
    parser.add_argument("--check", action="store_true", help="report whether the fixes are installed")
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
            print("  ", sync_handoff_copy())
            try:
                for item in copy_test_updates():
                    print("  ", item)
            except OSError as exc:
                print(f"Test refresh failed: {exc}", file=sys.stderr)
                return 2
        print("Link-conversion fixes already installed; no code changes made.")
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
        if not prerequisite_installed():
            print(
                f"Hint: this bundle is diffed against the private-source-fallback "
                f"release. Run `python3 {PREREQUISITE.relative_to(REPO_ROOT)} --check` first.",
                file=sys.stderr,
            )
        return 2
    if args.check:
        print("Conversion fixes are not installed; verified patch can be applied. (No files changed.)")
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

    print("Link-conversion fixes, retry queue, flow board and docs applied.")
    if args.with_tests:
        if not bundle_is_current():
            print(
                "Code is applied, but tests were not refreshed: patches/tests/ is "
                "not the current bundle. Copy the whole patches/ folder from the "
                "branch and re-run with --with-tests.",
                file=sys.stderr,
            )
            return 2
        print("  ", sync_handoff_copy())
        try:
            for item in copy_test_updates():
                print("  ", item)
        except OSError as exc:
            print(f"Code is applied, but tests could not be refreshed: {exc}", file=sys.stderr)
            return 2
    print(
        "Next: restart the worker/dashboard, then watch the flow board. A merchant "
        "deal whose conversion fails is parked and retried every 5 minutes instead "
        "of posting a link that pays nothing; set ALLOW_UNCONVERTED_POSTS=1 to post "
        "raw links immediately instead."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
