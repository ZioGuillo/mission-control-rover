"""
Portable GPIO backend selection.

Every other driver in app/hardware/ imports `GPIO` from here instead of
importing RPi.GPIO directly, so the same code runs across boards:

  - Raspberry Pi 3/4      → RPi.GPIO
  - Raspberry Pi 5        → RPi.GPIO too, *if* the `rpi-lgpio` package is
                             installed instead of the real RPi.GPIO — it
                             registers itself under the same module name
                             and exposes the same API, talking to the Pi 5's
                             RP1 chip via lgpio under the hood. No driver
                             code needs to change; only the installed
                             package differs (see scripts/install.sh).
  - Jetson Nano / Jetson   → Jetson.GPIO, which mirrors RPi.GPIO's API
                             (setmode, setup, output, input, PWM, cleanup)
                             including a BCM-compatible numbering mode for
                             boards with a Raspberry-Pi-shaped 40-pin header.
  - Anything else (macOS   → GPIO stays None and `available` is False, so
    dev machine, CI, etc.)   callers degrade gracefully like the rest of
                             app/hardware/*.
"""
import logging

_log = logging.getLogger(__name__)

GPIO = None
available = False
backend = "none"

try:
    import RPi.GPIO as GPIO  # noqa: F811 — intentional conditional import
    available = True
    backend = "RPi.GPIO"
except Exception:
    try:
        import Jetson.GPIO as GPIO  # noqa: F811
        available = True
        backend = "Jetson.GPIO"
    except Exception:
        _log.debug("no GPIO backend available (not a Pi/Jetson, or the library isn't installed)")
