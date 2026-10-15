import pytest
from unittest.mock import patch
import app.db as db_module


def test_settings_accessible_to_admin(client):
    r = client.get("/settings")
    assert r.status_code == 200


def test_approve_user(client):
    user = db_module.upsert_github_user(101, "pendinguser", "")
    assert user["role"] == "pending"
    r = client.post(f"/settings/users/{user['id']}/approve")
    assert r.status_code == 200  # follows redirect to /
    updated = db_module.get_user_by_github_id(101)
    assert updated["role"] == "approved"


def test_revoke_user(client):
    user = db_module.upsert_github_user(102, "approveduser", "")
    db_module.set_user_role(user["id"], "approved")
    r = client.post(f"/settings/users/{user['id']}/revoke")
    assert r.status_code == 200
    updated = db_module.get_user_by_github_id(102)
    assert updated["role"] == "revoked"


def test_save_github_settings_enable(client):
    r = client.post("/settings/github", data={
        "github_enabled": "on",
        "github_client_id": "newclientid",
        "github_client_secret": "newsecret",
    })
    assert r.status_code == 200
    assert db_module.get_setting("github_oauth_enabled") == "true"
    assert db_module.get_setting("github_client_id") == "newclientid"
    assert db_module.get_setting("github_client_secret") == "newsecret"


def test_save_github_settings_disable(client):
    db_module.set_setting("github_oauth_enabled", "true")
    r = client.post("/settings/github", data={
        "github_client_id": "cid",
        "github_client_secret": "",
    })
    assert r.status_code == 200
    assert db_module.get_setting("github_oauth_enabled") == "false"


def test_change_password_wrong_current(client):
    r = client.post("/settings/password", data={
        "current_password": "WrongPassword!",
        "new_password": "NewPass123!",
        "confirm_password": "NewPass123!",
    })
    assert r.status_code == 200
    assert db_module.verify_password("admin", db_module.get_user_by_username("admin")["password_hash"])


def test_change_password_mismatch(client):
    r = client.post("/settings/password", data={
        "current_password": "admin",
        "new_password": "NewPass123!",
        "confirm_password": "DifferentPass!",
    })
    assert r.status_code == 200
    assert db_module.verify_password("admin", db_module.get_user_by_username("admin")["password_hash"])


def test_change_password_too_short(client):
    r = client.post("/settings/password", data={
        "current_password": "admin",
        "new_password": "short",
        "confirm_password": "short",
    })
    assert r.status_code == 200
    assert db_module.verify_password("admin", db_module.get_user_by_username("admin")["password_hash"])


def test_change_password_success(client):
    r = client.post("/settings/password", data={
        "current_password": "admin",
        "new_password": "BrandNewPass999!",
        "confirm_password": "BrandNewPass999!",
    })
    assert r.status_code == 200
    assert db_module.verify_password("BrandNewPass999!", db_module.get_user_by_username("admin")["password_hash"])


def test_minifigures_only_switch_is_saved_and_applied(client):
    from app import db
    from app.hardware import ml_driver

    client.post("/settings/ml", data={"ml_enabled": "on", "ml_lego_only": "on"})
    assert db.get_setting("ml_lego_only") == "true"
    if ml_driver.available:
        assert ml_driver.lego_only is True
    client.post("/settings/ml", data={"ml_enabled": "on"})  # unticked = off
    assert db.get_setting("ml_lego_only") == "false"
    ml_driver.set_lego_only(False)
