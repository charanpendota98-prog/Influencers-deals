"""Regression coverage for the production ingestion, routing and worker fixes."""
from __future__ import annotations

import asyncio
import re
import sqlite3
from unittest.mock import AsyncMock


from influencer_hub import config, db, link_router, pipeline, puller, telegram_ops, worker


def test_official_defaults_and_compact_amazon_link():
    assert config.AMAZON_ASSOCIATE_TAG == "mama086-21"
    assert config.TELEGRAM_API_ID == "33595682"
    assert config.AMAZON_CREATORS_API_CLIENT_ID == (
        "amzn1.application-oa2-client.83229d9d14664351be9fc2059038a4f2"
    )
    raw = (
        "https://www.amazon.in/Long-Product-Title/dp/B0ABCDEFGH/ref=sr_1_1"
        "?tag=old-21&ref_=abc&linkCode=xyz&campaign=feed#details"
    )
    assert link_router.apply_amazon_tag(raw) == (
        "https://www.amazon.in/dp/B0ABCDEFGH?tag=mama086-21"
    )


def test_amazon_long_link_retagging_keeps_product_variant_and_replaces_old_tag():
    source = (
        "https://www.amazon.in/dp/B0HJR9W8FP?tag=old-21&th=1&linkCode=sl1"
        "&ref_=as_li_ss_tl&psc=1"
    )
    assert link_router.apply_amazon_tag(source, "lootsxpert-21") == (
        "https://www.amazon.in/dp/B0HJR9W8FP?th=1&psc=1&tag=lootsxpert-21"
    )


def test_unrestricted_filters_and_merchant_rewrites_are_permissive():
    deal = "Shopsy kurti deal ₹1,299/- https://www.shopsy.in/kurti/p/itm1234567890?pid=KURTI"
    for unrestricted in ("all", "unrestricted", "any", "*", "all,clothing"):
        assert link_router.matches_category_filter(deal, unrestricted)
    assert pipeline._parse_price_filter_spec("unrestricted") == (None, None)
    assert link_router.matches_price_filter("Meesho deal, price not shown", max_price=99)
    assert link_router.classify_url("https://fktr.in/abc123") == "merchant"
    assert link_router.compact_merchant_url("https://fktr.in/abc123") == "https://fktr.in/abc123"

    links = {
        "https://www.shopsy.in/kurti/p/itm1234567890?pid=KURTI": "https://ekaro.in/shopsy",
        "https://www.flipkart.com/headphones/p/itmABC123?pid=HEADPHONE1": "https://ekaro.in/flipkart",
        "https://www.myntra.com/shoes/brand/running-shoes/1234567/buy?utm_source=feed": "https://ekaro.in/myntra",
        "https://www.ajio.com/men/p/460123456_blue": "https://ekaro.in/ajio",
    }
    source = "\n".join(links) + "\nMeesho: https://hypd.store/12345/afflink/abcde12345"
    rendered = link_router.render_for_influencer(
        source, config.AMAZON_ASSOCIATE_TAG, links, hypd_store_id="93944"
    )
    for expected in (*links.values(), "https://hypd.store/93944/afflink/abcde12345"):
        assert expected in rendered
    for original in links:
        assert original not in rendered


def test_active_channel_and_all_toggles_do_not_drop_shopsy_deal(monkeypatch):
    db.init()
    influencer_id = db.add_influencer("Release Regression")
    channel_id = db.add_channel(
        influencer_id,
        "telegram",
        "@release_regression",
        status="active",
        categories="unrestricted",
        price_filter="unrestricted",
        only_amazon="all",
        allow_amazon="all",
        allow_earnkaro="unrestricted",
        allow_hypd="all",
    )
    sent = []

    async def fake_dispatch(influencer, channel, text):
        sent.append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    try:
        result = asyncio.run(pipeline.render_and_dispatch(
            "🔥 Shopsy loot at ₹199/-\n"
            "Amazon: https://amazon.in/dp/B0ABCDEFGH?ref=long&tag=old-21\n"
            "Flipkart: https://www.flipkart.com/p/itm1234567890?pid=SHOE1\n"
            "Meesho: https://hypd.store/55555/afflink/abcde12345",
            influencer_ids=[influencer_id],
        ))
        assert result[influencer_id][channel_id] == "posted"
        assert len(sent) == 1
        assert "https://www.amazon.in/dp/B0ABCDEFGH?tag=mama086-21" in sent[0]
        assert "https://www.flipkart.com/p/itm1234567890?pid=SHOE1" in sent[0]
        assert "https://hypd.store/93944/afflink/abcde12345" in sent[0]
    finally:
        db.delete_influencer(influencer_id)


