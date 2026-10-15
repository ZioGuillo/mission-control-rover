"""Helpers of the offline training scripts in scripts/lego_training/."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lego_training"))
from build_dataset import kept_boxes, yolo_line  # noqa: E402
from evaluate import iou, match  # noqa: E402


def test_kept_boxes_filters_by_score_but_keeps_hand_drawn_ones():
    entry = {"boxes": [[10, 200, 20, 230, 0.6], [30, 200, 40, 230, 0.2], [50, 200, 60, 230]]}
    assert kept_boxes(entry, min_score=0.35) == [[10, 200, 20, 230], [50, 200, 60, 230]]


def test_kept_boxes_drops_the_ignored_band_and_skipped_frames():
    entry = {"boxes": [[10, 90, 20, 120, 0.9], [10, 300, 20, 330, 0.9]]}
    assert kept_boxes(entry, 0.35, ignore_above_y=150) == [[10, 300, 20, 330]]
    assert kept_boxes({**entry, "skip": True}, 0.35) == []


def test_yolo_line_is_normalized_centre_and_size():
    assert yolo_line([160, 120, 480, 360], 640, 480) == "0 0.500000 0.500000 0.500000 0.500000"


def test_iou():
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == pytest.approx(1.0)
    assert iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(50 / 150)
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0


def test_match_counts_hits_misses_and_false_boxes():
    gt = [[0, 0, 10, 10], [100, 100, 110, 110]]
    preds = [([1, 0, 10, 10], 0.9), ([50, 50, 60, 60], 0.4)]
    hits, extra = match(gt, preds)
    assert hits == [0.9, None]   # first found, second missed
    assert extra == [0.4]         # the box over nothing is a false one


def test_one_prediction_cant_count_for_two_labels():
    gt = [[0, 0, 10, 10], [1, 0, 11, 10]]
    hits, _ = match(gt, [([0, 0, 10, 10], 0.9)])
    assert hits.count(None) == 1
