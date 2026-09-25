import pytest
from dashboard.app import app
from influencer_hub import db

def test_toggle_influencer_active_and_channel_status():
    db.init()
    db.migrate()
    client = app.test_client()

    inf_id = db.add_influencer("ToggleTester", "toggle-21")
    ch_id = db.add_channel(inf_id, "telegram", "@test_chan", role="broadcast")
    db.update_channel_details(ch_id, status="ready")

    # 1. Influencer initially active=1
    inf = db.get_influencer(inf_id)
    assert inf["active"] == 1

    # 2. Toggle influencer active -> becomes 0 (Total OFF)
    res = client.post(f"/influencer/{inf_id}/toggle-active", follow_redirects=True)
    assert res.status_code == 200
    inf = db.get_influencer(inf_id)
    assert inf["active"] == 0
    assert b"TOTAL OFF (PAUSED)" in res.data or b"Master Switch: TOTAL OFF" in res.data

    # 3. Toggle back -> becomes 1 (Active ON)
    res2 = client.post(f"/influencer/{inf_id}/toggle-active", follow_redirects=True)
    assert res2.status_code == 200
    inf = db.get_influencer(inf_id)
    assert inf["active"] == 1

    # 4. Toggle channel status
    res3 = client.post(f"/channel/{ch_id}/toggle-status", data={"inf_id": inf_id}, follow_redirects=True)
    assert res3.status_code == 200
    channels = db.list_channels(inf_id)
    ch = next(c for c in channels if c["id"] == ch_id)
    assert ch["status"] == "paused"
