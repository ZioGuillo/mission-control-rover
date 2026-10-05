from fastapi import APIRouter
from app import telemetry
from app.hardware import board_info, gpu_info, rrb3_driver as driver

router = APIRouter(prefix="/api/telemetry")


@router.get("")
def get_telemetry():
    data = telemetry.snapshot()
    data["battery_v"] = driver.get_battery_voltage()
    data["battery_motor_ok"] = driver.available
    data["battery_pi_ok"] = True
    data["board_battery_label"] = board_info.battery_label
    data["board_battery_sub"] = board_info.battery_sub
    data["gpu_available"] = gpu_info.available
    data["gpu_percent"] = gpu_info.get_usage_percent()
    data["gpu_clock_mhz"] = gpu_info.get_clock_mhz()
    return data
