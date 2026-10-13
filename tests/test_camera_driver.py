"""Hot-plug behaviour of the OpenCV (USB/V4L2) camera backend: unplugging the
camera must mark it unavailable and drop the stale frame; plugging it back
in must be picked up without restarting the app."""
import threading
import time

import numpy as np
import pytest

import app.hardware.camera_driver as cam

FRAME = np.zeros((48, 64, 3), dtype=np.uint8)


class FakeCapture:
    def __init__(self, good_reads):
        self.good_reads = good_reads
        self.released = False

    def read(self):
        if self.good_reads > 0:
            self.good_reads -= 1
            return True, FRAME
        return False, None  # unplugged: every read fails from here on

    def release(self):
        self.released = True


def _wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def opencv_backend(monkeypatch):
    """Run the OpenCV capture loop against a scripted sequence of cameras."""
    monkeypatch.setattr(cam, "_backend", "opencv")
    monkeypatch.setattr(cam, "_cv_cam", None)
    monkeypatch.setattr(cam, "available", False)
    monkeypatch.setattr(cam, "_latest_frame", None)
    monkeypatch.setattr(cam, "_READ_FAIL_LIMIT", 3)
    monkeypatch.setattr(cam, "_READ_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(cam, "_REDETECT_SECONDS", 0.005)
    opened = []

    def script(*cameras):
        queue = list(cameras)

        def fake_open():
            c = queue.pop(0) if queue else None
            if c is not None:
                opened.append(c)
            return c

        monkeypatch.setattr(cam, "_open_opencv", fake_open)

    yield script, opened
    cam.stop()
    time.sleep(0.05)


def test_unplug_marks_unavailable_and_drops_stale_frame(opencv_backend):
    script, opened = opencv_backend
    script(FakeCapture(good_reads=5))
    counter_before = cam._frame_counter
    cam.start()

    # the frame counter only ever grows, so this can't miss the short window
    # in which the 5 frames exist before the simulated unplug clears them
    assert _wait_for(lambda: cam._frame_counter >= counter_before + 5), "never captured a frame"
    # after the 5 good reads every read fails — the camera was unplugged
    assert _wait_for(lambda: cam.available is False)
    assert cam.get_frame() is None, "stale frame kept after unplug (ML would keep using it)"
    assert opened[0].released, "device not released, so a re-plug couldn't reopen it"


def test_replug_is_detected_without_restart(opencv_backend):
    script, _ = opencv_backend
    # plugged in, unplugged, two scans find nothing, then plugged back in
    script(FakeCapture(good_reads=2), None, None, FakeCapture(good_reads=10_000))
    cam.start()

    assert _wait_for(lambda: cam.available is False)
    counter_when_unplugged = cam._frame_counter
    assert _wait_for(lambda: cam.available is True)
    assert _wait_for(lambda: cam._frame_counter > counter_when_unplugged)
    assert cam.get_frame() is not None


def test_camera_plugged_in_after_boot_is_picked_up(opencv_backend):
    """Booting with no camera attached used to mean no camera until restart."""
    script, _ = opencv_backend
    script(None, None, FakeCapture(good_reads=10_000))
    assert cam.available is False
    cam.start()
    assert _wait_for(lambda: cam.available is True)
    assert _wait_for(lambda: cam.get_frame() is not None)


def test_video_device_indexes_come_from_dev_video(monkeypatch):
    monkeypatch.setattr(cam.glob, "glob", lambda _p: ["/dev/video2", "/dev/video0", "/dev/video10"])
    assert cam._video_device_indexes() == [0, 2, 10]
