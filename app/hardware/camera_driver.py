import glob
import io
import logging
import math
import sys
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
_cv_cam = None
_picam_jpeg = False  # picamera2 backend: camera delivers ready-made JPEG (USB/UVC)

# USB camera hot-plug (OpenCV backend): this many failed reads in a row means
# the camera is gone; while it's gone, look for one again this often.
_READ_FAIL_LIMIT = 20
_READ_RETRY_SECONDS = 0.05
_REDETECT_SECONDS = 2.0


def _video_device_indexes() -> list[int]:
    """V4L2 camera indexes present right now, from /dev/videoN."""
    idx = []
    for path in glob.glob("/dev/video*"):
        suffix = path[len("/dev/video"):]
        if suffix.isdigit():
            idx.append(int(suffix))
    return sorted(idx)


def _open_opencv(allow_default_index: bool = False):
    """Open the first working V4L2 camera, or return None.

    allow_default_index: off Linux (dev machines) there's no /dev/video*, so
    the one-time startup check falls back to OpenCV's index 0. The background
    rescan never does — on macOS opening a camera from a non-main thread
    crashes the interpreter."""
    import cv2

    indexes = _video_device_indexes()
    if not indexes and allow_default_index and not sys.platform.startswith("linux"):
        indexes = [0]
    for i in indexes:
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            return cap
        cap.release()
    return None

# Real camera hardware is tried first regardless of SIMULATE — SIMULATE
# fakes drivers that would otherwise need real motor/sonar/servo hardware,
# but a camera plugged in over USB/CSI is just as usable in a SIMULATE
# session (e.g. hardwareless mission testing with a real camera attached),
# so it's never ignored outright. SIMULATE only controls the synthetic
# fallback when no real camera is found.
try:
    from picamera2 import Picamera2

    _tuning = None
    if settings.camera_noir:
        _model = str(Picamera2.global_camera_info()[0].get("Model", ""))
        try:
            _tuning = Picamera2.load_tuning_file(f"{_model}_noir.json")
        except Exception:
            _log.warning("CAMERA_NOIR set but no %s_noir.json tuning file", _model)
    _cam = Picamera2(tuning=_tuning)
    # Only USB (UVC) cameras deliver MJPEG. A CSI camera (Pi Camera, OV5647,
    # IMX219…) comes out of the ISP as raw pixels: libcamera silently swaps
    # MJPEG for YUYV, so the JPEG end-marker search never finds a frame.
    # For those, ask for RGB and JPEG-encode each frame ourselves.
    _picam_jpeg = "usb" in str(Picamera2.global_camera_info()[0].get("Id", "")).lower()
    _cam.configure(_cam.create_video_configuration(
        main={"size": (640, 480), "format": "MJPEG" if _picam_jpeg else "RGB888"},
        controls={"FrameRate": settings.camera_fps},
    ))
    available = True
    _backend = "picamera2"
except Exception:
    pass

# Fallback for boards without picamera2/libcamera (e.g. Jetson Nano, or a
# Pi with a USB webcam instead of a CSI camera): any V4L2-visible camera
# via OpenCV. Requires opencv-python-headless — see requirements.txt.
# The backend is chosen even with no camera plugged in yet: the capture loop
# keeps looking, so a USB camera can be plugged in (or re-plugged) at any time.
if not available:
    try:
        import cv2  # noqa: F401 — only checking that OpenCV is importable

        _backend = "opencv"
        _cv_cam = _open_opencv(allow_default_index=True)
        available = _cv_cam is not None
    except Exception:
        pass

if _backend in ("none", "opencv") and not available and settings.simulate:
    # No real camera found — synthetic frames so the camera tab and
    # ML-overlay UI are still usable without hardware. See README "Local
    # testing without hardware".
    available = True
    _backend = "simulated"


def _picamera2_loop() -> None:
    global _latest_frame, _frame_counter, _running
    try:
        _cam.start()
        if not _picam_jpeg:
            import simplejpeg  # ships with picamera2

        while _running:
            request = _cam.capture_request()
            try:
                if _picam_jpeg:
                    buf = request.make_buffer("main")
                    # buf is a numpy array preallocated to max size; find actual JPEG end
                    ff_pos = np.where((buf[:-1] == 0xFF) & (buf[1:] == 0xD9))[0]
                    data = bytes(buf[: int(ff_pos[-1]) + 2]) if len(ff_pos) else None
                else:
                    # picamera2's "RGB888" is stored B,G,R in memory
                    data = simplejpeg.encode_jpeg(
                        request.make_array("main"), quality=80, colorspace="BGR"
                    )
            finally:
                request.release()
            if data:
                with _lock:
                    _latest_frame = data
                    _frame_counter += 1
    except Exception as exc:
        _log.error("camera capture loop error: %s", exc)
        _running = False
    finally:
        try:
            _cam.stop()
        except Exception:
            pass


def _set_available(value: bool) -> None:
    global available
    available = value
    try:
        from app import metrics as m
        m.HW_AVAILABLE.labels(component="camera").set(1 if value else 0)
    except Exception:
        pass


def _opencv_loop() -> None:
    """Capture from a USB/V4L2 camera, surviving unplug and re-plug: after
    _READ_FAIL_LIMIT failed reads in a row the camera counts as gone — it's
    released, marked unavailable and its last frame dropped (so ML inference
    doesn't keep acting on a frozen image) — then /dev/video* is rescanned
    every _REDETECT_SECONDS until a camera opens again."""
    global _latest_frame, _frame_counter, _running, _cv_cam
    import cv2

    fails = 0
    try:
        while _running:
            if _cv_cam is None:
                _cv_cam = _open_opencv()
                if _cv_cam is None:
                    time.sleep(_REDETECT_SECONDS)
                    continue
                _log.info("camera connected")
                _set_available(True)
                fails = 0

            ok, frame = _cv_cam.read()
            if not ok:
                fails += 1
                if fails >= _READ_FAIL_LIMIT:
                    _log.warning("camera disconnected — will keep looking for it")
                    try:
                        _cv_cam.release()
                    except Exception:
                        pass
                    _cv_cam = None
                    with _lock:
                        _latest_frame = None
                    _set_available(False)
                    continue
                time.sleep(_READ_RETRY_SECONDS)
                continue
            fails = 0
            ok2, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if ok2:
                with _lock:
                    _latest_frame = buf.tobytes()
                    _frame_counter += 1
    except Exception as exc:
        _log.error("camera capture loop error: %s", exc)
        _running = False
    finally:
        if _cv_cam is not None:
            try:
                _cv_cam.release()
            except Exception:
                pass
            _cv_cam = None


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
    if _running:
        return
    # The OpenCV loop also runs with no camera yet — it waits for one to be
    # plugged in. Other backends need a camera that's already there.
    if not available and _backend != "opencv":
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
