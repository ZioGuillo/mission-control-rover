import io
import logging
import threading
import time
from pathlib import Path

from app.hardware import camera_driver

_log = logging.getLogger(__name__)

available = False
library_available = False
library_error: str | None = None
model_found = False
enabled = False
_interpreter = None
_labels: list[str] = []
_lock = threading.Lock()
_detections: list[dict] = []
_running = False

_MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
_MODEL_PATH = _MODELS_DIR / "efficientdet_lite0.tflite"
_LABELS_PATH = _MODELS_DIR / "coco_labels.txt"

# Second, parallel single-class detector — trained separately (see
# scripts/train_lego_detector.ipynb), runs alongside the COCO model rather
# than being merged into it, so the person/animal/object detection above
# is untouched regardless of whether this one is present.
_LEGO_MODEL_PATH = _MODELS_DIR / "lego_minifigure.tflite"
_lego_interpreter = None
lego_available = False

model_found = _MODEL_PATH.exists() and _LABELS_PATH.exists()

try:
    import numpy as np
    from PIL import Image
    try:
        import tflite_runtime.interpreter as tflite
    except ImportError:
        import ai_edge_litert.interpreter as tflite  # type: ignore[no-redef]
    library_available = True

    if model_found:
        _interpreter = tflite.Interpreter(model_path=str(_MODEL_PATH))
        _interpreter.allocate_tensors()
        _labels = _LABELS_PATH.read_text().strip().splitlines()
        available = True

    if _LEGO_MODEL_PATH.exists():
        try:
            _lego_interpreter = tflite.Interpreter(model_path=str(_LEGO_MODEL_PATH))
            _lego_interpreter.allocate_tensors()
            lego_available = True
        except Exception as exc:
            _log.debug("lego detector unavailable: %s", exc)

    # `available` gates whether the detection loop runs at all — either
    # detector being ready is enough, each degrades independently below.
    available = available or lego_available
except Exception as exc:
    library_error = str(exc)
    _log.debug("ml_driver unavailable: %s", exc)


def _run_inference(frame: bytes) -> list[dict]:
    if _interpreter is None:
        return []

    input_details = _interpreter.get_input_details()
    output_details = _interpreter.get_output_details()

    _, in_h, in_w, _ = input_details[0]["shape"]
    img = Image.open(io.BytesIO(frame)).convert("RGB").resize((in_w, in_h))
    arr = np.array(img, dtype=np.uint8)[np.newaxis, :]

    _interpreter.set_tensor(input_details[0]["index"], arr)
    _interpreter.invoke()

    boxes = _interpreter.get_tensor(output_details[0]["index"])[0]
    classes = _interpreter.get_tensor(output_details[1]["index"])[0]
    scores = _interpreter.get_tensor(output_details[2]["index"])[0]
    num = int(_interpreter.get_tensor(output_details[3]["index"])[0])

    results = []
    for i in range(num):
        score = float(scores[i])
        if score < 0.5:
            continue
        # The TFLite_Detection_PostProcess op's class ids are background-
        # exclusive (0 == the first real category), but _labels[0] is the
        # "???" background placeholder from the standard 91-entry COCO
        # label map — so the real label is one index further in, e.g.
        # class_id 0 -> _labels[1] == "person". Skipping this +1 silently
        # mislabeled every detection (verified: cats detected as "bird",
        # people as "???").
        class_id = int(classes[i]) + 1
        if class_id >= len(_labels):
            continue
        label = _labels[class_id]
        if label == "???":
            # One of COCO's 11 retired/placeholder category slots — never
            # a meaningful detection, so not worth showing.
            continue
        results.append({
            "label": label,
            "score": round(score, 2),
            "box": [float(v) for v in boxes[i]],  # [y1, x1, y2, x2] normalized
        })
    return results


