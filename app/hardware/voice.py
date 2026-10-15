"""Mission-control voice: a radio call from "Houston" once the rover is up.

After startup, a background systems check waits for the camera to deliver
frames and for ML detection to settle on its backend (GPU or CPU), then
plays, framed by Apollo-style Quindar beeps:

    all OK     "RoboControl, this is Houston. All systems nominal. All systems
               connected. You are go for mission."
    otherwise  "…Systems check complete. Camera, offline. … Proceed with caution."

The clips are pre-rendered WAVs in app/audio (scripts/voice/render_voice.py)
and played with ALSA's aplay, so the robot needs nothing heavier than a
speaker: a USB speaker / USB sound card (Jetson Nano or Pi), or the Pi's
3.5 mm jack. The app's user must be in the `audio` group.

VOICE_ENABLED=false silences it; VOICE_DEVICE picks an ALSA device
(e.g. plughw:1,0) when the default isn't the speaker.
"""
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.config import settings

_log = logging.getLogger(__name__)

AUDIO_DIR = Path(__file__).resolve().parent.parent / "audio"
COMPONENTS = ("camera", "vision", "motors")
last_result: dict = {"ok": None, "message": "not played yet"}


def playback_problem() -> str | None:
    """Why sound can't play here, or None if it should."""
    if shutil.which("aplay") is None:
        return "aplay not installed (sudo apt install alsa-utils)"
    try:
        cards = Path("/proc/asound/cards").read_text()
    except OSError:
        return "no ALSA sound system on this machine"
    if "no soundcards" in cards or not cards.strip():
        return "no sound card found — plug in a USB speaker or sound card"
    controls = sorted(Path("/dev/snd").glob("controlC*"))
    if controls and not os.access(controls[0], os.R_OK | os.W_OK):
        return "no permission for the sound card — add this user to the 'audio' group"
    return None


def sequence(offline: list[str]) -> list[str]:
    """The clips to play for a systems check that found `offline` down."""
    if not offline:
        return ["quindar_in", "nominal", "quindar_out"]
    return ["quindar_in", "check", *[f"{c}_offline" for c in offline], "caution", "quindar_out"]


def play(clips: list[str], wait: bool = False) -> bool:
    """Play clips back to back (aplay plays its file list in order)."""
    global last_result
    problem = playback_problem()
    if problem:
        last_result = {"ok": False, "message": problem}
        _log.info("voice: not playing — %s", problem)
        return False
    files = [str(AUDIO_DIR / f"{c}.wav") for c in clips]
    missing = [f for f in files if not Path(f).exists()]
    if missing:
        last_result = {"ok": False, "message": f"missing audio files: {missing}"}
        return False
    cmd = ["aplay", "-q", *(["-D", settings.voice_device] if settings.voice_device else []), *files]

    def run() -> None:
        global last_result
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
            last_result = {"ok": ok, "message": "played" if ok else r.stderr.strip()[-300:]}
            if not ok:
                _log.warning("voice: aplay failed: %s", last_result["message"])
        except Exception as exc:
            last_result = {"ok": False, "message": str(exc)}

    if wait:
        run()
        return bool(last_result["ok"])
    threading.Thread(target=run, daemon=True).start()
    return True


def systems_check(timeout: float = 60.0, poll: float = 1.0) -> list[str]:
    """Components still offline once things have settled (or at timeout)."""
    from app.hardware import camera_driver, ml_driver, rrb3_driver

    end = time.monotonic() + timeout
    while True:
        camera_ok = camera_driver.available and camera_driver._frame_counter > 0
        vision_settled = not ml_driver.available or ml_driver.backend_settled.is_set()
        if (camera_ok and vision_settled) or time.monotonic() >= end:
            break
        time.sleep(poll)
    status = {
        "camera": camera_ok,
        "vision": ml_driver.available and ml_driver.backend != "none",
        "motors": rrb3_driver.available,
    }
    return [c for c in COMPONENTS if not status[c]]


def announce_startup() -> None:
    """Run the systems check and make the radio call — in the background."""
    if not settings.voice_enabled:
        return

    def run() -> None:
        offline = systems_check()
        _log.info("systems check: %s", "all nominal" if not offline else f"offline: {offline}")
        play(sequence(offline), wait=True)

    threading.Thread(target=run, daemon=True, name="voice-announce").start()
