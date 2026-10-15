import asyncio
import io
import time
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.config import settings
from app.hardware import camera_driver, ml_driver
from app.hardware.servo_driver import STEP
from app.hardware import servo_driver as driver
from app.security import client_ip

router = APIRouter(prefix="/api/camera")

ACTION_DELTAS: dict[str, tuple[int, int]] = {
    "up":        (0,     STEP),
    "down":      (0,    -STEP),
    "left":      (-STEP, 0),
    "right":     (STEP,  0),
    "upleft":    (-STEP, STEP),
    "upright":   (STEP,  STEP),
    "downleft":  (-STEP, -STEP),
    "downright": (STEP,  -STEP),
}


@router.get("/health")
async def camera_health():
    """Return current frame counter so the client can detect a frozen stream."""
    _, counter = camera_driver.get_frame_if_new(-1, wanted=False)
    # idle: capture paused with no viewer — a still counter isn't a dead camera
    return {"available": camera_driver.available, "counter": counter,
            "idle": camera_driver.idle()}


@router.get("/detections")
async def detections():
    """Latest ML detections for the browser to draw over the stream.
    Boxes are [y1, x1, y2, x2], 0-1 of the camera frame."""
    if not (ml_driver.available and ml_driver.enabled):
        return {"enabled": False, "detections": []}
    return {"enabled": True, "detections": ml_driver.get_shown_detections()}


class StreamSlots:
    """At most `limit` open streams per viewer; a new one ends that viewer's
    oldest. A browser that leaks streams on reconnect (or a forgotten tab)
    can't pile them up: six at 30 fps saturated the rover's Wi-Fi and every
    one of them froze."""

    def __init__(self, limit: int):
        self.limit = limit
        self._open: dict[str, list[asyncio.Event]] = {}

    def open(self, viewer: str) -> asyncio.Event:
        """Register a stream; its event is set when it should end."""
        stop = asyncio.Event()
        streams = self._open.setdefault(viewer, [])
        streams.append(stop)
        while len(streams) > self.limit:
            streams.pop(0).set()
        return stop

    def close(self, viewer: str, stop: asyncio.Event) -> None:
        streams = self._open.get(viewer, [])
        if stop in streams:
            streams.remove(stop)
        if not streams:
            self._open.pop(viewer, None)

    def count(self, viewer: str) -> int:
        return len(self._open.get(viewer, []))


# Two: a Camera tab and a Drive tab may both be open in one browser.
_streams = StreamSlots(limit=2)


def _viewer(request: Request) -> str:
    """One browser session. Not the IP alone: behind the Cloudflare tunnel
    every remote viewer arrives from 127.0.0.1."""
    token = request.session.get("token") if "session" in request.scope else None
    return f"{client_ip(request)}|{token or ''}"


@router.get("/stream")
async def mjpeg_stream(request: Request):
    if not camera_driver.available:
        raise HTTPException(status_code=503, detail="Camera not available")
    viewer = _viewer(request)
    interval = 1.0 / settings.stream_fps

    async def generate():
        stop = _streams.open(viewer)
        last_counter = 0
        next_send = 0.0
        try:
            while not stop.is_set():
                # STREAM_FPS caps what goes over Wi-Fi; detection still reads
                # every camera frame directly.
                wait = next_send - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                # Just a lock + compare: cheap enough to call inline. Going through
                # the thread pool 100×/s per viewer pinned a Pi Zero's CPU.
                frame, last_counter = camera_driver.get_frame_if_new(last_counter)
                if frame is None:
                    await asyncio.sleep(0.03)
                    continue
                next_send = time.monotonic() + interval
                # Frames go out untouched: detection boxes are drawn by the
                # browser (/detections). Drawing them here meant decoding and
                # re-encoding every frame for every viewer — 30×/s each — which
                # starved the detection loop itself (2/s instead of ~6/s).
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
        finally:
            _streams.close(viewer, stop)

    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")


# ── Training frames ────────────────────────────────────────────────────
# Frames saved from this camera, as-is (no detection boxes drawn), to
# retrain the Lego model on what the rover actually sees: its lens, its
# colours, its rooms. Download the zip and label it (e.g. Roboflow), then
# run scripts/train_lego_detector.ipynb.

def _training_dir() -> Path:
    return settings.data_dir / "training_frames"


def _training_frames() -> list[Path]:
    return sorted(_training_dir().glob("*.jpg"))


@router.get("/training-frames")
async def training_frames_count():
    return {"count": len(_training_frames())}


@router.post("/training-frame")
async def save_training_frame():
    frame = await asyncio.to_thread(camera_driver.wait_for_frame)
    if frame is None:
        raise HTTPException(status_code=503, detail="No camera frame available")
    folder = _training_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("frame_%Y%m%d_%H%M%S_%f.jpg")
    (folder / name).write_bytes(frame)
    return {"ok": True, "file": name, "count": len(_training_frames())}


@router.delete("/training-frames")
async def clear_training_frames():
    frames = _training_frames()
    for f in frames:
        f.unlink(missing_ok=True)
    return {"ok": True, "deleted": len(frames)}


@router.get("/training-frames.zip")
async def download_training_frames():
    frames = _training_frames()
    if not frames:
        raise HTTPException(status_code=404, detail="No training frames saved yet")
    buf = io.BytesIO()
    # JPEGs don't compress further — store them, it's much faster
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for f in frames:
            zf.write(f, f.name)
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="training_frames_{stamp}.zip"'},
    )


@router.post("/{action}")
async def camera_action(action: str):
    if not driver.available:
        raise HTTPException(
            status_code=503,
            detail={"ok": False, "message": "Servo driver unavailable"},
        )
    try:
        if action == "center":
            angles = driver.center()
        elif action in ACTION_DELTAS:
            pan_d, tilt_d = ACTION_DELTAS[action]
            angles = driver.move(pan_d, tilt_d)
        else:
            raise HTTPException(
                status_code=400,
                detail={"ok": False, "message": f"Unknown camera action: {action}"},
            )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail={"ok": False, "message": str(e)})
    return {"ok": True, "action": action, "angles": angles}
