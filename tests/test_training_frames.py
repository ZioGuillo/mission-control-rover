"""Saving camera frames for retraining the Lego detector."""
import io
import zipfile
from unittest.mock import patch

import pytest

from app.config import settings

JPEG = b"\xff\xd8\xff\xdbframe\xff\xd9"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def test_save_frame_writes_the_raw_camera_frame(client, data_dir):
    with patch("app.routes.camera.camera_driver.get_frame", return_value=JPEG):
        r = client.post("/api/camera/training-frame")
    assert r.status_code == 200
    assert r.json()["count"] == 1
    [saved] = (data_dir / "training_frames").glob("*.jpg")
    assert saved.read_bytes() == JPEG  # no detection boxes drawn on it


def test_save_frame_without_camera_returns_503(client, data_dir):
    with patch("app.routes.camera.camera_driver.get_frame", return_value=None):
        assert client.post("/api/camera/training-frame").status_code == 503


def test_count_and_zip_download(client, data_dir):
    assert client.get("/api/camera/training-frames").json() == {"count": 0}
    assert client.get("/api/camera/training-frames.zip").status_code == 404

    with patch("app.routes.camera.camera_driver.get_frame", return_value=JPEG):
        client.post("/api/camera/training-frame")
        client.post("/api/camera/training-frame")
    assert client.get("/api/camera/training-frames").json() == {"count": 2}

    r = client.get("/api/camera/training-frames.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 2 and all(n.endswith(".jpg") for n in names)


def test_training_routes_need_login(data_dir):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, follow_redirects=False) as anon:
        assert anon.post("/api/camera/training-frame").status_code in (302, 401, 403)
        assert anon.get("/api/camera/training-frames.zip").status_code in (302, 401, 403)
