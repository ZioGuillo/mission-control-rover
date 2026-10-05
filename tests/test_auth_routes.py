from urllib.parse import urlparse, parse_qs

import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
import app.db as db_module


def _start_oauth_and_get_state(client):
    """Drive /auth/github so the client picks up a valid oauth_state cookie,
    and return that state value for use on /auth/callback."""
    r = client.get("/auth/github")
    location = r.headers["location"]
    return parse_qs(urlparse(location).query)["state"][0]


@pytest.fixture()
def fresh_client(tmp_path, monkeypatch):
    """Unauthenticated client with a fresh DB — for testing login itself."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "auth_test.db")
    db_module.init_db()
    with patch("app.hardware.servo_driver.init"):
        from app.main import app
        with TestClient(app, follow_redirects=False) as c:
            yield c


def test_login_page_renders(fresh_client):
    r = fresh_client.get("/login")
    assert r.status_code == 200
    assert b"RoboControl" in r.content


def test_login_shows_github_button_when_enabled(fresh_client):
    db_module.set_setting("github_oauth_enabled", "true")
    db_module.set_setting("github_client_id", "test_client_id")
    r = fresh_client.get("/login")
    assert b"Continue with GitHub" in r.content


def test_login_hides_github_button_when_disabled(fresh_client):
    db_module.set_setting("github_oauth_enabled", "false")
    r = fresh_client.get("/login")
    assert b"Continue with GitHub" not in r.content


def test_login_success_redirects_to_root(fresh_client):
    r = fresh_client.post("/login", data={"username": "admin", "password": "admin"})
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_login_wrong_password_redirects_with_invalid_status(fresh_client):
    r = fresh_client.post("/login", data={"username": "admin", "password": "wrongpass"})
    assert r.status_code == 302
    assert "status=invalid" in r.headers["location"]


def test_login_wrong_username_redirects_with_invalid_status(fresh_client):
    r = fresh_client.post("/login", data={"username": "nobody", "password": "anything"})
    assert r.status_code == 302
    assert "status=invalid" in r.headers["location"]


def test_logout_clears_session(fresh_client):
    # Log in first
    fresh_client.post("/login", data={"username": "admin", "password": "admin"})
    # Then log out
    r = fresh_client.post("/auth/logout")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    # Subsequent request redirects to login
    r2 = fresh_client.get("/api/status")
    assert r2.status_code == 302


def test_github_redirect_returns_302_to_github_when_enabled(fresh_client):
    db_module.set_setting("github_oauth_enabled", "true")
    db_module.set_setting("github_client_id", "my_client_id")
    r = fresh_client.get("/auth/github")
    assert r.status_code == 302
    assert "github.com/login/oauth/authorize" in r.headers["location"]
    assert "my_client_id" in r.headers["location"]


def test_github_redirect_goes_to_login_when_disabled(fresh_client):
    db_module.set_setting("github_oauth_enabled", "false")
    r = fresh_client.get("/auth/github")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_github_callback_error_param_redirects_to_login_error(fresh_client):
    r = fresh_client.get("/auth/callback?error=access_denied")
    assert r.status_code == 302
    assert "status=error" in r.headers["location"]


def test_github_callback_pending_user_redirects_to_pending(fresh_client):
    db_module.set_setting("github_oauth_enabled", "true")
    db_module.set_setting("github_client_id", "cid")
    db_module.set_setting("github_client_secret", "csecret")
    state = _start_oauth_and_get_state(fresh_client)

    with patch("app.routes.auth.auth.exchange_github_code", new=AsyncMock(return_value="token123")), \
         patch("app.routes.auth.auth.get_github_profile", new=AsyncMock(return_value={
             "id": 777, "login": "newuser", "avatar_url": ""
         })):
        r = fresh_client.get(f"/auth/callback?code=validcode&state={state}")
    assert r.status_code == 302
    assert "status=pending" in r.headers["location"]


def test_github_callback_approved_user_redirects_to_root(fresh_client):
    db_module.set_setting("github_oauth_enabled", "true")
    db_module.set_setting("github_client_id", "cid")
    db_module.set_setting("github_client_secret", "csecret")
    user = db_module.upsert_github_user(888, "approved_user", "")
    db_module.set_user_role(user["id"], "approved")
    state = _start_oauth_and_get_state(fresh_client)

    with patch("app.routes.auth.auth.exchange_github_code", new=AsyncMock(return_value="token123")), \
         patch("app.routes.auth.auth.get_github_profile", new=AsyncMock(return_value={
             "id": 888, "login": "approved_user", "avatar_url": ""
         })):
        r = fresh_client.get(f"/auth/callback?code=validcode&state={state}")
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_github_callback_rejects_missing_state(fresh_client):
    db_module.set_setting("github_oauth_enabled", "true")
    db_module.set_setting("github_client_id", "cid")
    db_module.set_setting("github_client_secret", "csecret")
    _start_oauth_and_get_state(fresh_client)  # seeds a valid state in the session

    with patch("app.routes.auth.auth.exchange_github_code", new=AsyncMock(return_value="token123")), \
         patch("app.routes.auth.auth.get_github_profile", new=AsyncMock(return_value={
             "id": 999, "login": "attacker_linked", "avatar_url": ""
         })):
        r = fresh_client.get("/auth/callback?code=validcode")
    assert r.status_code == 302
    assert "status=error" in r.headers["location"]


def test_github_callback_rejects_mismatched_state(fresh_client):
    db_module.set_setting("github_oauth_enabled", "true")
    db_module.set_setting("github_client_id", "cid")
    db_module.set_setting("github_client_secret", "csecret")
    _start_oauth_and_get_state(fresh_client)  # seeds a valid state in the session

    with patch("app.routes.auth.auth.exchange_github_code", new=AsyncMock(return_value="token123")), \
         patch("app.routes.auth.auth.get_github_profile", new=AsyncMock(return_value={
             "id": 999, "login": "attacker_linked", "avatar_url": ""
         })):
        r = fresh_client.get("/auth/callback?code=validcode&state=not-the-real-state")
    assert r.status_code == 302
    assert "status=error" in r.headers["location"]


def test_github_callback_rejects_replayed_state(fresh_client):
    """A state token must not be usable twice (protects against callback replay)."""
    db_module.set_setting("github_oauth_enabled", "true")
    db_module.set_setting("github_client_id", "cid")
    db_module.set_setting("github_client_secret", "csecret")
    state = _start_oauth_and_get_state(fresh_client)

    with patch("app.routes.auth.auth.exchange_github_code", new=AsyncMock(return_value="token123")), \
         patch("app.routes.auth.auth.get_github_profile", new=AsyncMock(return_value={
             "id": 777, "login": "newuser", "avatar_url": ""
         })):
        first = fresh_client.get(f"/auth/callback?code=validcode&state={state}")
        second = fresh_client.get(f"/auth/callback?code=validcode&state={state}")
    assert first.status_code == 302
    assert "status=pending" in first.headers["location"]
    assert "status=error" in second.headers["location"]
