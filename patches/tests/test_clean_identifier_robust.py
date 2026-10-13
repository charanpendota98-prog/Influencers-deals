import pytest
from dashboard.app import clean_identifier

def test_clean_identifier_preserves_invite_links_and_jids():
    assert clean_identifier("https://t.me/+6LA1ljXGlbNmMjA1") == "https://t.me/+6LA1ljXGlbNmMjA1"
    assert clean_identifier("+6LA1ljXGlbNmMjA1") == "https://t.me/+6LA1ljXGlbNmMjA1"
    assert clean_identifier("https://t.me/joinchat/AbCdEf") == "https://t.me/joinchat/AbCdEf"
    assert clean_identifier("https://chat.whatsapp.com/123456789") == "https://chat.whatsapp.com/123456789"
    assert clean_identifier("1203630001@g.us") == "1203630001@g.us"
    assert clean_identifier("https://t.me/mychannel") == "@mychannel"
    assert clean_identifier("t.me/mychannel") == "@mychannel"
    assert clean_identifier("mychannel") == "@mychannel"
