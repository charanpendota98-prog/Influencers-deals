"""The deal text that came in is the deal text that goes out.

Two failures the operator sees in a real channel, and refuses to accept:

* **text that went missing** — a product title, a size list, a price line or a
  whole variant block that existed in the source post and never reached the
  creator's channel (a lost sale, and a post that reads like a different deal);
* **text that was never in the source** — a line the pipeline invented (a stray
  label, a leftover fragment of a watermark, half a sentence after a link was
  stripped) reaching a creator's followers under their name.

This module makes both *checkable*. It compares the deal text the pipeline was
about to render with what actually came out and classifies every difference:

* ``missing`` — lines with product meaning (a price, a product code, a store
  link, or real descriptive words) that are no longer in the output;
* ``added`` — output lines that are not derived from the source and are not an
  expected addition (the approval ``#ad (paid link)`` disclosure, the owner's own
  button footer);
* ``promo_dropped`` — source lines that were channel promotion/watermarks
  (invite links, "join @channel", admin contacts) and are *supposed* to go;
* ``links`` — which store links survived, which were retagged and which
  disappeared.

Comparisons are made on **canonical lines**: every URL is replaced by its
identity (host + path, i.e. the product) with the query string removed, so an
Amazon link that went from ``?tag=someoneelse-21`` to ``?tag=ravi-21`` counts as
the same link, kept — while a link that vanished counts as a loss.

The pipeline calls :func:`enforce` before delivering a post: when real text would
be lost it re-renders with the conservative renderer (every source line kept,
only URLs rewritten) and records the gap for the operator instead of posting
something poorer than the source.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from . import link_router

logger = logging.getLogger(__name__)

#: Additions the pipeline may make to a post without calling it "invented text".
#: The approval disclosure is a legal requirement; the button footer is the
#: owner's own navigation link, added deliberately per creator.
PIPELINE_ADDITIONS: tuple[str, ...] = (
    r"^\s*#ad\s*\(?\s*paid\s*link\s*\)?\s*$",
    r"^\s*#ad\b.*$",
    r"^\s*👉.*$",
    r"^\s*🔗.*$",
    r"^\s*(?:buy|shop)\s*(?:now|here)\s*:?.*$",
    r"^\s*[-–—•|=_*]{3,}\s*$",
)
_ADDITION_RES = tuple(re.compile(pattern, re.I) for pattern in PIPELINE_ADDITIONS)

#: A line with one of these is product-meaningful: losing it changes the deal.
_PRICE_RE = re.compile(
    r"(?i)(?:₹|rs\.?\s*\d|inr\s*\d|\b\d{2,6}\s*(?:rs|rupees)\b|\b\d+\s*%\s*(?:off|discount)|\bmrp\b)",
)
_PRODUCT_CODE_RE = re.compile(
    r"(?i)/(?:dp|gp/product|product)/[A-Za-z0-9]{6,}|/p/(?:itm)?[A-Za-z0-9]{6,}|\bB0[A-Z0-9]{8}\b",
)
_WORD_RE = re.compile(r"[A-Za-z\u0C00-\u0C7F]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

#: Lines that are *only* punctuation/emoji/separators carry no deal meaning.
_NOISE_RE = re.compile(r"^[\s\W_]+$")


def _canonical_url(url: str) -> str:
    """``host+path`` — the product identity, with the tracking query removed."""
    try:
        parsed = urlparse(url.strip())
    except Exception:  # pragma: no cover - defensive
        return url.strip().lower()
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    return f"{host}{path}" or url.strip().lower()


def canonical_line(line: str) -> str:
    """A line with every URL replaced by its product identity, lowercased."""
    def _swap(match: re.Match) -> str:
        return f"⟨{_canonical_url(match.group(0))}⟩"

    return re.sub(r"\s+", " ", _URL_RE.sub(_swap, str(line or ""))).strip().lower()


_NUMBER_RE = re.compile(r"\d{2,}(?:\.\d+)?")


def _strip_urls(text: str) -> str:
    """Text without its links: numbers inside a URL are not deal facts."""
    return _URL_RE.sub(" ", text or "")


def _tokens(text: str) -> list[str]:
    """Words **and** numbers: "₹1299" going missing is a lost sale, not formatting.

    URLs are removed first so a product code inside a link (``/itm1234``,
    ``B09N3ZNHTY``) is never mistaken for a price the post has to repeat.
    """
    body = _strip_urls(text)
    words = [token.lower() for token in _WORD_RE.findall(body)]
    numbers = _NUMBER_RE.findall(body)
    return words + numbers


def is_meaningful(line: str) -> bool:
    """Does this line carry deal meaning (title, price, code, description)?"""
    stripped = str(line or "").strip()
    if not stripped or _NOISE_RE.match(stripped):
        return False
    if link_router.is_promo_line(stripped):
        return False
    if _PRICE_RE.search(stripped) or _PRODUCT_CODE_RE.search(stripped):
        return True
    if _URL_RE.search(stripped):
        return True
    # Real prose: a product title is often just two words ("BOAT AIRDOPES"), so
    # two is the bar — watermarks were already excluded by is_promo_line above.
    return len(_tokens(stripped)) >= 2


def _significant_lines(text: str) -> list[str]:
    seen: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if is_meaningful(stripped):
            seen.append(stripped)
    return seen


def _copy_text(text: str) -> str:
    """The words of a post with every link (and its query) taken out."""
    without = _URL_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", without).strip().lower()


def _covered(line: str, rendered_tokens: set[str]) -> str:
    """Is the *copy* of ``line`` still in the rendered text?

    Returns ``""`` when it is, otherwise the reason (``"text"`` or ``"link"``).
    The link itself is judged separately and deliberately: the hub rewrites links
    on purpose (the creator's own Amazon tag, an EarnKaro conversion, a branded
    short link), so a product is "still there" when its words survived — whether
    the URL string is byte-identical is the link budget's business, not this
    function's.
    """
    words = [token for token in _tokens(line) if len(token) >= 2]
    if words:
        hits = sum(1 for token in words if token in rendered_tokens)
        if hits == len(words):
            return ""
        # A long line may have had a watermark tail trimmed; accept a strong majority.
        if hits >= max(2, int(len(words) * 0.8)) and hits >= len(words) - 2:
            return ""
        return "text"
    # Nothing but a link: the words are covered by the link budget below.
    return "" if _URL_RE.search(line) else "text"


def _is_allowed_addition(line: str) -> bool:
    return any(pattern.match(line) for pattern in _ADDITION_RES)


def compare(source: str, rendered: str) -> dict:
    """Classify every difference between the source deal text and the output.

    Text and links are judged separately, on purpose:

    * **text** — every product line's words must still be there ("text miss
      avvakudadu"): a title, a price, a size list or a description that vanished
      is reported with the exact line;
    * **links** — the hub *rewrites* links by design (the creator's own Amazon
      tag, an EarnKaro conversion, a branded short link or a first-party
      redirect), so what matters is the **link budget**: a post must never come
      out with fewer links than it went in with. Which of our own forms carries
      the product is not a fidelity problem, and a rewritten link is reported
      (``links.rewritten``) rather than counted as a loss.
    """
    source_lines = _significant_lines(source)
    rendered_tokens = set(_tokens(rendered))
    promo_dropped: list[str] = []
    other_dropped: list[str] = []
    for line in str(source or "").splitlines():
        stripped = line.strip()
        if not stripped or is_meaningful(stripped):
            continue
        if link_router.is_promo_line(stripped):
            promo_dropped.append(stripped)
        elif not _NOISE_RE.match(stripped):
            other_dropped.append(stripped)

    missing: list[str] = []
    for line in source_lines:
        if _covered(line, rendered_tokens) == "text":
            missing.append(line)

    source_links = [_canonical_url(url) for url in _URL_RE.findall(source)]
    rendered_links = [_canonical_url(url) for url in _URL_RE.findall(rendered)]
    source_url_lines = sum(1 for line in source_lines if _URL_RE.search(line))
    budget = max(source_url_lines, len(source_links))
    link_loss = budget > len(rendered_links)

    source_tokens = set(_tokens(source))
    source_canon = canonical_line(source)
    added: list[str] = []
    for line in str(rendered or "").splitlines():
        stripped = line.strip()
        if not stripped or _NOISE_RE.match(stripped):
            continue
        if _is_allowed_addition(stripped):
            continue
        if canonical_line(stripped) in source_canon:
            continue
        words = [token for token in _tokens(stripped) if len(token) >= 2]
        if words and all(token in source_tokens for token in words):
            continue
        urls = _URL_RE.findall(stripped)
        if urls and not words and len(rendered_links) <= budget:
            # A line that is nothing but a link: it is one of ours carrying a
            # source product, so it is not "invented text".
            continue
        added.append(stripped)

    dropped_links = [link for link in source_links if link not in rendered_links] \
        if link_loss else []

    report = {
        "ok": not missing and not added and not link_loss,
        "missing": missing,
        "link_loss": link_loss,
        "added": added,
        "promo_dropped": promo_dropped,
        "other_dropped": other_dropped,
        "links": {
            "source": len(source_links),
            "rendered": len(rendered_links),
            "dropped": dropped_links,
            # Links the hub rewrote on purpose (own tag, EarnKaro, shortener):
            # reported for the operator, never counted as a loss.
            "rewritten": len([link for link in source_links
                              if link not in rendered_links]) if not link_loss else 0,
            "kept": len(source_links) - len(dropped_links),
        },
        "counts": {
            "source_lines": len(source_lines),
            "missing": len(missing),
            "added": len(added),
            "promo_dropped": len(promo_dropped),
            "other_dropped": len(other_dropped),
        },
    }
    report["summary"] = summary_line(report)
    return report


def summary_line(report: dict) -> str:
    """One sentence for a log line, an audit row or the dashboard."""
    counts = report.get("counts") or {}
    if report.get("ok"):
        return (f"Source text preserved: {counts.get('source_lines', 0)} deal line(s), "
                f"{counts.get('promo_dropped', 0)} promotion line(s) removed, "
                f"nothing added.")
    parts = []
    if counts.get("missing"):
        parts.append(f"⛔ {counts['missing']} source line(s) missing")
    if counts.get("added"):
        parts.append(f"⛔ {counts['added']} invented line(s)")
    if report.get("link_loss"):
        parts.append("⛔ fewer links than the source post")
    if counts.get("promo_dropped"):
        parts.append(f"{counts['promo_dropped']} promotion line(s) removed")
    return "; ".join(parts) or "⛔ the post does not match its source"


def first_missing(report: dict, limit: int = 3) -> list[str]:
    return [str(line)[:120] for line in (report.get("missing") or [])[:limit]]


def enforce(source: str, rendered: str, *, fallback_render=None) -> tuple[str, dict]:
    """Guarantee the output keeps the source's deal text.

    ``fallback_render`` is called with no arguments when real text would be lost;
    it must return a more conservative render of the same source (the pipeline
    passes one that rewrites URLs only). The better of the two is returned, with
    the report of what was chosen.
    """
    report = compare(source, rendered)
    if report["ok"] or fallback_render is None:
        return rendered, report
    try:
        fallback = str(fallback_render() or "")
    except Exception as exc:  # pragma: no cover - a fallback must never break a post
        logger.warning("Fidelity fallback render failed: %s: %s", type(exc).__name__, exc)
        return rendered, report
    if not fallback.strip():
        return rendered, report
    fallback_report = compare(source, fallback)
    if (len(fallback_report["missing"]), len(fallback_report["added"])) < (
        len(report["missing"]), len(report["added"])
    ):
        fallback_report["recovered"] = True
        fallback_report["previous"] = {"missing": report["missing"], "added": report["added"]}
        return fallback, fallback_report
    return rendered, report
