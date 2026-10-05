import asyncio
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.hardware import rrb3_driver as driver
from app.config import settings
from app import telemetry, metrics as m

router = APIRouter(prefix="/api/motors")

VALID_ACTIONS = {"forward", "reverse", "left", "right", "stop"}

# M1 (left): dir=1=forward, dir=0=reverse
# M2 (right): dir=0=forward, dir=1=reverse  (wired with reversed polarity)
_M1_FWD, _M1_REV = 1, 0
_M2_FWD, _M2_REV = 0, 1

_AUTO_TICK_SECONDS = 0.15
_AUTO_AVOID_TURN_SECONDS = 0.5

_auto_lock = threading.Lock()
_auto_running = False
_auto_status: dict = {"action": None, "distance_cm": None}
_auto_thread: threading.Thread | None = None


class MotorRequest(BaseModel):
    speed: float | None = Field(default=None, ge=0.0, le=1.0)


def _auto_loop() -> None:
    global _auto_running
    speed = settings.motor_speed_default
    try:
        while True:
            with _auto_lock:
                if not _auto_running:
                    break
            dist = driver.get_distance()
            if 0 < dist < settings.obstacle_threshold_cm:
                driver.set_motors(0, 0, 0, 0)
                driver.set_motors(speed / 2, _M1_FWD, speed / 2, _M2_REV)  # turn right to evade
                time.sleep(_AUTO_AVOID_TURN_SECONDS)
                driver.set_motors(0, 0, 0, 0)
                telemetry.record_obstacle(dist)
                m.MOTOR_CMDS_TOTAL.labels(action="auto_avoid").inc()
                m.MOTOR_BLOCKED_TOTAL.inc()
                m.OBSTACLES_TOTAL.inc()
                m.SONAR_DISTANCE_CM.set(dist)
                with _auto_lock:
                    _auto_status["action"] = "avoiding"
                    _auto_status["distance_cm"] = round(dist, 1)
            else:
                driver.set_motors(speed, _M1_FWD, speed, _M2_FWD)
                m.MOTOR_CMDS_TOTAL.labels(action="auto_forward").inc()
                with _auto_lock:
                    _auto_status["action"] = "forward"
                    _auto_status["distance_cm"] = None if dist == float("inf") else round(dist, 1)
            time.sleep(_AUTO_TICK_SECONDS)
    finally:
        try:
            driver.set_motors(0, 0, 0, 0)
        except RuntimeError:
            pass
        with _auto_lock:
            _auto_running = False
            _auto_status["action"] = None
            _auto_status["distance_cm"] = None


def _stop_auto_drive() -> None:
    global _auto_running
    with _auto_lock:
        if not _auto_running:
            return
        _auto_running = False
        thread = _auto_thread
    if thread is not None:
        thread.join(timeout=2.0)  # bounded by _AUTO_TICK_SECONDS + _AUTO_AVOID_TURN_SECONDS
    try:
        driver.set_motors(0, 0, 0, 0)
    except RuntimeError:
        pass


@router.post("/auto/start")
async def auto_drive_start():
    global _auto_running, _auto_thread
    if not driver.available:
        raise HTTPException(
            status_code=503,
            detail={"ok": False, "message": "GPIO unavailable: rrb3 not initialized"},
        )
    with _auto_lock:
        if _auto_running:
            return {"ok": True, "running": True}
        _auto_running = True
        _auto_status["action"] = None
        _auto_status["distance_cm"] = None
        _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
        _auto_thread.start()
    return {"ok": True, "running": True}


@router.post("/auto/stop")
async def auto_drive_stop():
    _stop_auto_drive()
    return {"ok": True, "running": False}


@router.get("/auto/status")
async def auto_drive_status():
    with _auto_lock:
        return {"running": _auto_running, **_auto_status}


@router.post("/{action}")
async def motor_action(action: str, body: MotorRequest = None):
    if body is None:
        body = MotorRequest()
    if action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "message": f"Unknown action: {action}"},
        )
    _stop_auto_drive()  # taking manual control disengages autopilot
    if not driver.available:
        raise HTTPException(
            status_code=503,
            detail={"ok": False, "message": "GPIO unavailable: rrb3 not initialized"},
        )
    speed = body.speed if body.speed is not None else settings.motor_speed_default
    t0 = time.monotonic()
    try:
        if action == "forward":
            dist = driver.get_distance()
            if 0 < dist < settings.obstacle_threshold_cm:
                driver.set_motors(0, 0, 0, 0)
                driver.set_motors(speed / 2, _M1_FWD, speed / 2, _M2_REV)  # turn right to evade
                await asyncio.sleep(0.5)
                driver.set_motors(0, 0, 0, 0)
                telemetry.record_obstacle(dist)
                telemetry.record_command((time.monotonic() - t0) * 1000, action, speed)
                m.MOTOR_CMDS_TOTAL.labels(action=action).inc()
                m.MOTOR_BLOCKED_TOTAL.inc()
                m.OBSTACLES_TOTAL.inc()
                m.SONAR_DISTANCE_CM.set(dist)
                return {
                    "ok": False,
                    "action": "forward",
                    "blocked": True,
                    "message": f"Obstacle detected ({dist:.0f} cm) — stopping",
                }
            driver.set_motors(speed, _M1_FWD, speed, _M2_FWD)
        elif action == "reverse":
            driver.set_motors(speed, _M1_REV, speed, _M2_REV)
        elif action == "left":
            driver.set_motors(speed / 2, _M1_REV, speed / 2, _M2_FWD)
        elif action == "right":
            driver.set_motors(speed / 2, _M1_FWD, speed / 2, _M2_REV)
        elif action == "stop":
            driver.set_motors(0, 0, 0, 0)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail={"ok": False, "message": str(e)})
    telemetry.record_command((time.monotonic() - t0) * 1000, action, speed)
    m.MOTOR_CMDS_TOTAL.labels(action=action).inc()
    return {"ok": True, "action": action}
