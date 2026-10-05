import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
import app.db as db_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()

    with patch("app.hardware.servo_driver.init"):
        from app.main import app
        with TestClient(app, follow_redirects=True) as c:
            resp = c.post("/login", data={"username": "admin", "password": "admin"})
            assert resp.status_code == 200, f"Admin login failed in fixture: {resp.status_code}"
            yield c
