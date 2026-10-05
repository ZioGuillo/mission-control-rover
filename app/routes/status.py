from fastapi import APIRouter
from app.hardware import rrb3_driver, servo_driver

router = APIRouter(prefix="/api")


@router.get("/ping")
async def ping():
    return {"ok": True}


@router.get("/status")
async def get_status():
    motor_ok = rrb3_driver.available
    return {
        "ok": motor_ok,
        "message": "Ready" if motor_ok else "motor driver unavailable",
        "servo_ok": servo_driver.available,
        "battery_motor_ok": motor_ok,
        "battery_pi_ok": True,
    }
