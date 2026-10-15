"""Detection boxes are drawn by the browser from /api/camera/detections —
the MJPEG stream itself carries the camera frames untouched."""
from unittest.mock import patch

DETS = [{"label": "lego_minifigure", "score": 0.91, "box": [0.7, 0.4, 0.8, 0.5]}]


def test_detections_returned_while_detection_is_on(client):
    with patch("app.routes.camera.ml_driver.available", True), \
         patch("app.routes.camera.ml_driver.enabled", True), \
         patch("app.routes.camera.ml_driver.get_detections", return_value=DETS):
        assert client.get("/api/camera/detections").json() == {"enabled": True, "detections": DETS}


def test_no_stale_boxes_once_detection_is_switched_off(client):
    with patch("app.routes.camera.ml_driver.available", True), \
         patch("app.routes.camera.ml_driver.enabled", False), \
         patch("app.routes.camera.ml_driver.get_detections", return_value=DETS):
        assert client.get("/api/camera/detections").json() == {"enabled": False, "detections": []}


def test_detections_need_login():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/api/camera/detections").status_code in (302, 401, 403)


def test_stream_no_longer_draws_on_frames():
    import app.routes.camera as cam

    assert not hasattr(cam, "_annotate")
