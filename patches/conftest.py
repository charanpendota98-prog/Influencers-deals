"""Keep pytest out of this bundle.

The real, collected tests live in the repository's tests/ directory. The
files in patches/tests/ are verified copies that ``--with-tests`` hands to
another checkout; collecting them twice breaks the run, so this conftest
ignores them without requiring any config in the receiving repo.
"""
collect_ignore_glob = ["tests/*"]
