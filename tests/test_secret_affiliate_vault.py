import pytest
from dashboard.app import app
from influencer_hub import db, config, pipeline


def test_secret_affiliate_vault_password_protection():
    db.init()
    db.migrate()
    client = app.test_client()

    # 1. Attempt to update without correct password -> Should reject!
    res_wrong = client.post("/admin/affiliate-vault/update", data={
        "admin_password": "wrong_password",
        "hypd_store_id": "77777",
        "earnkaro_publisher_id": "999999",
        "earnkaro_api_key": "new_fake_jwt_token"
    }, follow_redirects=True)

    assert res_wrong.status_code == 200
    assert b"Invalid Admin Password" in res_wrong.data
    # Verify settings were NOT updated
    assert db.get_global_setting("hypd_store_id") != "77777"

    # 2. Update WITH correct password -> Should succeed!
    res_correct = client.post("/admin/affiliate-vault/update", data={
        "admin_password": config.ADMIN_DELETE_PASSWORD,
        "hypd_store_id": "77777",
        "earnkaro_publisher_id": "999999",
        "earnkaro_api_key": "new_fake_jwt_token"
    }, follow_redirects=True)

    assert res_correct.status_code == 200
    assert b"Affiliate Credentials Updated Successfully" in res_correct.data
    assert db.get_global_setting("hypd_store_id") == "77777"
    assert db.get_global_setting("earnkaro_publisher_id") == "999999"
    assert db.get_global_setting("earnkaro_api_key") == "new_fake_jwt_token"

    # Reset back to default for clean state
    db.set_global_setting("hypd_store_id", "93944")
    db.set_global_setting("earnkaro_publisher_id", "5478322")
