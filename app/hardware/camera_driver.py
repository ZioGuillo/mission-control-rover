import io
import logging
import math
import threading
import time

import numpy as np

from app.config import settings

_log = logging.getLogger(__name__)

available = False
_cam = None
_lock = threading.Lock()
_latest_frame: bytes | None = None
_frame_counter: int = 0
_running = False
_backend: str = "none"

# Real camera hardware is tried first regardless of SIMULATE — SIMULATE
# fakes drivers that would otherwise need real motor/sonar/servo hardware,
# but a camera plugged in over USB/CSI is just as usable in a SIMULATE
# session (e.g. hardwareless mission testing with a real camera attached),
# so it's never ignored outright. SIMULATE only controls the synthetic
# fallback when no real camera is found.
try:
    from picamera2 import Picamera2

    _cam = Picamera2()
    _cam.configure(_cam.create_video_configuration(
        main={"size": (640, 480), "format": "MJPEG"},
    ))
    available = True
    _backend = "picamera2"
except Exception:
    pass

# Fallback for boards without picamera2/libcamera (e.g. Jetson Nano, or a
# Pi with a USB webcam instead of a CSI camera): any V4L2-visible camera
# via OpenCV. Requires opencv-python-headless — see requirements.txt.
if not available:
    try:
        import cv2

        _cv_cam = cv2.VideoCapture(0)
        if _cv_cam.isOpened():
            _cv_cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            _cv_cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            available = True
            _backend = "opencv"
        else:
            _cv_cam.release()
            _cv_cam = None
    except Exception:
        pass

if not available and settings.simulate:
    # No real camera found — synthetic frames so the camera tab and
    # ML-overlay UI are still usable without hardware. See README "Local
    # testing without hardware".
    available = True
    _backend = "simulated"


def _picamera2_loop() -> None:
    global _latest_frame, _frame_counter, _running
    try:
        _cam.start()
        while _running:
            request = _cam.capture_request()
            try:
                buf = request.make_buffer("main")
                # buf is a numpy array preallocated to max size; find actual JPEG end
                ff_pos = np.where((buf[:-1] == 0xFF) & (buf[1:] == 0xD9))[0]
                if len(ff_pos):
                    data = bytes(buf[: int(ff_pos[-1]) + 2])
                    with _lock:
                        _latest_frame = data
                        _frame_counter += 1
            finally:
                request.release()
    except Exception as exc:
        _log.error("camera capture loop error: %s", exc)
        _running = False
    finally:
        try:
            _cam.stop()
        except Exception:
            pass


def _opencv_loop() -> None:
    global _latest_frame, _frame_counter, _running
    import cv2

    try:
        while _running:
            ok, frame = _cv_cam.read()
            if not ok:
                time.sleep(0.05)
                continue
            ok2, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if ok2:
                with _lock:
                    _latest_frame = buf.tobytes()
                    _frame_counter += 1
    except Exception as exc:
        _log.error("camera capture loop error: %s", exc)
        _running = False
    finally:
        try:
            _cv_cam.release()
        except Exception:
            pass


def _simulated_loop() -> None:
    global _latest_frame, _frame_counter, _running
    from PIL import Image, ImageDraw

    w, h = 640, 480
    t0 = time.monotonic()
    try:
        while _running:
            t = time.monotonic() - t0
            img = Image.new("RGB", (w, h), (20, 24, 36))
            draw = ImageDraw.Draw(img)
            # a ball bouncing across the frame — gives the dashboard/ML
            # overlay something visibly changing to render, frame to frame
            x = int((math.sin(t * 0.8) * 0.5 + 0.5) * (w - 60)) + 30
            y = int((math.sin(t * 1.3) * 0.5 + 0.5) * (h - 60)) + 30
            draw.ellipse([x - 30, y - 30, x + 30, y + 30], fill=(90, 160, 255))
            draw.text((10, 10), f"SIMULATED CAMERA  frame {_frame_counter}", fill=(220, 220, 220))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            with _lock:
                _latest_frame = buf.getvalue()
                _frame_counter += 1
            time.sleep(0.1)  # ~10 fps
    except Exception as exc:
        _log.error("camera capture loop error (simulated): %s", exc)
        _running = False


def _capture_loop() -> None:
    if _backend == "picamera2":
        _picamera2_loop()
    elif _backend == "opencv":
        _opencv_loop()
    elif _backend == "simulated":
        _simulated_loop()


def start() -> None:
    global _running
    if not available or _running:
        return
    _running = True
    threading.Thread(target=_capture_loop, daemon=True).start()


def stop() -> None:
    global _running
    _running = False


def get_frame() -> bytes | None:
    with _lock:
        return _latest_frame


def get_frame_if_new(last_counter: int) -> tuple[bytes | None, int]:
    """Return (frame, counter). frame is None when no new frame since last_counter."""
    with _lock:
        if _frame_counter == last_counter:
            return None, last_counter
        return _latest_frame, _frame_counter
