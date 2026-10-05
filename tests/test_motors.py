import time
from unittest.mock import patch

# M1: dir=1=fwd, dir=0=rev  |  M2: dir=0=fwd, dir=1=rev  (M2 wired reversed)


def test_forward(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors") as mock:
        r = client.post("/api/motors/forward", json={"speed": 0.8})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "action": "forward"}
        mock.assert_called_once_with(0.8, 1, 0.8, 0)


def test_forward_blocked_by_obstacle(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=15.0), \
         patch("app.routes.motors.driver.set_motors") as mock_set, \
         patch("app.routes.motors.settings.obstacle_threshold_cm", 20.0), \
         patch("app.routes.motors.asyncio.sleep"):
        r = client.post("/api/motors/forward", json={"speed": 0.75})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert data["blocked"] is True
        assert "15" in data["message"]
        assert "obstacle" in data["message"].lower()
        calls = mock_set.call_args_list
        assert calls[0].args == (0, 0, 0, 0)
        assert calls[-1].args == (0, 0, 0, 0)


def test_forward_clears_path(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=100.0), \
         patch("app.routes.motors.settings.obstacle_threshold_cm", 20.0), \
         patch("app.routes.motors.driver.set_motors") as mock:
        r = client.post("/api/motors/forward", json={"speed": 0.75})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock.assert_called_once_with(0.75, 1, 0.75, 0)


def test_reverse(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.set_motors") as mock:
        r = client.post("/api/motors/reverse", json={"speed": 0.5})
        assert r.status_code == 200
        mock.assert_called_once_with(0.5, 0, 0.5, 1)


def test_left(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.set_motors") as mock:
        r = client.post("/api/motors/left", json={"speed": 0.6})
        assert r.status_code == 200
        mock.assert_called_once_with(0.3, 0, 0.3, 0)


def test_right(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.set_motors") as mock:
        r = client.post("/api/motors/right", json={"speed": 0.6})
        assert r.status_code == 200
        mock.assert_called_once_with(0.3, 1, 0.3, 1)


def test_stop(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.set_motors") as mock:
        r = client.post("/api/motors/stop")
        assert r.status_code == 200
        mock.assert_called_once_with(0, 0, 0, 0)


def test_auto_start_requires_availability(client):
    with patch("app.routes.motors.driver.available", False):
        r = client.post("/api/motors/auto/start")
        assert r.status_code == 503


def test_auto_stop_when_not_running_is_noop(client):
    r = client.post("/api/motors/auto/stop")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "running": False}


def test_auto_drive_forward_then_stop(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors") as mock_set, \
         patch("app.routes.motors.time.sleep"):
        r = client.post("/api/motors/auto/start")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "running": True}

        time.sleep(0.05)  # let the background thread run at least one tick
        status = client.get("/api/motors/auto/status").json()
        assert status["running"] is True
        assert status["action"] == "forward"

        r2 = client.post("/api/motors/auto/stop")
        assert r2.json() == {"ok": True, "running": False}

        status2 = client.get("/api/motors/auto/status").json()
        assert status2 == {"running": False, "action": None, "distance_cm": None}

        # forward motion was commanded, and the final command was a full stop
        calls = [c.args for c in mock_set.call_args_list]
        assert (0.75, 1, 0.75, 0) in calls
        assert calls[-1] == (0, 0, 0, 0)


def test_auto_drive_avoids_obstacle(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=15.0), \
         patch("app.routes.motors.driver.set_motors"), \
         patch("app.routes.motors.settings.obstacle_threshold_cm", 20.0), \
         patch("app.routes.motors.time.sleep"):
        client.post("/api/motors/auto/start")
        time.sleep(0.05)

        status = client.get("/api/motors/auto/status").json()
        assert status["action"] == "avoiding"
        assert status["distance_cm"] == 15.0

        client.post("/api/motors/auto/stop")


def test_auto_start_twice_is_idempotent(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors"), \
         patch("app.routes.motors.time.sleep"):
        r1 = client.post("/api/motors/auto/start")
        r2 = client.post("/api/motors/auto/start")
        assert r1.json() == r2.json() == {"ok": True, "running": True}
        client.post("/api/motors/auto/stop")


def test_manual_command_stops_auto_drive(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors"), \
         patch("app.routes.motors.time.sleep"):
        client.post("/api/motors/auto/start")
        time.sleep(0.05)
        assert client.get("/api/motors/auto/status").json()["running"] is True

        r = client.post("/api/motors/stop")
        assert r.status_code == 200

        assert client.get("/api/motors/auto/status").json()["running"] is False


def test_unknown_action_returns_400(client):
    with patch("app.routes.motors.driver.available", True):
        r = client.post("/api/motors/dance")
        assert r.status_code == 400


def test_motor_unavailable_returns_503(client):
    with patch("app.routes.motors.driver.available", False):
        r = client.post("/api/motors/forward")
        assert r.status_code == 503


def test_speed_defaults_to_config(client):
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors") as mock, \
         patch("app.routes.motors.settings.motor_speed_default", 0.75):
        r = client.post("/api/motors/forward")
        assert r.status_code == 200
        mock.assert_called_once_with(0.75, 1, 0.75, 0)
