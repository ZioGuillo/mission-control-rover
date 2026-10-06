import time
from unittest.mock import patch

from app.routes import motors

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
    # Sonar reports blocked on every read — including the re-checks inside
    # the evasion retry loop — so the obstacle never actually clears. With
    # the retry-until-clear logic, that's a stable "blocked" steady state
    # (not the old single-blind-turn "avoiding"), reached deterministically
    # regardless of how many outer-loop ticks race through during the wait.
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=15.0), \
         patch("app.routes.motors.driver.set_motors"), \
         patch("app.routes.motors.settings.obstacle_threshold_cm", 20.0), \
         patch("app.routes.motors.time.sleep"):
        client.post("/api/motors/auto/start")
        time.sleep(0.05)

        status = client.get("/api/motors/auto/status").json()
        assert status["action"] == "blocked"
        assert status["distance_cm"] == 15.0

        client.post("/api/motors/auto/stop")


def test_auto_drive_avoids_via_ml_detection_with_clear_sonar(client):
    # Sonar's cone is narrower than the camera's view — ML can see an
    # obstacle sonar doesn't. A large, centered detection should trigger
    # evasion on its own, even with sonar reporting a clear path.
    big_centered_box = {"label": "person", "score": 0.9, "box": [0.1, 0.3, 0.9, 0.7]}
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors"), \
         patch("app.routes.motors.ml_driver.get_detections", return_value=[big_centered_box]), \
         patch("app.routes.motors.time.sleep"):
        client.post("/api/motors/auto/start")
        time.sleep(0.05)

        status = client.get("/api/motors/auto/status").json()
        assert status["action"] == "avoiding"

        client.post("/api/motors/auto/stop")


def test_auto_drive_ignores_tiny_ml_detection(client):
    tiny_box = {"label": "cup", "score": 0.9, "box": [0.48, 0.48, 0.52, 0.52]}
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors"), \
         patch("app.routes.motors.ml_driver.get_detections", return_value=[tiny_box]), \
         patch("app.routes.motors.time.sleep"):
        client.post("/api/motors/auto/start")
        time.sleep(0.05)

        status = client.get("/api/motors/auto/status").json()
        assert status["action"] == "forward"

        client.post("/api/motors/auto/stop")


def test_ml_obstacle_direction_none_when_no_detections():
    assert motors._ml_obstacle_direction([]) is None


def test_ml_obstacle_direction_ignores_small_detection():
    small = {"label": "chair", "score": 0.9, "box": [0.45, 0.45, 0.55, 0.55]}
    assert motors._ml_obstacle_direction([small]) is None


def test_ml_obstacle_direction_ignores_large_off_center_detection():
    # Large enough to matter, but hugging the left edge — not blocking the
    # forward path, so this should not count as "in the way".
    off_center = {"label": "person", "score": 0.9, "box": [0.0, 0.0, 1.0, 0.3]}
    assert motors._ml_obstacle_direction([off_center]) is None


def test_ml_obstacle_direction_returns_right_for_obstacle_on_left():
    left_box = {"label": "person", "score": 0.9, "box": [0.1, 0.1, 0.9, 0.5]}
    assert motors._ml_obstacle_direction([left_box]) == "right"


def test_ml_obstacle_direction_returns_left_for_obstacle_on_right():
    right_box = {"label": "person", "score": 0.9, "box": [0.1, 0.5, 0.9, 0.9]}
    assert motors._ml_obstacle_direction([right_box]) == "left"


def test_evade_until_clear_returns_true_when_path_clears_within_attempts():
    with patch("app.routes.motors.driver.get_distance", side_effect=[15.0, 100.0]), \
         patch("app.routes.motors.driver.set_motors") as mock_set, \
         patch("app.routes.motors.settings.obstacle_threshold_cm", 20.0), \
         patch("app.routes.motors.time.sleep"):
        result = motors._evade_until_clear("right", 0.8)

    assert result is True
    turn_commands = [c.args for c in mock_set.call_args_list if c.args != (0, 0, 0, 0)]
    assert turn_commands == [(0.4, motors._M1_FWD, 0.4, motors._M2_REV)] * 2


def test_evade_until_clear_returns_false_after_max_attempts():
    with patch("app.routes.motors.driver.get_distance", return_value=15.0) as mock_dist, \
         patch("app.routes.motors.driver.set_motors"), \
         patch("app.routes.motors.settings.obstacle_threshold_cm", 20.0), \
         patch("app.routes.motors.time.sleep"):
        result = motors._evade_until_clear("left", 0.8)

    assert result is False
    assert mock_dist.call_count == motors._AUTO_MAX_EVADE_ATTEMPTS


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
