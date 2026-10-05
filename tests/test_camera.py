from unittest.mock import patch


def test_up_action(client):
    with patch("app.routes.camera.driver.available", True), \
         patch("app.routes.camera.driver.move", return_value={"pan": 90, "tilt": 105}) as mock:
        r = client.post("/api/camera/up")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock.assert_called_once_with(0, 15)


def test_down_action(client):
    with patch("app.routes.camera.driver.available", True), \
         patch("app.routes.camera.driver.move", return_value={"pan": 90, "tilt": 75}) as mock:
        r = client.post("/api/camera/down")
        assert r.status_code == 200
        mock.assert_called_once_with(0, -15)


def test_left_action(client):
    with patch("app.routes.camera.driver.available", True), \
         patch("app.routes.camera.driver.move", return_value={"pan": 75, "tilt": 90}) as mock:
        r = client.post("/api/camera/left")
        assert r.status_code == 200
        mock.assert_called_once_with(-15, 0)


def test_center_action(client):
    with patch("app.routes.camera.driver.available", True), \
         patch("app.routes.camera.driver.center", return_value={"pan": 90, "tilt": 90}) as mock:
        r = client.post("/api/camera/center")
        assert r.status_code == 200
        mock.assert_called_once()


def test_unknown_action_returns_400(client):
    with patch("app.routes.camera.driver.available", True):
        r = client.post("/api/camera/spin")
        assert r.status_code == 400


def test_unavailable_returns_503(client):
    with patch("app.routes.camera.driver.available", False):
        r = client.post("/api/camera/up")
        assert r.status_code == 503
