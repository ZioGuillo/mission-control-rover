from unittest.mock import patch
import app.db as db_module
from fastapi.testclient import TestClient


def _unauthenticated_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "sec_test.db")
    db_module.init_db()
    with patch("app.hardware.servo_driver.init"):
        from app.main import app
        return TestClient(app, follow_redirects=False)


def test_unauthenticated_request_redirects_to_login(tmp_path, monkeypatch):
    c = _unauthenticated_client(tmp_path, monkeypatch)
    r = c.get("/api/status")
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_authenticated_admin_can_access_api(client):
    r = client.get("/api/status")
    assert r.status_code == 200


def test_non_admin_cannot_access_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "nonadmin.db")
    db_module.init_db()
    user = db_module.upsert_github_user(42, "regularuser", "")
    db_module.set_user_role(user["id"], "approved")

    with patch("app.hardware.servo_driver.init"):
        from app.main import app
        with TestClient(app, follow_redirects=False) as c:
            with patch("app.main.db.get_session_user", return_value=user):
                r = c.get("/settings")
                assert r.status_code in (302, 403)


def test_rate_limit_blocks_excessive_motor_commands(client):
    from app.security import rate_limiter
    rate_limiter._windows.clear()

    with patch("app.main.settings.motor_rate_limit", 3), \
         patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors"):
        responses = [
            client.post("/api/motors/forward", json={"speed": 0.5})
            for _ in range(6)
        ]
    assert 200 in [r.status_code for r in responses]
    assert 429 in [r.status_code for r in responses]


def test_rate_limit_disabled_when_zero(client):
    with patch("app.main.settings.motor_rate_limit", 0), \
         patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors"):
        responses = [
            client.post("/api/motors/forward", json={"speed": 0.5})
            for _ in range(10)
        ]
    assert all(r.status_code == 200 for r in responses)


def test_status_endpoint_not_rate_limited(client):
    with patch("app.main.settings.motor_rate_limit", 2):
        responses = [client.get("/api/status") for _ in range(10)]
    assert all(r.status_code == 200 for r in responses)


def test_revoked_user_session_denied_by_role_check(tmp_path, monkeypatch):
    """A user whose role changed to 'revoked' must lose access even if their
    session row (checked only by db.get_session_user) is otherwise still valid."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "revoked_test.db")
    db_module.init_db()
    user = db_module.upsert_github_user(201, "revokedguy", "")
    db_module.set_user_role(user["id"], "revoked")

    with patch("app.hardware.servo_driver.init"):
        from app.main import app
        with TestClient(app, follow_redirects=False) as c:
            with patch("app.main.db.get_session_user", return_value=user):
                r = c.get("/api/status")
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_pending_user_session_denied_by_role_check(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "pending_test.db")
    db_module.init_db()
    user = db_module.upsert_github_user(202, "pendingguy", "")  # defaults to 'pending'

    with patch("app.hardware.servo_driver.init"):
        from app.main import app
        with TestClient(app, follow_redirects=False) as c:
            with patch("app.main.db.get_session_user", return_value=user):
                r = c.get("/api/status")
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_revoking_user_kills_their_live_session_end_to_end(client, tmp_path, monkeypatch):
    """Full integration test: an approved user logs in for real, an admin
    revokes them through /settings, and their next request — with the same
    still-unexpired session cookie — must be bounced to /login."""
    with db_module.get_conn() as conn:
        now = "2025-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'approved', ?)",
            ("bob", db_module.hash_password("bobpass123"), now),
        )
    bob_id = db_module.get_user_by_username("bob")["id"]

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, follow_redirects=False) as bob_client:
        login_resp = bob_client.post("/login", data={"username": "bob", "password": "bobpass123"})
        assert login_resp.status_code == 302
        assert login_resp.headers["location"] == "/"

        r = bob_client.get("/api/status")
        assert r.status_code == 200

        revoke_resp = client.post(f"/settings/users/{bob_id}/revoke")
        assert revoke_resp.status_code == 200

        r2 = bob_client.get("/api/status")
        assert r2.status_code == 302
        assert "/login" in r2.headers["location"]
