import pytest
from pathlib import Path
import app.db as db_module


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()
    return db_module


def test_init_db_creates_tables(db):
    with db.get_conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert {"users", "sessions", "app_settings"} <= tables


def test_admin_seeded_on_first_init(db):
    admin = db.get_user_by_username("admin")
    assert admin is not None
    assert admin["role"] == "admin"
    assert admin["password_hash"] is not None


def test_admin_seeded_with_must_change_password_on_truly_fresh_db(db):
    """Regression test: the admin-seed INSERT used to reference
    must_change_password before the column-migration ALTER TABLE ran, which
    raised sqlite3.OperationalError on every first-ever install (an empty
    users table with no pre-existing must_change_password column)."""
    admin = db.get_user_by_username("admin")
    assert admin["must_change_password"] == 1


def test_init_db_idempotent(db):
    db.init_db()  # second call should not duplicate admin
    with db.get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
    assert count == 1


def test_verify_password_correct(db):
    admin = db.get_user_by_username("admin")
    assert db.verify_password("admin", admin["password_hash"])


def test_verify_password_wrong(db):
    admin = db.get_user_by_username("admin")
    assert not db.verify_password("wrongpass", admin["password_hash"])


def test_create_and_get_session(db):
    admin = db.get_user_by_username("admin")
    token = db.create_session(admin["id"])
    user = db.get_session_user(token)
    assert user is not None
    assert user["id"] == admin["id"]


def test_get_session_returns_none_for_unknown_token(db):
    assert db.get_session_user("nonexistent-token") is None


def test_delete_session(db):
    admin = db.get_user_by_username("admin")
    token = db.create_session(admin["id"])
    db.delete_session(token)
    assert db.get_session_user(token) is None


def test_upsert_github_user_creates_pending(db):
    user = db.upsert_github_user(12345, "octocat", "https://example.com/avatar.png")
    assert user["github_id"] == 12345
    assert user["github_login"] == "octocat"
    assert user["role"] == "pending"


def test_upsert_github_user_updates_existing(db):
    db.upsert_github_user(12345, "octocat", "https://example.com/avatar.png")
    user = db.upsert_github_user(12345, "octocat-renamed", "https://example.com/new.png")
    assert user["github_login"] == "octocat-renamed"
    assert user["role"] == "pending"  # role unchanged on update


def test_set_user_role(db):
    user = db.upsert_github_user(99, "testuser", "")
    db.set_user_role(user["id"], "approved")
    updated = db.get_user_by_github_id(99)
    assert updated["role"] == "approved"


def test_get_all_github_users(db):
    db.upsert_github_user(1, "alice", "")
    db.upsert_github_user(2, "bob", "")
    users = db.get_all_github_users()
    logins = {u["github_login"] for u in users}
    assert {"alice", "bob"} <= logins


def test_get_setting_default(db):
    assert db.get_setting("github_oauth_enabled") == "false"


def test_set_and_get_setting(db):
    db.set_setting("github_oauth_enabled", "true")
    assert db.get_setting("github_oauth_enabled") == "true"


def test_update_admin_password(db):
    new_hash = db.hash_password("NewPassword123!")
    db.update_admin_password(new_hash)
    admin = db.get_user_by_username("admin")
    assert db.verify_password("NewPassword123!", admin["password_hash"])
    assert not db.verify_password("admin", admin["password_hash"])


def test_expired_session_is_rejected(db):
    import sqlite3 as _sqlite3
    admin = db.get_user_by_username("admin")
    token = db.create_session(admin["id"])
    # Manually expire the session
    with db.get_conn() as conn:
        conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token = ?", (token,))
    assert db.get_session_user(token) is None


def test_set_user_role_invalid_role_raises(db):
    user = db.upsert_github_user(55, "roletest", "")
    with pytest.raises(ValueError):
        db.set_user_role(user["id"], "superadmin")


def test_revoking_role_invalidates_existing_sessions(db):
    user = db.upsert_github_user(66, "revoketest", "")
    db.set_user_role(user["id"], "approved")
    token = db.create_session(user["id"])
    assert db.get_session_user(token) is not None

    db.set_user_role(user["id"], "revoked")

    assert db.get_session_user(token) is None


def test_setting_role_back_to_pending_invalidates_existing_sessions(db):
    user = db.upsert_github_user(67, "pendingtest", "")
    db.set_user_role(user["id"], "approved")
    token = db.create_session(user["id"])

    db.set_user_role(user["id"], "pending")

    assert db.get_session_user(token) is None


def test_approving_role_does_not_touch_existing_sessions(db):
    admin = db.get_user_by_username("admin")
    token = db.create_session(admin["id"])

    db.set_user_role(admin["id"], "admin")

    assert db.get_session_user(token) is not None
