"""GPU (TensorRT) detection: the worker protocol, backend choice, YOLO COCO
decoding and the fallback to TFLite. No GPU needed — a fake worker speaks
the same protocol as app/hardware/trt_worker.py."""
import sys
import textwrap

import numpy as np
import pytest

from app.hardware import ml_driver
from app.hardware.trt_client import TrtError, TrtWorker

FAKE_WORKER = textwrap.dedent('''
    import json, os, struct, sys
    H = struct.Struct("<II")
    out = os.fdopen(os.dup(1), "wb", 0); os.dup2(2, 1)
    inp = os.fdopen(0, "rb", 0)
    mode = sys.argv[1]
    if mode == "refuse":
        out.write(b'{"ok": false, "error": "engine built for another GPU"}\\n'); sys.exit(1)
    out.write((json.dumps({"ok": True, "engines": [
        {"input": [1, 3, 4, 4], "output": [1, 2, 3]},
        {"input": [1, 3, 4, 4], "output": [1, 2, 3]}]}) + "\\n").encode())
    while True:
        h = inp.read(H.size)
        if not h:
            break
        i, n = H.unpack(h)
        img = inp.read(n)
        if mode == "die":
            sys.exit(3)
        if i == 1:
            msg = b"execution failed"; out.write(H.pack(1, len(msg)) + msg); continue
        # echo something derived from the input so the round trip is checked
        res = struct.pack("<6f", *([float(sum(img))] * 6))
        out.write(H.pack(0, len(res)) + res)
''')


@pytest.fixture
def fake_worker(tmp_path):
    path = tmp_path / "fake_worker.py"
    path.write_text(FAKE_WORKER)
    workers = []

    def start(mode="ok"):
        w = TrtWorker([mode], python=sys.executable, worker=path, timeout=20)
        workers.append(w)
        return w

    yield start
    for w in workers:
        w.close()


def test_infer_round_trip_and_output_shape(fake_worker):
    w = fake_worker()
    assert w.input_size(0) == 4
    out = w.infer(0, np.ones((4, 4, 3), dtype=np.uint8))
    assert out.shape == (1, 2, 3)
    assert out[0, 0, 0] == 48.0  # 4·4·3 ones, summed by the worker


def test_engine_error_is_reported_and_worker_keeps_going(fake_worker):
    w = fake_worker()
    with pytest.raises(TrtError, match="execution failed"):
        w.infer(1, np.zeros((4, 4, 3), dtype=np.uint8))
    assert w.infer(0, np.zeros((4, 4, 3), dtype=np.uint8)).shape == (1, 2, 3)


def test_worker_that_refuses_to_start_raises(fake_worker):
    with pytest.raises(TrtError, match="another GPU"):
        fake_worker("refuse")


def test_worker_dying_mid_request_raises(fake_worker):
    w = fake_worker("die")
    with pytest.raises(TrtError, match="died"):
        w.infer(0, np.zeros((4, 4, 3), dtype=np.uint8))
    w._proc.wait(timeout=5)  # its pipe closes a moment before it's reaped
    assert not w.alive


def test_real_worker_without_tensorrt_fails_cleanly():
    # this machine has no TensorRT: the real worker must say so, not hang
    pytest.importorskip("numpy")
    try:
        import tensorrt  # noqa: F401
        pytest.skip("TensorRT is installed here")
    except ImportError:
        pass
    with pytest.raises(TrtError, match="tensorrt"):
        TrtWorker(["missing.engine"], python=sys.executable, timeout=20)


@pytest.mark.parametrize("requested, board, engines, expected", [
    ("auto", "jetson", True, True),
    ("auto", "jetson", False, False),        # engines not built yet → CPU
    ("auto", "raspberry_pi", True, False),   # no TensorRT on a Pi
    ("tflite", "jetson", True, False),
    ("tensorrt", "unknown", False, True),    # forced: try, fall back if it fails
])
def test_backend_choice(requested, board, engines, expected):
    assert ml_driver.wants_tensorrt(requested, board, engines) is expected


def _coco_raw(*boxes, classes=80):
    """[4+classes, N] from (cx, cy, w, h, class_id, score) in square pixels."""
    raw = np.zeros((4 + classes, len(boxes)), dtype=np.float32)
    for i, (cx, cy, w, h, c, s) in enumerate(boxes):
        raw[:4, i] = (cx, cy, w, h)
        raw[4 + c, i] = s
    return raw


LABELS = ["person", "bicycle", "car"] + [f"c{i}" for i in range(3, 80)]


def test_coco_decode_labels_threshold_and_frame_coordinates():
    # 640×480 letterboxed into 416: scale 0.65, 52 px bars top and bottom
    raw = _coco_raw((208, 208, 104, 156, 0, 0.9),   # person in the middle
                    (50, 100, 20, 20, 2, 0.3))      # car below the 0.5 threshold
    [det] = ml_driver._decode_yolo_coco(raw, LABELS, 416, 0.65, 0, 52, 640, 480)
    assert det["label"] == "person" and det["score"] == 0.9
    y1, x1, y2, x2 = det["box"]
    # 104×156 square px = 160×240 frame px, centred: x 240–400, y 120–360
    assert (x1, x2) == pytest.approx((240 / 640, 400 / 640), abs=1e-3)
    assert (y1, y2) == pytest.approx((120 / 480, 360 / 480), abs=1e-3)


def test_coco_nms_is_per_class():
    raw = _coco_raw((208, 208, 100, 100, 0, 0.9),
                    (210, 208, 100, 100, 0, 0.8),   # duplicate person → suppressed
                    (210, 208, 100, 100, 1, 0.7))   # bicycle in the same spot → kept
    labels = sorted(d["label"] for d in ml_driver._decode_yolo_coco(raw, LABELS, 416, 0.65, 0, 52, 640, 480))
    assert labels == ["bicycle", "person"]


class _BrokenWorker:
    def input_size(self, _i):
        return 416

    def infer(self, *_a):
        raise TrtError("TensorRT worker died: broken pipe")

    def close(self):
        self.closed = True


def test_gpu_failure_falls_back_to_tflite(monkeypatch):
    import io
    from PIL import Image

    broken = _BrokenWorker()
    monkeypatch.setattr(ml_driver, "_trt", broken)
    monkeypatch.setattr(ml_driver, "_trt_coco", 0)
    monkeypatch.setattr(ml_driver, "_trt_lego", None)
    monkeypatch.setattr(ml_driver, "backend", "tensorrt")
    monkeypatch.setattr(ml_driver, "_run_inference", lambda f: [{"label": "cpu", "score": 1, "box": [0, 0, 1, 1]}])
    monkeypatch.setattr(ml_driver, "_run_lego_inference", lambda f: [])
    buf = io.BytesIO()
    Image.new("RGB", (64, 48)).save(buf, format="JPEG")

    assert ml_driver._detect(buf.getvalue()) == [{"label": "cpu", "score": 1, "box": [0, 0, 1, 1]}]
    assert ml_driver._trt is None and broken.closed
    assert ml_driver.backend != "tensorrt"
