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
