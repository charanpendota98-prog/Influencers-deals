import pytest

from influencer_hub import earnkaro as ek


def test_parse_string_link():
    body = '{"success": 1, "data": "https://ekaro.in/AbC123"}'
    assert ek.parse_ek_response(body) == "https://ekaro.in/AbC123"


def test_parse_list_link():
    body = '{"success": 1, "data": ["https://ekaro.in/xYz", "https://other.example/y"]}'
    assert ek.parse_ek_response(body) == "https://ekaro.in/xYz"


def test_parse_failure_flag():
    body = '{"success": 0, "data": "https://ekaro.in/nope"}'
    assert ek.parse_ek_response(body) is None


def test_parse_non_http_ignored():
    body = '{"success": 1, "data": "not-a-url"}'
    assert ek.parse_ek_response(body) is None


def test_parse_garbage():
    assert ek.parse_ek_response("<<not json>>") is None


def test_parse_rejects_wrong_publisher():
    # A link that earns for a DIFFERENT account (5478322 vs 999) must be rejected.
    body = ('{"success": 1, "data": '
            '"https://www.flipkart.com/p/itm?pid=X&affid=someone&affExtParam2=999"}')
    assert ek.parse_ek_response(body, expected_pubid="5478322") is None


def test_parse_accepts_our_publisher():
    body = ('{"success": 1, "data": '
            '"https://www.flipkart.com/p/itm?pid=X&affid=rohanpouri&affExtParam1=Y&affExtParam2=5478322"}')
    assert ek.parse_ek_response(body, expected_pubid="5478322") == (
        "https://www.flipkart.com/p/itm?pid=X&affid=rohanpouri&affExtParam1=Y&affExtParam2=5478322")


def test_parse_accepts_short_link_without_pubid_param():
    # ekaro.in short links don't carry affExtParam2; accept them.
    body = '{"success": 1, "data": "https://ekaro.in/AbC123"}'
    assert ek.parse_ek_response(body, expected_pubid="5478322") == "https://ekaro.in/AbC123"


def test_clean_normalises():
    # parse_ek_response normalises the host case; query is preserved as-is.
    body = '{"success": 1, "data": "https://EKARO.IN/AbC?utm_source=x"}'
    assert ek.parse_ek_response(body) == "https://ekaro.in/AbC?utm_source=x"
