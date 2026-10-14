"""
Detects which single-board computer this is running on, for display only
(e.g. the Telemetry tab's "<Board> Battery" card). Not used for any
hardware-selection logic — app/hardware/gpio_compat.py already owns that.

Primary source: /proc/device-tree/model, present on both Raspberry Pi and
Jetson boards (scripts/install.sh already reads this same file for the
GPIO-backend install step) — works directly on a bare-metal install. A
container usually can't see this file even when bind-mounted (Docker's own
procfs mount shadows anything placed under /proc), so BOARD_MODEL can be
passed in as an env var instead (read from the host at `docker run` time).
Falls back to the GPIO backend that actually loaded if neither is present.
"""
import os
from pathlib import Path

from app.hardware import gpio_compat

_DEVICE_TREE_MODEL = Path("/proc/device-tree/model")

_SHORT_NAMES = {
    "raspberry_pi": "Pi",
    "jetson": "Jetson",
    "unknown": "Board",
}


def _read_device_tree_model() -> str | None:
    env_model = os.environ.get("BOARD_MODEL", "").strip()
    if env_model:
        return env_model
    try:
        raw = _DEVICE_TREE_MODEL.read_bytes()
    except Exception:
        return None
    model = raw.decode("utf-8", errors="ignore").strip("\x00").strip()
    return model or None


def _classify(model: str | None) -> str:
    if model:
        lower = model.lower()
        if "raspberry pi" in lower:
            return "raspberry_pi"
        if "jetson" in lower or "nvidia" in lower:
            return "jetson"
        return "unknown"

    if gpio_compat.backend in ("RPi.GPIO", "rpi-lgpio"):
        return "raspberry_pi"
    if gpio_compat.backend == "Jetson.GPIO":
        return "jetson"
    return "unknown"


model = _read_device_tree_model()
board_type = _classify(model)
short_name = _SHORT_NAMES[board_type]
battery_label = f"{short_name} Battery"
battery_sub = f"powers {model or short_name}"


# ── Unit number (which rover this is) ─────────────────────────────────
# Every board has a factory serial number. units.txt at the repo root maps
# each serial to a unit number, so the Pi Zero and the Jetson don't both
# call themselves "Unit 01". scripts/install.sh registers new boards.
# UNIT_NUMBER in .env overrides the registry.
_SERIAL_FILE = Path("/proc/device-tree/serial-number")
UNITS_FILE = Path(__file__).resolve().parents[2] / "units.txt"


def _read_serial() -> str | None:
    try:
        raw = _SERIAL_FILE.read_bytes().decode("utf-8", errors="ignore")
    except Exception:
        return None
    return raw.strip("\x00").strip() or None


def read_units(path: Path = UNITS_FILE) -> dict[str, int]:
    """serial → unit number, from 'serial  unit  description' lines."""
    units: dict[str, int] = {}
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return units
    for line in lines:
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            units[parts[0].lower()] = int(parts[1])
    return units


def _unit_number() -> int | None:
    from app.config import settings

    if settings.unit_number:
        return settings.unit_number
    if serial:
        return read_units().get(serial.lower())
    return None


serial = _read_serial()
unit_number = _unit_number()
unit_label = f"Unit {unit_number:02d}" if unit_number else "Unit —"
