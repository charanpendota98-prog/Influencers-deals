#!/usr/bin/env python3
"""Commission-leak fixes for `influencer_hub/` (the three verified leaks).

1. Amazon attribution is decided by the `tag`, not by the `/dp/` path.
   A search page / storefront / amzn.to short link carrying OUR Associate tag
   used to be flagged "not OUR canonical" and deleted by the commission guard,
   which threw away real commission. Now a URL with exactly one tag equal to
   ours is kept; only a wrong or missing tag is a leak.

2. EarnKaro converter payload.
   The API contract is
       {"deal": <clean merchant url>, "convert_option": "convert_only"}
   A wrong body answers HTTP 200 with no link, so Flipkart / Myntra / Ajio /
   Nykaa / Croma / Shopsy links were posted for free. Both `convert_one` and
   the live verifier now send the verified body.

3. Publisher provenance on converted links.
   The publisher id is read from `affExtParam2` *and* from a plain `id=`
   parameter (numeric, or equal to our own id). A link that carries somebody
   else's id is rejected back to the raw merchant URL instead of being posted
   as ours.

Usage (from the repo root)
--------------------------
    python3 patches/apply-commission-fixes.py
    python3 patches/apply-commission-fixes.py --check
    python3 patches/apply-commission-fixes.py --with-tests

Exit codes
----------
    0  applied (or `--check` confirmed it is already in place)
    1  already applied -> stops, never double-edits
    2  a file did not match the expected revision -> nothing was written

Safety notes
------------
* Touches only `influencer_hub/` (link_router, advanced_shortener,
  commission_guard, money_radar, pipeline, earnkaro). It never edits
  `dashboard/`, so the dashboard password policy is untouched.
* Exact-text replacements, validated before anything is written.
* Do NOT apply with `git checkout --` or `patch -U0`.
* Live verification on the VM (needs the configured token):
      python3 -m influencer_hub.cli verify-earnkaro
  Expected: ok: True, publisher_id: '5478322',
            publisher_provenance_verified: True.
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


class PatchError(RuntimeError):
    """Raised when a file does not look like the revision this patch expects."""


EDITS: list[tuple[str, str, str, str]] = [
    # ---------------- influencer_hub/link_router.py ----------------
    (
        "influencer_hub/link_router.py",
        "def _is_domain(host: str, domain: str) -> bool:\n"
        "    return host == domain or host.endswith(\".\" + domain)",
        "def _is_domain(host: str, domain: str) -> bool:\n"
        "    return host == domain or host.endswith(\".\" + domain)\n"
        "\n"
        "\n"
        "# Affiliaters/EarnKaro encode the publisher id in the query string. The API's\n"
        "# own parameter is `affExtParam2` (case-insensitive); converted links also\n"
        "# carry a plain `id`.\n"
        "PUBLISHER_PARAM_KEYS = (\"affextparam2\", \"id\")\n"
        "\n"
        "\n"
        "def _looks_like_publisher_id(value: str, expected_pubid: str = \"\") -> bool:\n"
        "    candidate = str(value or \"\").strip()\n"
        "    if not candidate:\n"
        "        return False\n"
        "    expected = str(expected_pubid or \"\").strip()\n"
        "    if expected and candidate.casefold() == expected.casefold():\n"
        "        return True\n"
        "    # A bare numeric id is a real publisher id; anything else (variant ids,\n"
        "    # session ids, slugs) is only trusted when it matches our own id.\n"
        "    return bool(re.fullmatch(r\"\\d{3,24}\", candidate))\n"
        "\n"
        "\n"
        "def publisher_ids_in_url(url: str, expected_pubid: str = \"\") -> list[str]:\n"
        "    \"\"\"Return the EarnKaro/Affiliaters publisher ids visible in a URL.\n"
        "\n"
        "    ``affExtParam2`` is always a publisher signal. ``id`` is weaker, so it only\n"
        "    counts when it matches the expected publisher or looks like a numeric\n"
        "    publisher id — unrelated merchant ``id=`` parameters must not be mistaken\n"
        "    for somebody else's publisher.\n"
        "    \"\"\"\n"
        "    try:\n"
        "        pairs = parse_qsl(urlparse(url).query, keep_blank_values=True)\n"
        "    except (TypeError, ValueError):\n"
        "        return []\n"
        "    values: list[str] = []\n"
        "    for key, value in pairs:\n"
        "        name = key.casefold()\n"
        "        if name == \"affextparam2\":\n"
        "            values.append(value)\n"
        "        elif name == \"id\" and _looks_like_publisher_id(value, expected_pubid):\n"
        "            values.append(value)\n"
        "    return values",
        "link_router: shared publisher-id reader (affExtParam2 + id)",
    ),
    # ---------------- influencer_hub/advanced_shortener.py ----------------
    (
        "influencer_hub/advanced_shortener.py",
        "def is_our_hypd_link(url: str, hypd_store_id: str) -> bool:",
        "def is_our_amazon_attribution(url: str, amazon_tag: str) -> bool:\n"
        "    \"\"\"True when an Amazon URL carries OUR Associate tag, whatever its route.\n"
        "\n"
        "    Amazon attribution travels in the ``tag`` query parameter, not in the path.\n"
        "    A search page (``/s?k=...``), a storefront or an ``amzn.to`` short link that\n"
        "    carries exactly one tag equal to ours earns exactly like a ``/dp/ASIN``\n"
        "    product page — so the audit must keep the page instead of deleting it.\n"
        "    ``is_our_amazon_link`` stays the stricter check used before we shorten or\n"
        "    re-mint a link ourselves.\n"
        "    \"\"\"\n"
        "    try:\n"
        "        tag = str(amazon_tag or \"\").strip()\n"
        "        if not tag or link_router.classify_url(url) != \"amazon\":\n"
        "            return False\n"
        "        parsed = urlparse(url)\n"
        "        tags = [\n"
        "            value for key, value in parse_qsl(parsed.query, keep_blank_values=True)\n"
        "            if key.casefold() == \"tag\" and value\n"
        "        ]\n"
        "        return len(tags) == 1 and tags[0] == tag\n"
        "    except Exception:\n"
        "        return False\n"
        "\n"
        "\n"
        "def is_our_hypd_link(url: str, hypd_store_id: str) -> bool:",
        "advanced_shortener: is_our_amazon_attribution()",
    ),
    # ---------------- influencer_hub/commission_guard.py ----------------
    (
        "influencer_hub/commission_guard.py",
        "from .advanced_shortener import is_our_amazon_link, is_our_hypd_link",
        "from .advanced_shortener import (\n"
        "    is_our_amazon_attribution,\n"
        "    is_our_amazon_link,\n"
        "    is_our_hypd_link,\n"
        ")",
        "commission_guard: import the attribution helper",
    ),
    (
        "influencer_hub/commission_guard.py",
        "def _affextparam2_values(url: str) -> list[str]:\n"
        "    try:\n"
        "        return [v for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True) if k.casefold() == \"affextparam2\"]\n"
        "    except Exception:\n"
        "        return []",
        "def _publisher_id_values(url: str, expected_pubid: str = \"\") -> list[str]:\n"
        "    \"\"\"EarnKaro publisher ids visible in a link (``affExtParam2`` and ``id``).\"\"\"\n"
        "    try:\n"
        "        return link_router.publisher_ids_in_url(url, expected_pubid)\n"
        "    except Exception:\n"
        "        return []",
        "commission_guard: read publisher ids (affExtParam2 + id)",
    ),
    (
        "influencer_hub/commission_guard.py",
        "            elif is_our_amazon_link(url, effective_tag):\n"
        "                entry[\"reason\"] = f\"OUR Amazon verified tag={effective_tag}\"\n"
        "            else:\n"
        "                parsed = urlparse(url)\n"
        "                tags = [v for k, v in parse_qsl(parsed.query) if k.lower() == \"tag\"]\n"
        "                if _host_of(url) in {\"amzn.to\", \"www.amzn.to\", \"amzn.in\", \"www.amzn.in\"}:\n"
        "                    if tags == [effective_tag]:\n"
        "                        entry[\"reason\"] = f\"amzn.to with OUR tag (short code opaque, tag appended): {url}\"\n"
        "                        issues.append(f\"WARN amzn.to opaque link (commission best-effort, prefer /dp/ASIN): {url}\")\n"
        "                    else:\n"
        "                        entry[\"ok\"] = False\n"
        "                        entry[\"reason\"] = f\"amzn.to without OUR tag (expected {effective_tag}): {url}\"\n"
        "                        amazon_ok = False\n"
        "                        issues.append(entry[\"reason\"])\n"
        "                else:\n"
        "                    entry[\"ok\"] = False\n"
        "                    entry[\"reason\"] = f\"Amazon link not OUR canonical (expected tag {effective_tag}): {url}\"\n"
        "                    amazon_ok = False\n"
        "                    issues.append(entry[\"reason\"])",
        "            elif is_our_amazon_link(url, effective_tag):\n"
        "                entry[\"reason\"] = f\"OUR Amazon verified tag={effective_tag}\"\n"
        "            elif is_our_amazon_attribution(url, effective_tag):\n"
        "                # A search page, a storefront or a short link cannot be turned\n"
        "                # into /dp/ASIN without a network round-trip, but commission is\n"
        "                # carried by the tag — so the page is KEPT, never deleted.\n"
        "                if _host_of(url) in {\"amzn.to\", \"www.amzn.to\", \"amzn.in\", \"www.amzn.in\"}:\n"
        "                    entry[\"reason\"] = f\"amzn.to with OUR tag (short code opaque, tag appended): {url}\"\n"
        "                    issues.append(f\"WARN amzn.to opaque link (commission best-effort, prefer /dp/ASIN): {url}\")\n"
        "                else:\n"
        "                    entry[\"reason\"] = (\n"
        "                        f\"OUR Amazon page kept as-is (tag={effective_tag}, \"\n"
        "                        f\"route {urlparse(url).path or '/'}) — attribution comes \"\n"
        "                        \"from the tag, not from the /dp/ path\"\n"
        "                    )\n"
        "            else:\n"
        "                parsed = urlparse(url)\n"
        "                tags = [v for k, v in parse_qsl(parsed.query) if k.lower() == \"tag\"]\n"
        "                if _host_of(url) in {\"amzn.to\", \"www.amzn.to\", \"amzn.in\", \"www.amzn.in\"}:\n"
        "                    entry[\"ok\"] = False\n"
        "                    entry[\"reason\"] = f\"amzn.to without OUR tag (expected {effective_tag}): {url}\"\n"
        "                    amazon_ok = False\n"
        "                    issues.append(entry[\"reason\"])\n"
        "                else:\n"
        "                    entry[\"ok\"] = False\n"
        "                    entry[\"reason\"] = f\"Amazon link not OUR canonical (expected tag {effective_tag}): {url}\"\n"
        "                    amazon_ok = False\n"
        "                    issues.append(entry[\"reason\"])",
        "commission_guard: keep tag-attributed Amazon pages",
    ),
    (
        "influencer_hub/commission_guard.py",
        "            # Verify provenance if expected_pubid known — check affExtParam2 if visible\n"
        "            pubids = _affextparam2_values(url)",
        "            # Verify provenance if expected_pubid known — check the publisher\n"
        "            # id parameters visible on the short link itself.\n"
        "            pubids = _publisher_id_values(url, expected_pubid)",
        "commission_guard: id-aware provenance on short links",
    ),
    (
        "influencer_hub/commission_guard.py",
        "                        entry[\"reason\"] = f\"EarnKaro short has wrong pubid {pubids} (expected {expected_pubid}): {url}\"",
        "                        entry[\"reason\"] = f\"EarnKaro short has wrong publisher {pubids} (expected {expected_pubid}): {url}\"",
        "commission_guard: clearer provenance wording",
    ),
    # ---------------- influencer_hub/money_radar.py ----------------
    (
        "influencer_hub/money_radar.py",
        "def _publisher_ids(url: str) -> list[str]:\n"
        "    try:\n"
        "        return [\n"
        "            value for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True)\n"
        "            if key.casefold() == \"affextparam2\"\n"
        "        ]\n"
        "    except Exception:\n"
        "        return []",
        "def _publisher_ids(url: str, expected_pubid: str = \"\") -> list[str]:\n"
        "    \"\"\"Publisher ids visible in a link (``affExtParam2`` and plain ``id``).\"\"\"\n"
        "    try:\n"
        "        return link_router.publisher_ids_in_url(url, expected_pubid)\n"
        "    except Exception:\n"
        "        return []",
        "money_radar: read publisher ids (affExtParam2 + id)",
    ),
    (
        "influencer_hub/money_radar.py",
        "    if kind == \"amazon\":\n"
        "        tags = [value for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True)\n"
        "                if key.casefold() == \"tag\"]\n"
        "        if tag and advanced_shortener.is_our_amazon_link(url, tag):\n"
        "            entry[\"state\"] = STATE_EARNING\n"
        "            entry[\"reason\"] = f\"Amazon with our tag {tag}\"\n"
        "        elif not tags:",
        "    if kind == \"amazon\":\n"
        "        tags = [value for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True)\n"
        "                if key.casefold() == \"tag\"]\n"
        "        if tag and advanced_shortener.is_our_amazon_link(url, tag):\n"
        "            entry[\"state\"] = STATE_EARNING\n"
        "            entry[\"reason\"] = f\"Amazon with our tag {tag}\"\n"
        "        elif tag and advanced_shortener.is_our_amazon_attribution(url, tag):\n"
        "            # Search pages and storefronts cannot be canonicalised to /dp/ASIN,\n"
        "            # but the tag carries the commission, so they still earn.\n"
        "            entry[\"state\"] = STATE_EARNING\n"
        "            entry[\"reason\"] = (\n"
        "                f\"Amazon page with our tag {tag} kept as-is \"\n"
        "                \"(attribution by tag, not by the /dp/ path)\"\n"
        "            )\n"
        "        elif not tags:",
        "money_radar: tag-attributed Amazon pages earn",
    ),
    (
        "influencer_hub/money_radar.py",
        "        ids = _publisher_ids(url)",
        "        ids = _publisher_ids(url, pubid)",
        "money_radar: pass the expected publisher id",
    ),
    # ---------------- influencer_hub/pipeline.py ----------------
    (
        "influencer_hub/pipeline.py",
        "                            if kind == \"amazon\" and effective_amz_tag and advanced_shortener.is_our_amazon_link(u, effective_amz_tag):\n"
        "                                has_affiliate = True\n"
        "                                break",
        "                            if kind == \"amazon\" and effective_amz_tag and (\n"
        "                                advanced_shortener.is_our_amazon_link(u, effective_amz_tag)\n"
        "                                or advanced_shortener.is_our_amazon_attribution(u, effective_amz_tag)\n"
        "                            ):\n"
        "                                # A tagged search/store page keeps our attribution\n"
        "                                # even though it is not a /dp/ASIN link.\n"
        "                                has_affiliate = True\n"
        "                                break",
        "pipeline: a tagged Amazon page still counts as OUR affiliate",
    ),
    # ---------------- influencer_hub/earnkaro.py ----------------
    (
        "influencer_hub/earnkaro.py",
        "async def _resolve_affextparam2(session: aiohttp.ClientSession, link: str,\n"
        "                                 timeout: float = 8.0) -> str | None:\n"
        "    \"\"\"Follow redirects and return the affExtParam2 of the final URL (best-effort).\"\"\"\n"
        "    try:\n"
        "        async with session.get(\n"
        "            link, allow_redirects=True,\n"
        "            timeout=aiohttp.ClientTimeout(total=timeout),\n"
        "            headers={\"User-Agent\": \"Mozilla/5.0\"},\n"
        "        ) as resp:\n"
        "            final = str(resp.url)\n"
        "    except Exception:\n"
        "        return None\n"
        "    return next(iter(_affextparam2_values(final)), None)",
        "def _publisher_id_values(link: str, expected_pubid: str = \"\") -> list[str]:\n"
        "    \"\"\"Publisher IDs visible on a link: ``affExtParam2`` and plain ``id``.\n"
        "\n"
        "    The converter sometimes returns a merchant URL that carries our publisher\n"
        "    id as ``id=`` instead of ``affExtParam2=``. Both are checked, and a link\n"
        "    that carries somebody else's id is rejected back to the raw URL.\n"
        "    \"\"\"\n"
        "    return link_router.publisher_ids_in_url(link, expected_pubid)\n"
        "\n"
        "\n"
        "async def _resolve_final_url(session: aiohttp.ClientSession, link: str,\n"
        "                             timeout: float = 8.0) -> str | None:\n"
        "    \"\"\"Follow redirects and return the final URL (best-effort).\"\"\"\n"
        "    try:\n"
        "        async with session.get(\n"
        "            link, allow_redirects=True,\n"
        "            timeout=aiohttp.ClientTimeout(total=timeout),\n"
        "            headers={\"User-Agent\": \"Mozilla/5.0\"},\n"
        "        ) as resp:\n"
        "            return str(resp.url)\n"
        "    except Exception:\n"
        "        return None\n"
        "\n"
        "\n"
        "async def _resolve_affextparam2(session: aiohttp.ClientSession, link: str,\n"
        "                                timeout: float = 8.0) -> str | None:\n"
        "    \"\"\"Follow redirects and return the affExtParam2 of the final URL (best-effort).\"\"\"\n"
        "    final = await _resolve_final_url(session, link, timeout=timeout)\n"
        "    if final is None:\n"
        "        return None\n"
        "    return next(iter(_affextparam2_values(final)), None)\n"
        "\n"
        "\n"
        "async def _resolve_publisher_id(session: aiohttp.ClientSession, link: str,\n"
        "                                expected_pubid: str = \"\",\n"
        "                                timeout: float = 8.0) -> str | None:\n"
        "    \"\"\"Follow redirects and return the publisher id the landing page carries.\"\"\"\n"
        "    final = await _resolve_final_url(session, link, timeout=timeout)\n"
        "    if final is None:\n"
        "        return None\n"
        "    return next(iter(_publisher_id_values(final, expected_pubid)), None)",
        "earnkaro: id-aware publisher resolution for short links",
    ),
    (
        "influencer_hub/earnkaro.py",
        "    if expected_pubid:\n"
        "        publisher_ids = _affextparam2_values(result)",
        "    if expected_pubid:\n"
        "        publisher_ids = _publisher_id_values(result, expected_pubid)",
        "earnkaro: direct results accept affExtParam2 or id",
    ),
    (
        "influencer_hub/earnkaro.py",
        "                json={\"deal\": api_deal_url},",
        "                # Verified contract of the Affiliaters/EarnKaro converter:\n"
        "                # {\"deal\": <clean merchant url>, \"convert_option\": \"convert_only\"}.\n"
        "                # A wrong body returns HTTP 200 with no link, which silently\n"
        "                # posted free (unpaid) merchant links.\n"
        "                json={\"deal\": api_deal_url, \"convert_option\": \"convert_only\"},",
        "earnkaro: verified converter payload (convert_option)",
    ),
    (
        "influencer_hub/earnkaro.py",
        "                if effective_ek_pubid and _is_shortener(converted):\n"
        "                    resolved_pubid = await _resolve_affextparam2(session, converted)\n"
        "                    if resolved_pubid and resolved_pubid != effective_ek_pubid:",
        "                if effective_ek_pubid and _is_shortener(converted):\n"
        "                    resolved_pubid = await _resolve_publisher_id(\n"
        "                        session, converted, effective_ek_pubid\n"
        "                    )\n"
        "                    if resolved_pubid and resolved_pubid != effective_ek_pubid:",
        "earnkaro: short-link provenance checks id too",
    ),
    (
        "influencer_hub/earnkaro.py",
        "                json={\"deal\": test_url},",
        "                json={\"deal\": test_url, \"convert_option\": \"convert_only\"},",
        "earnkaro: live verifier sends the verified payload",
    ),
    (
        "influencer_hub/earnkaro.py",
        "        if link and expected_pubid:\n"
        "            if _is_shortener(link):\n"
        "                resolved_pubid = await _resolve_affextparam2(session, link)\n"
        "                if resolved_pubid is not None:\n"
        "                    provenance_verified = resolved_pubid == expected_pubid\n"
        "            else:\n"
        "                publisher_ids = _affextparam2_values(link)",
        "        if link and expected_pubid:\n"
        "            if _is_shortener(link):\n"
        "                resolved_pubid = await _resolve_publisher_id(\n"
        "                    session, link, expected_pubid\n"
        "                )\n"
        "                if resolved_pubid is not None:\n"
        "                    provenance_verified = resolved_pubid == expected_pubid\n"
        "            else:\n"
        "                publisher_ids = _publisher_id_values(link, expected_pubid)",
        "earnkaro: live verifier provenance reads id too",
    ),
]

MODULE_DOC = (
    "influencer_hub/earnkaro.py",
    '"""EarnKaro converter client.\n'
    "\n"
    "The configured integration posts a cleaned merchant URL to the EarnKaro\n"
    "converter endpoint with a Bearer credential and parses a returned affiliate\n"
    "link.",
    '"""EarnKaro converter client.\n'
    "\n"
    "The configured integration posts a cleaned merchant URL plus\n"
    '`"convert_option": "convert_only"` to the EarnKaro converter endpoint with a\n'
    "Bearer credential and parses a returned affiliate link.",
    "earnkaro: module docstring mentions convert_option",
)