def test_amazon_associate_links_are_not_sent_to_bitly(monkeypatch):
    db.init()
    influencer_id = db.add_influencer(
        "No Amazon Shortening", bitly_api_key="test-bitly-token"
    )
    channel_id = db.add_channel(
        influencer_id, "telegram", "@no_amazon_shortening", status="ready"
    )
    calls = []
    sent = []

    async def fake_shorten(urls, token=None):
        calls.append((urls, token))
        return {url: "https://bit.ly/test" for url in urls}

    async def fake_dispatch(influencer, channel, text):
        sent.append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline.bitly_client, "shorten_urls", fake_shorten)
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    try:
        result = asyncio.run(pipeline.render_and_dispatch(
            "Amazon headphones https://amazon.in/dp/B0ABCDEFGH?tag=old-21&ref=feed",
            influencer_ids=[influencer_id],
        ))
        assert result[influencer_id][channel_id] == "posted"
        assert calls == []
        assert "https://www.amazon.in/dp/B0ABCDEFGH?tag=mama086-21" in sent[0]
    finally:
        db.delete_influencer(influencer_id)


def test_first_party_amazon_short_links_preserve_influencer_tag_and_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "amazon-shortlinks.sqlite3")
    db.init()
    monkeypatch.setattr(config, "AMAZON_SHORT_LINK_BASE_URL", "https://amz.example.test")
    influencer_id = db.add_influencer("Short-link Creator", "creator-21", bitly_api_key="unused")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@short_link_creator", status="ready"
    )
    sent = []

    async def fake_dispatch(influencer, channel, text):
        sent.append(text)
        return "posted"

    async def bitly_must_not_be_used(*_args, **_kwargs):
        raise AssertionError("Amazon links must not be sent to Bitly")

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline.bitly_client, "shorten_urls", bitly_must_not_be_used)
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    try:
        result = asyncio.run(pipeline.render_and_dispatch(
            "Product https://amazon.in/dp/B0ABCDEFGH?tag=source-21&th=1&ref=tracking",
            influencer_ids=[influencer_id],
        ))
        assert result[influencer_id][channel_id] == "posted"
        match = re.search(
            r"https://amz\.example\.test/amazon/([A-Za-z0-9_-]{8})\?tag=creator-21",
            sent[0],
        )
        assert match
        record = db.get_amazon_short_link(match.group(1))
        assert record["associate_tag"] == "creator-21"
        assert record["target_url"] == "https://www.amazon.in/dp/B0ABCDEFGH?th=1&tag=creator-21"

        # The redirect preserves the tag and cannot be retargeted by changing it.
        from dashboard.app import app
        client = app.test_client()
        response = client.get(f"/amazon/{match.group(1)}?tag=creator-21")
        assert response.status_code == 302
        assert response.headers["Location"] == record["target_url"]
        assert client.get(f"/amazon/{match.group(1)}?tag=other-21").status_code == 404
    finally:
        db.delete_influencer(influencer_id)


def test_two_influencers_get_independent_amazon_shortlinks_and_tags(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "multi-amazon-tags.sqlite3")
    monkeypatch.setattr(config, "AMAZON_SHORT_LINK_BASE_URL", "https://go.example.test")
    db.init()
    creator_one = db.add_influencer("Creator One", "creator-one-21")
    creator_two = db.add_influencer("Creator Two", "creator-two-21")
    channel_one = db.add_channel(
        creator_one, "telegram", "@creator_one", status="ready"
    )
    channel_two = db.add_channel(
        creator_two, "telegram", "@creator_two", status="ready"
    )
    sent = {}

    async def fake_dispatch(influencer, _channel, text):
        sent[influencer["id"]] = text
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    results = asyncio.run(
        pipeline.render_and_dispatch(
            "One product https://amazon.in/dp/B0ABCDEFGH?tag=source-21&ref=tracking",
            influencer_ids=[creator_one, creator_two],
        )
    )

    assert results[creator_one][channel_one] == "posted"
    assert results[creator_two][channel_two] == "posted"
    short_links = {}
    for creator_id, tag in (
        (creator_one, "creator-one-21"),
        (creator_two, "creator-two-21"),
    ):
        match = re.search(
            rf"https://go\.example\.test/amazon/([A-Za-z0-9_-]{{8}})\?tag={re.escape(tag)}",
            sent[creator_id],
        )
        assert match
        record = db.get_amazon_short_link(match.group(1))
        assert record["associate_tag"] == tag
        assert record["target_url"] == f"https://www.amazon.in/dp/B0ABCDEFGH?tag={tag}"
        short_links[creator_id] = (match.group(1), tag, record["target_url"])

    assert short_links[creator_one][0] != short_links[creator_two][0]
    from dashboard.app import app

    client = app.test_client()
    for code, tag, target in short_links.values():
        response = client.get(f"/amazon/{code}?tag={tag}")
        assert response.status_code == 302
        assert response.headers["Location"] == target
        assert client.get(f"/amazon/{code}?tag=wrong-21").status_code == 404


