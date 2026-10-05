"""
Jetson GPU utilization/clock — display-only telemetry, not used for any
hardware-selection logic. Raspberry Pi has no equivalent sysfs node (no
discrete/integrated GPU exposed this way), so `available` is simply False
there and the Telemetry card hides itself.

Reads directly from sysfs rather than requiring jetson-stats (which needs
a root-level background service + udev rules to install) — these two
particular files are world-readable on stock L4T without any extra setup:
  - /sys/devices/gpu.0/load        0-1000, GPU busy-time permille (÷10 = %)
  - /sys/devices/gpu.0/devfreq/*/cur_freq   current clock, Hz

GPU *power* (watts) would need the board's INA3221 power monitor, which is
root-only (0600) on stock L4T — out of scope here; see docker/README.md's
BOARD_MODEL section if that ever gets added via jetson-stats instead.
"""
import glob
from pathlib import Path

_LOAD_PATH = Path("/sys/devices/gpu.0/load")
_FREQ_GLOB = "/sys/devices/gpu.0/devfreq/*/cur_freq"

available = _LOAD_PATH.exists()


def get_usage_percent() -> float | None:
    if not available:
        return None
    try:
        raw = int(_LOAD_PATH.read_text().strip())
        return round(raw / 10.0, 1)
    except Exception:
        return None


def get_clock_mhz() -> float | None:
    if not available:
        return None
    try:
        matches = glob.glob(_FREQ_GLOB)
        if not matches:
            return None
        hz = int(Path(matches[0]).read_text().strip())
        return round(hz / 1_000_000, 0)
    except Exception:
        return None
