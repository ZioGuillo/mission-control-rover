import io

from PIL import Image

from app.routes.camera import _annotate


def _make_jpeg(width: int = 100, height: int = 100) -> bytes:
    img = Image.new("RGB", (width, height), color=(80, 80, 80))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_annotate_no_detections_returns_unchanged():
    frame = _make_jpeg()
    result = _annotate(frame, [])
    assert result == frame


def test_annotate_single_detection_returns_valid_jpeg():
    frame = _make_jpeg()
    detections = [{"label": "person", "score": 0.91, "box": [0.1, 0.1, 0.8, 0.8]}]
    result = _annotate(frame, detections)
    assert isinstance(result, bytes)
    img = Image.open(io.BytesIO(result))
    assert img.size == (100, 100)
    orig = Image.open(io.BytesIO(frame))
    assert orig.tobytes() != img.tobytes()


def test_annotate_multiple_detections():
    frame = _make_jpeg()
    detections = [
        {"label": "person", "score": 0.9, "box": [0.0, 0.0, 0.5, 0.5]},
        {"label": "dog", "score": 0.7, "box": [0.5, 0.5, 1.0, 1.0]},
    ]
    result = _annotate(frame, detections)
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_annotate_clamps_box_to_image_bounds():
    frame = _make_jpeg()
    detections = [{"label": "car", "score": 0.6, "box": [-0.1, -0.1, 1.2, 1.2]}]
    result = _annotate(frame, detections)
    assert isinstance(result, bytes)
