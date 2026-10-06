"""The `patches/` bundle must stay in step with the tree it describes.

Three ways to hand this work to another checkout, and all three have to agree:

1. `patches/*.patch` — `git apply` on a clean `0bf913f` checkout;
2. `patches/apply-*.py` — surgical appliers for a VM that already has part of
   the work;
3. `patches/tests/` — verified copies of the test files that `--with-tests`
   installs.

If any of them drifts from the real sources, a new session would replay *older*
code and the suite would fail there for reasons that do not exist here. These
tests fail first, on this branch.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PATCH_DIR = REPO_ROOT / "patches"
TESTS_COPY_DIR = PATCH_DIR / "tests"

PATCH_FILES = (
    "commission-leaks.patch",
    "password-policy.patch",
    "tests.patch",
    "deal-flow.patch",
)
APPLIERS = (
    "apply-commission-fixes.py",
    "apply-password-policy.py",
    "apply-account-model.py",
    "apply-deal-flow.py",
)


def _load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(f"patch_bundle_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _in_place(module) -> bool:
    """Each applier names its own idempotency check slightly differently."""
    for name in ("policy_in_place", "_policy_in_place", "flow_in_place"):
        if hasattr(module, name):
            return bool(getattr(module, name)())
    raise AssertionError("applier has no in-place check")


# --------------------------------------------------------------------------
# 1. The git-apply patches
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", PATCH_FILES)
def test_patch_file_is_present_and_well_formed(name):
    patch = PATCH_DIR / name
    assert patch.is_file(), f"patches/{name} is missing"
    text = patch.read_text(encoding="utf-8")
    assert text.startswith("diff --git "), "not a git diff"
    assert "--- a/" in text or "--- /dev/null" in text
    assert "+++ b/" in text
    assert len(text.splitlines()) > 50, "suspiciously small patch"


def test_patches_cover_the_touched_trees():
    """The three patch files together cover influencer_hub/, dashboard/ and tests/."""
    covered: set[str] = set()
    for name in PATCH_FILES:
        for line in (PATCH_DIR / name).read_text(encoding="utf-8").splitlines():
            if line.startswith("diff --git a/"):
                covered.add(line.split(" b/", 1)[0][len("diff --git a/"):])
    assert any(path.startswith("influencer_hub/") for path in covered)
    assert any(path.startswith("dashboard/") for path in covered)
    assert any(path.startswith("tests/") for path in covered)
    # Every path a patch touches must exist in the tree now.
    for path in sorted(covered):
        assert (REPO_ROOT / path).exists(), f"{path} is in a patch but not in the tree"


# --------------------------------------------------------------------------
# 2. The appliers
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", APPLIERS)
def test_applier_is_present_and_reports_the_branch_state(name):
    module = _load(PATCH_DIR / name)
    # On this branch every applier must report "already applied": that is what
    # makes a rerun on the VM stop instead of double-editing.
    assert _in_place(module) is True, f"patches/{name} thinks the branch is unpatched"


def test_applier_edits_point_at_real_files_and_real_kinds():
    for name in APPLIERS:
        module = _load(PATCH_DIR / name)
        for relative, old, _new, _label in module.EDITS:
            assert old.strip(), f"{name}: empty search text"
            target = REPO_ROOT / relative
            assert target.is_file(), f"{name}: {relative} does not exist"


def test_account_applier_embeds_the_real_module_byte_for_byte():
    module = _load(PATCH_DIR / "apply-account-model.py")
    real = (REPO_ROOT / "influencer_hub" / "accounts.py").read_text(encoding="utf-8")
    assert module.ACCOUNTS_MODULE == real


# --------------------------------------------------------------------------
# 3. The verified test copies
# --------------------------------------------------------------------------
def test_test_copies_are_byte_identical_to_the_real_tests():
    copies = sorted(TESTS_COPY_DIR.glob("test_*.py"))
    assert copies, "patches/tests/ is empty"
    for copy in copies:
        real = REPO_ROOT / "tests" / copy.name
        assert real.is_file(), f"{copy.name} has no counterpart in tests/"
        assert copy.read_text(encoding="utf-8") == real.read_text(encoding="utf-8"), (
            f"patches/tests/{copy.name} drifted from tests/{copy.name}; "
            "re-copy it so --with-tests cannot install stale tests"
        )


def test_bundle_keeps_itself_out_of_the_suite():
    """patches/tests/ must never be collected twice."""
    conftest = (PATCH_DIR / "conftest.py").read_text(encoding="utf-8")
    assert "collect_ignore_glob" in conftest
    assert "tests/*" in conftest