def _stage(path: Path, pending: dict[Path, str]) -> str:
    text = pending.get(path)
    if text is None:
        if not path.is_file():
            raise PatchError(f"missing file {path.relative_to(REPO_ROOT)}")
        text = path.read_text(encoding="utf-8")
        pending[path] = text
    return text


def apply_edits(pending: dict[Path, str], stats: list[str]) -> None:
    edits = list(EDITS) + [MODULE_DOC]
    for relative, old, new, label in edits:
        path = REPO_ROOT / relative
        text = _stage(path, pending)
        count = text.count(old)
        expected = 1
        if relative == "influencer_hub/money_radar.py" and old == "        ids = _publisher_ids(url)":
            expected = 2
        if count != expected:
            raise PatchError(
                f"{label}: expected {expected} match(es) in {relative}, found {count}. "
                "This file is not the revision the patch was verified against."
            )
        pending[path] = text.replace(old, new, expected)
        stats.append(f"edit  {label}")


def policy_in_place() -> bool:
    """True when the commission fixes are already applied."""
    earnkaro_text = (REPO_ROOT / "influencer_hub" / "earnkaro.py").read_text(
        encoding="utf-8"
    )
    guard_text = (REPO_ROOT / "influencer_hub" / "commission_guard.py").read_text(
        encoding="utf-8"
    )
    return (
        '"convert_option": "convert_only"' in earnkaro_text
        and "is_our_amazon_attribution" in guard_text
        and "_resolve_publisher_id" in earnkaro_text
    )


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
        description="Apply the three verified commission-leak fixes."
    )
    parser.add_argument("--check", action="store_true",
                        help="report whether the fixes are in place; write nothing")
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
            "✅ Commission fixes already applied — nothing to do.\n"
            "   (convert_option payload + tag-based Amazon attribution + id-aware\n"
            "   publisher provenance are all in place.)\n"
            "   Double-applying is refused on purpose: this patch never edits a\n"
            "   file twice."
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

    for path, text in pending.items():
        path.write_text(text, encoding="utf-8")
    print(f"✅ Commission fixes applied to {len(pending)} files.")

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
        "   python3 -m influencer_hub.cli verify-earnkaro\n"
        "      # expect ok: True, publisher_id: '5478322',\n"
        "      #        publisher_provenance_verified: True\n"
        "   pytest -q tests/test_earnkaro.py tests/test_earnkaro_live.py \\\n"
        "            tests/test_earnkaro_provenance.py tests/test_amazon_tag_attribution.py\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
