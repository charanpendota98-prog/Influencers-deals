#!/usr/bin/env python3
"""Atomic delivery-safety and product-category bundle installer. Never starts services or contacts Telegram."""
import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PATCH = Path(__file__).with_name('delivery-safety.patch')


def git(*args):
    return subprocess.run(['git', *args, str(PATCH)], cwd=ROOT,
                          capture_output=True, text=True)


def smoke():
    # Syntax validation always works even without deployment dependencies.
    for name in ('db', 'channel_health', 'cli', 'pipeline', 'link_router', 'selfcheck', 'telegram_ops', 'categories', 'onboarding', 'category_preferences'):
        ast.parse((ROOT / 'influencer_hub' / (name + '.py')).read_text())
    ast.parse((ROOT / 'dashboard' / 'app.py').read_text())
    result = subprocess.run([sys.executable, '-c',
        'from influencer_hub import db, channel_health, cli, pipeline, link_router, selfcheck, categories, onboarding, category_preferences'],
        cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True)
    if result.returncode:
        if 'ModuleNotFoundError' not in result.stderr:
            raise RuntimeError(result.stderr)
        print('Dependencies unavailable: syntax fallback passed; run full tests in deployment venv.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--check', action='store_true')
    flags.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not PATCH.is_file():
        print('Missing delivery-safety.patch', file=sys.stderr)
        return 2
    installed = git('apply', '--reverse', '--check').returncode == 0
    if installed:
        smoke()
        print('Delivery safety already installed; no files changed.')
        return 0 if args.check or args.dry_run else 1
    if args.check:
        print('Delivery safety not fully installed.')
        return 1
    checked = git('apply', '--check')
    if checked.returncode:
        print(checked.stderr, file=sys.stderr)
        return 2
    if args.dry_run:
        print('Patch validated; no files changed.')
        return 0
    applied = git('apply')  # no --reject: all hunks apply atomically or none do
    if applied.returncode:
        print(applied.stderr, file=sys.stderr)
        return 2
    try:
        smoke()
    except Exception:
        undone = git('apply', '--reverse')
        if undone.returncode:
            print('Rollback failed: inspect working tree.', file=sys.stderr)
        raise
    print('Delivery safety installed; services were not restarted.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
