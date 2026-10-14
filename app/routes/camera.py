import asyncio
import hashlib
import io
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from PIL import Image, ImageDraw

from app.config import settings
from app.hardware import camera_driver, ml_driver
from app.hardware.servo_driver import STEP
from app.hardware import servo_driver as driver

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


def _label_color(label: str) -> tuple[int, int, int]:
    h = int(hashlib.md5(label.encode()).hexdigest()[:6], 16)
    r = max((h >> 16) & 0xFF, 80)
    g = max((h >> 8) & 0xFF, 80)
    b = max(h & 0xFF, 80)
    return (r, g, b)


def _annotate(frame: bytes, detections: list[dict]) -> bytes:
    if not detections:
        return frame
    img = Image.open(io.BytesIO(frame)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for det in detections:
        y1, x1, y2, x2 = det["box"]
        x1c = max(0, min(int(x1 * w), w - 1))
        y1c = max(0, min(int(y1 * h), h - 1))
        x2c = max(0, min(int(x2 * w), w - 1))
        y2c = max(0, min(int(y2 * h), h - 1))
        color = _label_color(det["label"])
        draw.rectangle([x1c, y1c, x2c, y2c], outline=color, width=2)
        text = f"{det['label']} {int(det['score'] * 100)}%"
        text_w = len(text) * 7
        draw.rectangle([x1c, max(y1c - 16, 0), x1c + text_w, y1c], fill=color)
        draw.text((x1c + 2, max(y1c - 15, 0)), text, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@router.get("/health")
async def camera_health():
    """Return current frame counter so the client can detect a frozen stream."""
    _, counter = camera_driver.get_frame_if_new(-1)
    return {"available": camera_driver.available, "counter": counter}


@router.get("/stream")
async def mjpeg_stream():
    if not camera_driver.available:
        raise HTTPException(status_code=503, detail="Camera not available")

    async def generate():
        loop = asyncio.get_running_loop()
        last_counter = 0
        while True:
            # Just a lock + compare: cheap enough to call inline. Going through
            # the thread pool 100×/s per viewer pinned a Pi Zero's CPU.
            frame, last_counter = camera_driver.get_frame_if_new(last_counter)
            if frame is None:
                await asyncio.sleep(0.03)
                continue
            if ml_driver.available and ml_driver.enabled:
                detections = ml_driver.get_detections()
                if detections:
                    frame = await loop.run_in_executor(
                        None, _annotate, frame, detections
                    )
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )

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
    frame = camera_driver.get_frame()
    if frame is None:
        raise HTTPException(status_code=503, detail="No camera frame available")
    folder = _training_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("frame_%Y%m%d_%H%M%S_%f.jpg")
    (folder / name).write_bytes(frame)
    return {"ok": True, "file": name, "count": len(_training_frames())}


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
