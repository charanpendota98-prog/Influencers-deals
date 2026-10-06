"""Who earns on which network — the account model in one place.

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
"""
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
    """Read a global setting without ever exploding on a fresh database."""
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
    """True when EarnKaro/HYPD must use OUR accounts, whatever a row says."""
    setting = _global_setting(
        CENTRAL_ACCOUNTS_SETTING, bool(config.CENTRAL_NETWORK_ACCOUNTS)
    )
    return _truthy(setting)


def central_hypd_store_id() -> str:
    """OUR HYPD store id: vault/global setting first, then the environment."""
    configured = _global_setting("hypd_store_id", "") or config.HYPD_STORE_ID
    return str(configured or config.HYPD_STORE_ID).strip() or config.HYPD_STORE_ID


def central_earnkaro_publisher_id() -> str:
    """OUR EarnKaro/Affiliaters publisher id."""
    configured = (
        _global_setting("earnkaro_publisher_id", "")
        or config.EARNKARO_PUBLISHER_ID
        or ""
    )
    return str(configured).strip()


def central_earnkaro_api_key() -> str:
    """OUR EarnKaro/Affiliaters API token (never logged, never cached)."""
    configured = _global_setting("earnkaro_api_key", "") or config.EARNKARO_API_KEY
    return str(configured).strip()


def creator_amazon_tag(
    influencer: dict | None = None, channel: dict | None = None
) -> str:
    """The tag an Amazon link is posted with: the creator's own id.

    Priority: the channel's explicit override (still an operator-entered id for
    that creator), then the creator's own tag, then the configured fallback so a
    deal is never dropped for a missing profile value.
    """
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
    """Where :func:`creator_amazon_tag` got its value: channel/creator/fallback."""
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
    """True when the post is signed with the creator's own id, not our fallback."""
    return amazon_tag_source(influencer, channel) in {"channel", "creator"}


def hypd_store_for(
    influencer: dict | None = None, channel: dict | None = None
) -> str:
    """The HYPD store id that will appear in posts.

    With central accounts on (the default) this is always OUR store, so a stale
    or copied per-creator value cannot take HYPD commission elsewhere. Turning
    the setting off restores the legacy channel -> creator -> central priority.
    """
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
    """The (amazon_tag, hypd_store) pair the pipeline will actually use."""
    return (
        creator_amazon_tag(influencer, channel),
        hypd_store_for(influencer, channel),
    )


def model_rows(
    amazon_tag: str = "", hypd_store: str = "", earnkaro_pubid: str = ""
) -> list[dict]:
    """Human-readable rows of the model, for the dashboard and the preview."""
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
    """Creators whose Amazon posts are not signed with an id of their own.

    A profile counts as missing when it has no tag, or when its tag is still the
    configured fallback (`config.AMAZON_ASSOCIATE_TAG`) — which is what a profile
    created without a tag ends up storing. Operator checklist material: for these
    creators the Amazon post earns under the fallback tag, not theirs.
    """
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
