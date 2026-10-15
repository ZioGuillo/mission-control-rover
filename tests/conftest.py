import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
import app.db as db_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()

    # The app's startup would start the real camera loop — on a Mac that
    # opens the built-in webcam from a background thread, which crashes
    # Python (segfault) a few tests later. Camera tests use fakes instead.
    with patch("app.hardware.servo_driver.init"), \
         patch("app.hardware.camera_driver.start"):
        from app.main import app
        with TestClient(app, follow_redirects=True) as c:
            resp = c.post("/login", data={"username": "admin", "password": "admin"})
            assert resp.status_code == 200, f"Admin login failed in fixture: {resp.status_code}"
            yield c
