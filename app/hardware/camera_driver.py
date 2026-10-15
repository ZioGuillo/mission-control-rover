import glob
import io
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time

import numpy as np

from app.config import settings
from app.hardware import board_info

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
_argus_proc: subprocess.Popen | None = None
_argus_sensor: int | None = None

# ── Per-board camera profile ──────────────────────────────────────────
# Which capture backends to try, in order, and the frame rate when
# CAMERA_FPS isn't set. CAMERA_BACKEND in .env overrides the order.
#   Raspberry Pi → picamera2 (libcamera + the Pi's ISP), then OpenCV for a
#                  USB webcam. ~10 fps: JPEG encoding costs a Pi Zero's CPU.
#   Jetson       → Argus (nvarguscamerasrc: the Jetson's ISP + hardware JPEG
#                  encoder), then OpenCV for a USB webcam. Plain V4L2 on a
#                  Jetson CSI camera returns raw Bayer (a solid green image),
#                  and holding it open while Argus starts can hang the board.
BOARD_CAMERA = {
    "raspberry_pi": {"backends": ("picamera2", "opencv"), "fps": 10},
    "jetson": {"backends": ("argus", "opencv"), "fps": 30},
    "unknown": {"backends": ("picamera2", "opencv"), "fps": 10},
}


def backend_order(requested: str, board_type: str) -> tuple[str, ...]:
    """Backends to try for CAMERA_BACKEND=requested on this board."""
    if requested != "auto":
        return (requested,)
    return BOARD_CAMERA.get(board_type, BOARD_CAMERA["unknown"])["backends"]


def camera_fps(board_type: str = board_info.board_type) -> int:
    if settings.camera_fps:
        return settings.camera_fps
    return BOARD_CAMERA.get(board_type, BOARD_CAMERA["unknown"])["fps"]

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

def _picam_config(fmt: str):
    return _cam.create_video_configuration(
        main={"size": (640, 480), "format": fmt},
        # Strongest ISP denoise: done in hardware, so it's free on the CPU,
        # and less sensor noise (worst at night) means much smaller JPEGs.
        controls={"FrameRate": camera_fps(), "NoiseReductionMode": 2},
    )


def _argus_sensor_id() -> int | None:
    """Argus sensor-id of the first Jetson CSI camera, or None.

    Jetson CSI cameras show up as /dev/videoN named "vi-output, <sensor>";
    Argus numbers the ones that probed OK from 0, whichever CAM port they're
    on. Needs gst-launch-1.0 (JetPack)."""
    if shutil.which("gst-launch-1.0") is None:
        return None
    for path in glob.glob("/sys/class/video4linux/video*/name"):
        try:
            with open(path) as f:
                if f.read().startswith("vi-output"):
                    return 0
        except OSError:
            continue
    return None


def _argus_cmd(sensor_id: int, fps: int, out_fd: int) -> list[str]:
    """1640x1232 is the IMX219's full-view 4:3 mode; the ISP scales it down
    and nvjpegenc encodes on the GPU, so the CPU never touches a pixel.
    JPEGs go to out_fd: nvarguscamerasrc prints its log lines to stdout."""
    return [
        "gst-launch-1.0", "-q",
        "nvarguscamerasrc", f"sensor-id={sensor_id}",
        f"wbmode={settings.camera_wb_mode}",
        f"saturation={settings.camera_saturation}",
        f"exposurecompensation={settings.camera_exposure_comp}",
        "!", f"video/x-raw(memory:NVMM),width=1640,height=1232,framerate={fps}/1",
        "!", "nvvidconv",
        "!", "video/x-raw(memory:NVMM),width=640,height=480",
        "!", "nvjpegenc", "quality=85",
        "!", "fdsink", f"fd={out_fd}",
    ]


def split_jpegs(buf: bytes) -> tuple[list[bytes], bytes]:
    """Complete JPEGs in buf, plus the unfinished remainder to keep.
    Inside a JPEG's compressed data every 0xFF is followed by 0x00, and
    nvjpegenc writes no embedded thumbnail, so the first FFD9 after an FFD8
    is that image's end."""
    frames = []
    while True:
        start = buf.find(b"\xff\xd8")
        if start < 0:
            return frames, b""
        end = buf.find(b"\xff\xd9", start + 2)
        if end < 0:
            return frames, buf[start:]
        frames.append(buf[start:end + 2])
        buf = buf[end + 2:]


def _die_with_parent() -> None:
    """Child: get SIGTERM if the app dies, so Argus never keeps the camera."""
    try:
        import ctypes
        import signal

        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


# Real camera hardware is tried first regardless of SIMULATE — SIMULATE
# fakes drivers that would otherwise need real motor/sonar/servo hardware,
# but a camera plugged in over USB/CSI is just as usable in a SIMULATE
# session (e.g. hardwareless mission testing with a real camera attached),
# so it's never ignored outright. SIMULATE only controls the synthetic
# fallback when no real camera is found.
_order = backend_order(settings.camera_backend, board_info.board_type)

