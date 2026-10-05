# ── New feature template ───────────────────────────────────────────────────────
#
# HOW TO ADD A NEW FEATURE
# ========================
# 1. Copy this file to app/routes/my_feature.py  (no leading underscore)
# 2. Set the `prefix` below to your API path, e.g. "/api/lights"
# 3. Add your endpoints — GET, POST, whatever you need
# 4. That's it. The app auto-discovers this file on next start; no edits
#    needed anywhere else.
#
# Hardware helpers available:
#   from app.hardware import rrb3_driver as driver   → motors, sonar, battery
#   from app.hardware import servo_driver             → pan/tilt servos
#
# Config values available:
#   from app.config import settings                  → all .env settings
#
# Telemetry (optional — record your commands in the Telemetry tab):
#   from app import telemetry
#   telemetry.record_command(latency_ms, action, speed)
# ──────────────────────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException

# ── 1. Change the prefix to match your feature's API path ─────────────────────
router = APIRouter(prefix="/api/my_feature")
# ──────────────────────────────────────────────────────────────────────────────


# ── 2. Add your endpoints below ───────────────────────────────────────────────

@router.get("/status")
async def my_feature_status():
    """Return the current state of my feature."""
    # TODO: replace with real implementation
    return {"ok": True, "status": "idle"}


@router.post("/{action}")
async def my_feature_action(action: str):
    """Perform an action."""
    valid_actions = {"on", "off"}   # TODO: define your valid actions
    if action not in valid_actions:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "message": f"Unknown action: {action}"},
        )

    # TODO: add your logic here
    # Example using the motor driver:
    #   from app.hardware import rrb3_driver as driver
    #   if not driver.available:
    #       raise HTTPException(503, {"ok": False, "message": "Hardware unavailable"})
    #   driver.set_motors(...)

    return {"ok": True, "action": action}

# ──────────────────────────────────────────────────────────────────────────────
