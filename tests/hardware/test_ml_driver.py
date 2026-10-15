from app.hardware import ml_driver


def test_available_is_bool():
    assert isinstance(ml_driver.available, bool)


def test_get_detections_returns_list():
    result = ml_driver.get_detections()
    assert isinstance(result, list)


def test_set_enabled_flips_flag():
    ml_driver.set_enabled(True)
    assert ml_driver.enabled is True
    ml_driver.set_enabled(False)
    assert ml_driver.enabled is False


def test_start_does_not_crash_when_unavailable():
    if not ml_driver.available:
        ml_driver.start()  # must not raise


def test_stop_does_not_crash():
    ml_driver.stop()  # must not raise regardless of state


# ── Lego detector: letterbox preprocessing ────────────────────────────
import io  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

PIL = pytest.importorskip("PIL.Image")


def test_letterbox_keeps_aspect_ratio_and_pads_grey():
    img = PIL.new("RGB", (640, 480), (255, 0, 0))
    square, scale, pad_x, pad_y = ml_driver.letterbox(img, 320)
    assert square.size == (320, 320)
    assert scale == 0.5
    assert (pad_x, pad_y) == (0, 40)  # 640×480 → 320×240, centred
    assert square.getpixel((160, 10)) == (114, 114, 114)   # top bar
    assert square.getpixel((160, 160)) == (255, 0, 0)      # the frame itself
    assert square.getpixel((160, 310)) == (114, 114, 114)  # bottom bar


def _raw(*boxes):
    """[5, N] model output from (cx, cy, w, h, score) tuples, square-relative."""
    return np.array(boxes, dtype=np.float32).T


def test_lego_boxes_are_mapped_back_to_the_frame():
    # 640×480 frame letterboxed into 320: scale 0.5, 40 px bars top/bottom.
    # A box in the middle of the square, 80×120 px (in square pixels)…
    raw = _raw((0.5, 0.5, 80 / 320, 120 / 320, 0.9))
    [det] = ml_driver._decode_lego(raw, 320, 0.5, 0, 40, 640, 480)
    y1, x1, y2, x2 = det["box"]
    # …is 160×240 frame px centred at (320, 240): x 240–400, y 120–360
    assert (x1, x2) == pytest.approx((240 / 640, 400 / 640))
    assert (y1, y2) == pytest.approx((120 / 480, 360 / 480))
    assert det["label"] == "lego_minifigure" and det["score"] == 0.9


def test_lego_box_in_padding_is_clipped_to_the_frame():
    # centred on the top grey bar (square y 20 px) — mostly outside the frame
    raw = _raw((0.5, 20 / 320, 40 / 320, 60 / 320, 0.8))
    [det] = ml_driver._decode_lego(raw, 320, 0.5, 0, 40, 640, 480)
    y1, _, y2, _ = det["box"]
    assert y1 == 0.0 and 0 < y2 < 0.1


def test_lego_low_scores_and_huge_boxes_are_dropped():
    raw = _raw((0.5, 0.5, 0.2, 0.2, 0.1),   # below the 0.3 threshold
               (0.5, 0.5, 1.0, 0.75, 0.95))  # fills the whole frame
    assert ml_driver._decode_lego(raw, 320, 0.5, 0, 40, 640, 480) == []


class FakeLegoInterpreter:
    """Records the input tensor; returns one confident box in the middle."""

    def __init__(self, shape):
        self.shape = shape
        self.fed = None

    def get_input_details(self):
        return [{"shape": np.array(self.shape), "index": 0}]

    def get_output_details(self):
        return [{"index": 1}]

    def set_tensor(self, _index, arr):
        self.fed = arr

    def invoke(self):
        pass

    def get_tensor(self, _index):
        return np.array([[[0.5], [0.5], [0.1], [0.1], [0.9]]], dtype=np.float32)


@pytest.mark.parametrize("shape, fed_shape", [
    ((1, 3, 320, 320), (1, 3, 320, 320)),   # older NCHW export
    ((1, 416, 416, 3), (1, 416, 416, 3)),   # current Ultralytics NHWC export
])
def test_lego_inference_handles_both_tensor_layouts(monkeypatch, shape, fed_shape):
    fake = FakeLegoInterpreter(shape)
    monkeypatch.setattr(ml_driver, "_lego_interpreter", fake)
    buf = io.BytesIO()
    PIL.new("RGB", (640, 480), (10, 20, 30)).save(buf, format="JPEG")
    [det] = ml_driver._run_lego_inference(buf.getvalue())
    assert fake.fed.shape == fed_shape
    assert fake.fed.dtype == np.float32 and fake.fed.max() <= 1.0
    assert det["label"] == "lego_minifigure"


def test_lego_pixel_coordinates_are_treated_like_normalized_ones():
    # same box as test_lego_boxes_are_mapped_back_to_the_frame, in square pixels
    norm = _raw((0.5, 0.5, 80 / 320, 120 / 320, 0.9))
    px = _raw((160, 160, 80, 120, 0.9))
    a = ml_driver._decode_lego(norm, 320, 0.5, 0, 40, 640, 480)
    b = ml_driver._decode_lego(px, 320, 0.5, 0, 40, 640, 480)
    assert a[0]["box"] == pytest.approx(b[0]["box"])


# ── Detection speed (Telemetry tab) ───────────────────────────────────