if "argus" in _order:
    _argus_sensor = _argus_sensor_id()
    if _argus_sensor is not None:
        available = True
        _backend = "argus"

if not available and "picamera2" in _order:
    try:
        from picamera2 import Picamera2

        _tuning = None
        if settings.camera_noir:
            # The tuning must be chosen before libcamera starts, so the sensor
            # name comes from the kernel (e.g. "ov5647 10-0036"), not from
            # Picamera2.global_camera_info(), which would start it too early.
            _model = ""
            for _p in glob.glob("/sys/class/video4linux/v4l-subdev*/name"):
                with open(_p) as _f:
                    _name = _f.read().split()
                if _name and _name[0].startswith(("ov", "imx")):
                    _model = _name[0]
                    break
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
        # YUV420 feeds the Pi's hardware JPEG encoder (see _picamera2_loop).
        _cam.configure(_picam_config("MJPEG" if _picam_jpeg else "YUV420"))
        available = True
        _backend = "picamera2"
    except Exception:
        pass

# Fallback for a USB webcam (Pi or Jetson with no CSI camera): any
# V4L2-visible camera via OpenCV. Requires opencv-python-headless — see
# requirements.txt. The backend is chosen even with no camera plugged in
# yet: the capture loop keeps looking, so a USB camera can be plugged in
# (or re-plugged) at any time.
if not available and "opencv" in _order:
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


class _FrameSink(io.BufferedIOBase):
    """Receives each JPEG from picamera2's encoder and publishes it."""

    def write(self, buf) -> int:
        global _latest_frame, _frame_counter
        with _lock:
            _latest_frame = bytes(buf)
            _frame_counter += 1
        return len(buf)


def _picamera2_hw_jpeg() -> bool:
    """CSI camera: JPEG-encode on the GPU's hardware encoder (Pi Zero–4).
    Encoding in Python instead pins a Pi Zero's CPU. Returns False when
    there's no hardware encoder (Pi 5) so the caller can encode in software."""
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput

    try:
        _cam.start_recording(MJPEGEncoder(), FileOutput(_FrameSink()))
    except Exception as exc:
        _log.warning("no hardware JPEG encoder (%s), encoding in software", exc)
        return False
    try:
        while _running:
            time.sleep(0.2)
    finally:
        _cam.stop_recording()
    return True


def _picamera2_loop() -> None:
    global _latest_frame, _frame_counter, _running
    try:
        if not _picam_jpeg:
            if _picamera2_hw_jpeg():
                return
            import simplejpeg  # ships with picamera2

            _cam.configure(_picam_config("RGB888"))
        _cam.start()

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


def _argus_loop() -> None:
    """Capture from a Jetson CSI camera through Argus. If gst-launch-1.0
    exits (nvargus-daemon restarted, cable knocked loose) the camera counts
    as gone and the pipeline is restarted every _REDETECT_SECONDS."""
    global _latest_frame, _frame_counter, _argus_proc

    while _running:
        r, w = os.pipe()
        try:
            proc = subprocess.Popen(
                _argus_cmd(_argus_sensor, camera_fps(), w),
                pass_fds=(w,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                # Linux-only (prctl); running Python in a forked child of a
                # threaded process can crash on macOS, where there's no Argus anyway
                preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None,
            )
        except OSError as exc:
            os.close(r)
            os.close(w)
            _log.error("can't start Argus camera pipeline: %s", exc)
            _set_available(False)
            time.sleep(_REDETECT_SECONDS)
            continue
        os.close(w)
        _argus_proc = proc
        buf = b""
        got_frame = False
        with os.fdopen(r, "rb", buffering=0) as pipe:
            while _running:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                frames, buf = split_jpegs(buf + chunk)
                if frames:
                    if not got_frame:
                        got_frame = True
                        _log.info("camera connected (Argus sensor %d)", _argus_sensor)
                        _set_available(True)
                    with _lock:
                        _latest_frame = frames[-1]
                        _frame_counter += len(frames)
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        _argus_proc = None
        if not _running:
            break
        err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        _log.warning("Argus camera pipeline exited (%s) — restarting: %s",
                     proc.returncode, err[-300:])
        with _lock:
            _latest_frame = None
        _set_available(False)
        time.sleep(_REDETECT_SECONDS)


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
    if _backend == "argus":
        _argus_loop()
    elif _backend == "picamera2":
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
    proc = _argus_proc
    if proc is not None and proc.poll() is None:
        proc.terminate()  # unblocks _argus_loop's read and frees the camera


def get_frame() -> bytes | None:
    with _lock:
        return _latest_frame


def get_frame_if_new(last_counter: int) -> tuple[bytes | None, int]:
    """Return (frame, counter). frame is None when no new frame since last_counter."""
    with _lock:
        if _frame_counter == last_counter:
            return None, last_counter
        return _latest_frame, _frame_counter