def test_pull_recent_reads_joined_dialogs_without_invite_resolution(monkeypatch):
    class Message:
        def __init__(self, message_id, text):
            self.id = message_id
            self.message = text

    class Entity:
        id = 42
        title = "Already Joined Deals"
        megagroup = True
        username = "joined_deals"
        creator = False

    entity = Entity()

    class Dialog:
        id = 42
        name = "Already Joined Deals"
        is_group = True
        is_channel = False
        is_user = False
        message = Message(3, "latest")

        def __init__(self):
            self.entity = entity

    class FakeClient:
        def __init__(self):
            self.dialogs_called = False
            self.message_entities = []

        def is_connected(self):
            return True

        async def iter_dialogs(self):
            self.dialogs_called = True
            yield Dialog()

        async def iter_messages(self, dialog_entity, limit=None, **kwargs):
            self.message_entities.append(dialog_entity)
            yield Message(3, "latest")
            yield Message(2, "older")

    fake = FakeClient()
    monkeypatch.setattr(telegram_ops, "_client", lambda: fake)
    monkeypatch.setattr(puller, "_source_entries", lambda _use_dummy: [])
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))
    deals = asyncio.run(puller.pull_recent_deals(limit=2, include_source=True))
    assert fake.dialogs_called
    assert fake.message_entities == [entity]
    assert [deal["text"] for deal in deals] == ["older", "latest"]
    assert all(deal["source"] == "Already Joined Deals" for deal in deals)


def test_output_dialog_exclusion_matches_marked_and_raw_telegram_ids(monkeypatch):
    from types import SimpleNamespace

    dialog = SimpleNamespace(
        id=-1004200,
        entity=SimpleNamespace(id=4200, title="Output Channel", megagroup=True, creator=True),
        is_user=False,
        is_bot=False,
        is_group=True,
        is_channel=True,
        name="Output Channel",
    )
    monkeypatch.setattr(config, "TELEGRAM_OUTPUT_CHANNELS", ["4200"])
    monkeypatch.setattr(config, "OPS_TELEGRAM_CHANNEL", "")
    monkeypatch.setattr(db, "list_channels", lambda: [])
    assert puller._joined_source_dialogs([dialog], []) == []


def test_pull_new_deals_uses_cursor_and_skips_unchanged_dialog(monkeypatch):
    class Message:
        def __init__(self, message_id, text):
            self.id = message_id
            self.message = text

    class Entity:
        id = 52
        title = "Cursor Source"
        megagroup = True
        username = "cursor_source"
        creator = False

    entity = Entity()

    class Dialog:
        id = 52
        name = "Cursor Source"
        is_group = True
        is_channel = False
        is_user = False
        message = Message(5, "latest")

        def __init__(self):
            self.entity = entity

    class FakeClient:
        def __init__(self):
            self.calls = []

        def is_connected(self):
            return True

        async def iter_dialogs(self):
            yield Dialog()

        async def iter_messages(self, dialog_entity, **kwargs):
            self.calls.append((dialog_entity, kwargs))
            yield Message(5, "latest")

    fake = FakeClient()
    monkeypatch.setattr(telegram_ops, "_client", lambda: fake)
    monkeypatch.setattr(puller, "_source_entries", lambda _use_dummy: [])
    monkeypatch.setattr(puller, "_excluded_output_keys", lambda: (set(), set()))
    monkeypatch.setattr(db, "get_worker_offset", lambda _key: 4)
    records = asyncio.run(puller.pull_new_deals(limit=10))
    assert records[0]["message_id"] == 5
    assert fake.calls[0][1]["min_id"] == 4
    assert fake.calls[0][1]["reverse"] is True

    monkeypatch.setattr(db, "get_worker_offset", lambda _key: 5)
    fake.calls.clear()
    assert asyncio.run(puller.pull_new_deals(limit=10)) == []
    assert fake.calls == []


