import asyncio
import hashlib
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from PIL import Image, ImageDraw

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
            frame, last_counter = await loop.run_in_executor(
                None, camera_driver.get_frame_if_new, last_counter
            )
            if frame is None:
                await asyncio.sleep(0.01)
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
