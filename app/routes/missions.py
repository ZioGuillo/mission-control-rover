from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/missions")


@router.post("/path")
async def path_mission():
    # ── IMPLEMENT: Path Following ──────────────────────────────────────────────
    # Goal: accept a sequence of waypoints and drive the car through each one.
    #
    # Suggested steps:
    #   1. Add a Pydantic request body (e.g. {"waypoints": [{"action": "forward",
    #      "duration": 1.0}, {"action": "left", "duration": 0.3}, ...]})
    #   2. Loop over waypoints, call the motor driver, sleep for `duration`
    #   3. Check driver.get_distance() before each forward step to avoid obstacles
    #
    # Useful imports:
    #   from app.hardware import rrb3_driver as driver   (motor control)
    #   from app.config import settings                  (thresholds)
    #   import asyncio                                   (async sleep)
    #
    # Return {"ok": True, "completed": <n_waypoints>} when done.
    # ──────────────────────────────────────────────────────────────────────────
    raise HTTPException(
        status_code=501,
        detail={"ok": False, "message": "Path following not yet implemented"},
    )


@router.post("/search")
async def search_mission():
    # ── IMPLEMENT: Visual Search ───────────────────────────────────────────────
    # Goal: roam until the camera sees a target image, then beep and alert.
    #
    # Suggested steps:
    #   1. Accept a target image in the request body (base64 or multipart)
    #   2. Start a roam loop: forward → check sonar → turn if blocked
    #   3. Capture frames from CAMERA_STREAM_URL and compare with the target
    #      (e.g. using OpenCV template matching or a feature descriptor)
    #   4. On match: stop motors, trigger buzzer (GPIO settings.buzzer_pin),
    #      return {"ok": True, "found": True, "distance_cm": <sonar reading>}
    #
    # Useful imports:
    #   from app.hardware import rrb3_driver as driver   (motors + sonar)
    #   from app.config import settings                  (camera URL, buzzer pin)
    #   import asyncio                                   (async sleep)
    # ──────────────────────────────────────────────────────────────────────────
    raise HTTPException(
        status_code=501,
        detail={"ok": False, "message": "Visual search not yet implemented"},
    )
