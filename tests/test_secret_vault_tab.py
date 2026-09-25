import pytest
from dashboard.app import app
from influencer_hub import db, config

def test_secret_vault_tab_access_and_lock():
    db.init()
    db.migrate()
    client = app.test_client()

    # 1. Visiting /admin/secret-vault when locked shows simple password form
    res = client.get("/admin/secret-vault")
    assert res.status_code == 200
    assert b"Password Required" in res.data
    assert b"Shared Telegram Source Channels Pool" not in res.data

    # 2. Submitting WRONG password fails and keeps page locked
    res_fail = client.post("/admin/secret-vault", data={"admin_password": "wrong"}, follow_redirects=True)
    assert res_fail.status_code == 200
    assert b"Invalid Admin Password" in res_fail.data
    assert b"Shared Telegram Source Channels Pool" not in res_fail.data

    # 3. Submitting CORRECT password unlocks secret tab immediately
    res_unlock = client.post("/admin/secret-vault", data={"admin_password": config.ADMIN_DELETE_PASSWORD}, follow_redirects=True)
    assert res_unlock.status_code == 200
    assert b"Central Affiliate Credentials Vault" in res_unlock.data
    assert b"Shared Telegram Source Channels Pool" in res_unlock.data

    # 4. Locking the vault returns to password gate
    res_lock = client.post("/admin/secret-vault/lock", follow_redirects=True)
    assert res_lock.status_code == 200
    assert b"Password Required" in res_lock.data
    assert b"Shared Telegram Source Channels Pool" not in res_lock.data