def test_stats_reports_passes_per_second_and_mean_latency(monkeypatch):
    import collections

    passes = collections.deque([(100.0, 0.030), (101.0, 0.040), (109.5, 0.050),
                                (85.0, 0.900)])  # the last one is outside the 10 s window
    monkeypatch.setattr(ml_driver, "_passes", passes)
    monkeypatch.setattr(ml_driver, "_running", True)
    monkeypatch.setattr(ml_driver, "enabled", True)
    monkeypatch.setattr(ml_driver, "backend", "tensorrt")
    monkeypatch.setattr(ml_driver, "_last_wanted", 109.0)
    s = ml_driver.stats(now=110.0)
    assert s == {"enabled": True, "paused": False, "backend": "tensorrt", "label": "GPU",
                 "fps": 0.3, "latency_ms": 40}


def test_detection_pauses_when_nothing_reads_it(monkeypatch):
    monkeypatch.setattr(ml_driver, "_last_wanted", float("-inf"))
    assert not ml_driver.watched()
    ml_driver.get_shown_detections()       # a camera view polling for boxes
    assert ml_driver.watched()
    later = ml_driver._last_wanted + ml_driver._IDLE_AFTER + 1
    assert not ml_driver.watched(now=later)
    ml_driver.get_detections()             # auto-drive asking for obstacles
    assert ml_driver.watched()


def test_stats_say_paused_while_unwatched(monkeypatch):
    import collections

    monkeypatch.setattr(ml_driver, "_passes", collections.deque())
    monkeypatch.setattr(ml_driver, "enabled", True)
    monkeypatch.setattr(ml_driver, "_last_wanted", 50.0)
    assert ml_driver.stats(now=100.0)["paused"] is True
    monkeypatch.setattr(ml_driver, "enabled", False)
    assert ml_driver.stats(now=100.0)["paused"] is False


def test_stats_when_detection_is_off(monkeypatch):
    import collections

    monkeypatch.setattr(ml_driver, "_passes", collections.deque([(100.0, 0.5)]))
    monkeypatch.setattr(ml_driver, "enabled", False)
    monkeypatch.setattr(ml_driver, "backend", "tflite")
    s = ml_driver.stats(now=101.0)
    assert s["enabled"] is False and s["fps"] == 0.0 and s["latency_ms"] is None
    assert s["label"] == "CPU"


def test_record_pass_feeds_prometheus(monkeypatch):
    import collections
    from app import metrics

    monkeypatch.setattr(ml_driver, "_passes", collections.deque(maxlen=10))
    monkeypatch.setattr(ml_driver, "backend", "tflite")
    before = metrics.ML_INFERENCE_SECONDS.labels(backend="tflite")._sum.get()
    ml_driver._record_pass(0.25)
    assert metrics.ML_INFERENCE_SECONDS.labels(backend="tflite")._sum.get() == pytest.approx(before + 0.25)
    assert len(ml_driver._passes) == 1


# ── What gets shown: people + minifigures, or minifigures only ────────

_MIXED = [
    {"label": "person", "score": 0.9, "box": [0, 0, 1, 1]},
    {"label": "chair", "score": 0.8, "box": [0, 0, 1, 1]},
    {"label": "lego_minifigure", "score": 0.95, "box": [0, 0, 1, 1]},
]


def test_default_shows_people_and_minifigures_but_autodrive_gets_all(monkeypatch):
    monkeypatch.setattr(ml_driver, "_detections", list(_MIXED))
    monkeypatch.setattr(ml_driver, "lego_only", False)
    assert [d["label"] for d in ml_driver.get_shown_detections()] == ["person", "lego_minifigure"]
    assert len(ml_driver.get_detections()) == 3  # obstacle avoidance still sees the chair


def test_lego_only_shows_only_minifigures_and_drops_stale_boxes(monkeypatch):
    monkeypatch.setattr(ml_driver, "_detections", list(_MIXED))
    monkeypatch.setattr(ml_driver, "lego_only", False)
    ml_driver.set_lego_only(True)
    try:
        assert [d["label"] for d in ml_driver.get_shown_detections()] == ["lego_minifigure"]
        assert [d["label"] for d in ml_driver.get_detections()] == ["lego_minifigure"]
    finally:
        ml_driver.set_lego_only(False)


def test_lego_only_skips_the_coco_model_on_the_cpu(monkeypatch):
    calls = []
    monkeypatch.setattr(ml_driver, "_trt", None)
    monkeypatch.setattr(ml_driver, "_run_inference", lambda f: calls.append("coco") or [])
    monkeypatch.setattr(ml_driver, "_run_lego_inference", lambda f: calls.append("lego") or [])
    monkeypatch.setattr(ml_driver, "lego_only", True)
    ml_driver._detect(b"frame")
    assert calls == ["lego"]
    monkeypatch.setattr(ml_driver, "lego_only", False)
    ml_driver._detect(b"frame")
    assert calls == ["lego", "coco", "lego"]


def test_lego_only_skips_the_coco_engine_on_the_gpu(monkeypatch):
    run = []

    class Worker:
        def input_size(self, i):
            return 32

        def infer(self, i, arr):
            run.append(i)
            return np.zeros((1, 5, 4) if i == 1 else (1, 84, 4), dtype=np.float32)

    buf = io.BytesIO()
    PIL.new("RGB", (64, 48)).save(buf, format="JPEG")
    monkeypatch.setattr(ml_driver, "_trt", Worker())
    monkeypatch.setattr(ml_driver, "_trt_coco", 0)
    monkeypatch.setattr(ml_driver, "_trt_lego", 1)
    monkeypatch.setattr(ml_driver, "lego_only", True)
    ml_driver._run_trt(buf.getvalue())
    assert run == [1]
