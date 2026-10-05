"""
SIMULATE=true is decided at import time (module-level, like every other
app/hardware/* backend selection), so it can't be toggled on an
already-imported module within this test process — these tests spawn a
fresh interpreter with the env var set, matching how docker/Dockerfile
actually runs the app.
"""
import json
import subprocess
import sys
import tempfile


def _run_in_simulate_mode(snippet: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        script = f"""
import os
os.environ["SIMULATE"] = "true"
os.environ["DATA_DIR"] = {tmp!r}
import json
{snippet}
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])


def test_rrb3_driver_simulates_motors_and_distance():
    out = _run_in_simulate_mode("""
from app.hardware import rrb3_driver as driver
driver.set_motors(0.8, 1, 0.8, 0)  # must not raise
dist = driver.get_distance()
print(json.dumps({
    "available": driver.available,
    "simulated": driver.simulated,
    "distance_in_range": 15.0 <= dist <= 150.0,
}))
""")
    assert out == {"available": True, "simulated": True, "distance_in_range": True}


def test_servo_driver_simulates_after_init():
    out = _run_in_simulate_mode("""
from app.hardware import servo_driver as driver
driver.init(12, 13)
result = driver.move(15, 0)
print(json.dumps({
    "available": driver.available,
    "simulated": driver.simulated,
    "pan": result["pan"],
}))
""")
    assert out == {"available": True, "simulated": True, "pan": 105}


def test_camera_driver_simulates_frames():
    # Real camera hardware is tried first regardless of SIMULATE (so a
    # camera plugged into a SIMULATE=true session isn't ignored) — the
    # synthetic "simulated" backend is only the fallback when no real
    # camera is found, so a dev machine with a webcam legitimately gets
    # "opencv"/"picamera2" here instead. The contract under test is that
    # SIMULATE=true always yields *some* working camera backend, not
    # specifically the synthetic one.
    out = _run_in_simulate_mode("""
from app.hardware import camera_driver as driver
import time
driver.start()
time.sleep(0.3)
driver.stop()
frame, counter = driver.get_frame_if_new(-1)
print(json.dumps({
    "available": driver.available,
    "backend": driver._backend,
    "got_a_frame": frame is not None and len(frame) > 0,
    "counter": counter,
}))
""")
    assert out["available"] is True
    assert out["backend"] in ("simulated", "opencv", "picamera2")
    assert out["got_a_frame"] is True
    assert out["counter"] > 0