def _nms(boxes_xyxy, scores, iou_threshold: float) -> list[int]:
    """Greedy single-class non-max suppression. boxes_xyxy: (N,4) array of
    [x1,y1,x2,y2]; returns indices to keep, highest score first.

    Suppresses on max(IoU, overlap-over-smaller-area), not plain IoU alone —
    a large low-quality box that fully *contains* a smaller good one has low
    IoU (union is dominated by the large box) but is still a duplicate of
    the same object, and plain-IoU NMS lets both through."""
    order = scores.argsort()[::-1]
    areas = (boxes_xyxy[:, 2] - boxes_xyxy[:, 0]) * (boxes_xyxy[:, 3] - boxes_xyxy[:, 1])
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(boxes_xyxy[i, 0], boxes_xyxy[rest, 0])
        yy1 = np.maximum(boxes_xyxy[i, 1], boxes_xyxy[rest, 1])
        xx2 = np.minimum(boxes_xyxy[i, 2], boxes_xyxy[rest, 2])
        yy2 = np.minimum(boxes_xyxy[i, 3], boxes_xyxy[rest, 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        overlap_ratio = inter / (np.minimum(areas[i], areas[rest]) + 1e-9)
        order = rest[np.maximum(iou, overlap_ratio) <= iou_threshold]
    return keep


def _run_lego_inference(frame: bytes) -> list[dict]:
    """Decodes the custom single-class YOLOv8 lego_minifigure.tflite model
    — a raw (NMS-less) export, unlike the COCO model's built-in
    TFLite_Detection_PostProcess op, so thresholding/NMS happen here.
    Input is channel-first (NCHW) float32 0-1, per this model's own export
    (see scripts/train_lego_detector.ipynb) — not the NHWC uint8 the COCO
    model above uses, so this does not share _run_inference's preprocessing.
    """
    if _lego_interpreter is None:
        return []

    input_details = _lego_interpreter.get_input_details()
    output_details = _lego_interpreter.get_output_details()

    _, _, in_h, in_w = input_details[0]["shape"]
    img = Image.open(io.BytesIO(frame)).convert("RGB").resize((in_w, in_h))
    arr = np.array(img, dtype=np.float32) / 255.0
    arr = np.transpose(arr, (2, 0, 1))[np.newaxis, :]  # HWC -> NCHW

    _lego_interpreter.set_tensor(input_details[0]["index"], arr)
    _lego_interpreter.invoke()

    raw = _lego_interpreter.get_tensor(output_details[0]["index"])[0]  # [5, N]
    scores = raw[4]
    # Trained on 481 clean, single-figure product photos — a busy real
    # room scene is out of distribution, and the model occasionally fires
    # a high-confidence box spanning most of the frame. A minifigure held
    # up to the camera realistically doesn't fill more than ~40% of it, so
    # drop candidates bigger than that before NMS even sees them.
    mask = (scores >= 0.3) & (raw[2] * raw[3] <= 0.4)
    if not mask.any():
        return []

    cx, cy, w, h = raw[0, mask], raw[1, mask], raw[2, mask], raw[3, mask]
    scores = scores[mask]
    boxes_xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    boxes_xyxy = np.clip(boxes_xyxy, 0.0, 1.0)

    keep = _nms(boxes_xyxy, scores, iou_threshold=0.45)
    results = []
    for i in keep:
        x1, y1, x2, y2 = boxes_xyxy[i]
        results.append({
            "label": "lego_minifigure",
            "score": round(float(scores[i]), 2),
            "box": [float(y1), float(x1), float(y2), float(x2)],  # match _run_inference's [y1,x1,y2,x2]
        })
    return results


def _detection_loop() -> None:
    global _detections, _running

    while _running:
        with _lock:
            is_enabled = enabled
        if not is_enabled:
            time.sleep(0.2)
            continue
        frame = camera_driver.get_frame()
        if frame is None:
            time.sleep(0.5)
            continue
        try:
            found = _run_inference(frame) + _run_lego_inference(frame)
            with _lock:
                _detections = found
        except Exception as exc:
            _log.warning("inference error: %s", exc)
            time.sleep(2.0)
        time.sleep(0.5)


def start() -> None:
    global _running, enabled
    if not available or _running:
        return
    from app import db  # local import — db not available at module import time
    enabled = db.get_setting("ml_detection_enabled") == "true"
    _running = True
    threading.Thread(target=_detection_loop, daemon=True).start()


def stop() -> None:
    global _running
    _running = False


def set_enabled(value: bool) -> None:
    # Memory-only: persistence is the caller's responsibility.
    global enabled
    with _lock:
        enabled = value


def get_detections() -> list[dict]:
    with _lock:
        return list(_detections)