def test_worker_holds_cursor_on_delivery_failure_and_retries(monkeypatch):
    records = [
        {"text": "Deal 10", "source": "Retry Source", "source_id": "42", "message_id": 10},
        {"text": "Deal 11", "source": "Retry Source", "source_id": "42", "message_id": 11},
    ]
    offsets = []
    outcomes = iter([
        {1: {2: "failed:temporary Telegram outage"}},
        {1: {2: "posted"}},
        {1: {2: "posted"}},
    ])
    monkeypatch.setattr(worker.puller, "pull_new_deals", AsyncMock(return_value=records))
    monkeypatch.setattr(worker.pipeline, "run_once", AsyncMock(side_effect=lambda _deals: next(outcomes)))
    monkeypatch.setattr(worker.db, "set_worker_offset", lambda key, value: offsets.append((key, value)))

    first = asyncio.run(worker.process_pending_batch())
    assert first["retry_sources"] == 1
    assert first["handled"] == 0
    assert offsets == []

    second = asyncio.run(worker.process_pending_batch())
    assert second["retry_sources"] == 0
    assert second["handled"] == 2
    assert offsets == [("42", 10), ("42", 11)]


def test_telegram_post_resolves_numeric_channel_ids_as_integers(monkeypatch):
    calls = []

    class FakeClient:
        def is_connected(self):
            return True

        async def get_input_entity(self, entity):
            calls.append(("resolve", entity))
            return "peer"

        async def send_message(self, entity, text, buttons=None):
            calls.append(("send", entity, text, buttons))

    monkeypatch.setattr(telegram_ops, "_client", lambda: FakeClient())
    asyncio.run(telegram_ops.post_to_channel("-1004200", "test message"))
    assert calls[0] == ("resolve", -1004200)
    assert calls[1][0:3] == ("send", "peer", "test message")


def test_session_isolation_uses_sqlite_backup_not_shared_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "TELEGRAM_SESSION", "authorized")
    monkeypatch.setattr(config, "TELEGRAM_SESSION_ISOLATION", True)
    monkeypatch.setattr(config, "TELEGRAM_SESSION_SLOT", "release-test")
    base = tmp_path / "influencer_hub" / "authorized.session"
    base.parent.mkdir(parents=True)
    con = sqlite3.connect(base)
    con.execute("CREATE TABLE seed (value TEXT)")
    con.execute("INSERT INTO seed VALUES ('authorized')")
    con.commit()
    con.close()

    loop = asyncio.new_event_loop()
    try:
        isolated = telegram_ops._session_path_for_loop(loop)
        assert isolated != base
        copied = sqlite3.connect(isolated)
        assert copied.execute("SELECT value FROM seed").fetchone()[0] == "authorized"
        copied.close()
    finally:
        telegram_ops._LOOP_SLOTS.pop(loop, None)
        loop.close()


class _FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.body


class _FakeAmazonSession:
    def __init__(self):
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/auth/o2/token"):
            return _FakeResponse({"access_token": "test-token", "expires_in": 3600})
        return _FakeResponse({"itemsResult": {"items": [{"asin": "B0ABCDEFGH"}]}})

    async def close(self):
        self.closed = True


def test_amazon_creators_client_oauth_and_catalog_request(monkeypatch):
    from influencer_hub import amazon_creators

    fake_session = _FakeAmazonSession()
    monkeypatch.setattr(amazon_creators.aiohttp, "ClientSession", lambda **_kwargs: fake_session)
    client = amazon_creators.AmazonCreatorsAPI(
        credential_id="test-id",
        credential_secret="test-secret",
        version="3.2",
        associate_tag="mama086-21",
        timeout=2,
    )
    body = asyncio.run(client.get_items(["B0ABCDEFGH"]))
    assert body["itemsResult"]["items"][0]["asin"] == "B0ABCDEFGH"
    assert len(fake_session.calls) == 2
    token_url, token_call = fake_session.calls[0]
    assert token_url == "https://api.amazon.co.uk/auth/o2/token"
    assert token_call["json"]["scope"] == "creatorsapi::default"
    assert token_call["headers"]["Content-Type"] == "application/json"
    catalog_url, catalog_call = fake_session.calls[1]
    assert catalog_url.endswith("/getItems")
    assert catalog_call["json"]["partnerTag"] == "mama086-21"
    assert catalog_call["headers"]["x-marketplace"] == "www.amazon.in"
    assert catalog_call["headers"]["Authorization"] == "Bearer test-token"
    assert fake_session.closed
