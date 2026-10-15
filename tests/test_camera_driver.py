"""Hot-plug behaviour of the OpenCV (USB/V4L2) camera backend: unplugging the
camera must mark it unavailable and drop the stale frame; plugging it back
in must be picked up without restarting the app."""
import sys
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


# ── Per-board backend choice ──────────────────────────────────────────

def test_auto_backend_follows_the_board():
    assert cam.backend_order("auto", "jetson") == ("argus", "opencv")
    assert cam.backend_order("auto", "raspberry_pi") == ("picamera2", "opencv")
    assert cam.backend_order("auto", "unknown") == ("picamera2", "opencv")


def test_camera_backend_setting_overrides_the_board():
    assert cam.backend_order("opencv", "jetson") == ("opencv",)
    assert cam.backend_order("argus", "raspberry_pi") == ("argus",)


def test_fps_defaults_per_board_unless_set(monkeypatch):
    monkeypatch.setattr(cam.settings, "camera_fps", None)
    assert cam.camera_fps("jetson") == 15
    assert cam.camera_fps("raspberry_pi") == 10
    monkeypatch.setattr(cam.settings, "camera_fps", 15)
    assert cam.camera_fps("jetson") == 15


def test_blank_camera_fps_in_env_means_board_default(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("CAMERA_FPS", "")
    assert Settings(_env_file=None).camera_fps is None


# ── Argus (Jetson CSI) backend ────────────────────────────────────────

JPEG_A = b"\xff\xd8\xff\xdbAAAA\xff\x00\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xdbBBBB\xff\xd9"


def test_split_jpegs_keeps_partial_frame_for_next_chunk():
    frames, rest = cam.split_jpegs(b"GST_ARGUS: Running\n" + JPEG_A + JPEG_B[:6])
    assert frames == [JPEG_A]
    assert rest == JPEG_B[:6]
    frames, rest = cam.split_jpegs(rest + JPEG_B[6:])
    assert frames == [JPEG_B]
    assert rest == b""


def test_argus_sensor_found_only_for_jetson_csi_camera(monkeypatch, tmp_path):
    (tmp_path / "video0").mkdir()
    name = tmp_path / "video0" / "name"
    monkeypatch.setattr(cam.shutil, "which", lambda _c: "/usr/bin/gst-launch-1.0")
    monkeypatch.setattr(cam.glob, "glob", lambda _p: [str(name)])

    name.write_text("vi-output, imx219 8-0010\n")
    assert cam._argus_sensor_id() == 0
    name.write_text("USB 2.0 Camera\n")  # a webcam is OpenCV's job
    assert cam._argus_sensor_id() is None


def test_argus_needs_gstreamer(monkeypatch):
    monkeypatch.setattr(cam.shutil, "which", lambda _c: None)
    assert cam._argus_sensor_id() is None


def test_argus_cmd_writes_jpegs_to_the_given_fd():
    cmd = cam._argus_cmd(0, 30, 7)
    assert cmd[:4] == ["gst-launch-1.0", "-q", "nvarguscamerasrc", "sensor-id=0"]
    assert "framerate=30/1" in " ".join(cmd)
    assert cmd[-1] == "fd=7"


@pytest.fixture
def argus_backend(monkeypatch):
    """Run the Argus loop against a fake gst-launch: a Python child that
    writes log noise to stdout and JPEGs to the pipe, like the real one."""
    monkeypatch.setattr(cam, "_backend", "argus")
    monkeypatch.setattr(cam, "_argus_sensor", 0)
    monkeypatch.setattr(cam, "available", True)
    monkeypatch.setattr(cam, "_latest_frame", None)
    monkeypatch.setattr(cam, "_REDETECT_SECONDS", 0.01)
    monkeypatch.setattr(cam, "_IDLE_AFTER", 1e9)  # always watched unless a test says otherwise
    cam.want()
    runs = []

    def script(child_code):
        def fake_cmd(_sensor, _fps, fd):
            runs.append(fd)
            return [sys.executable, "-c", child_code.replace("FD", str(fd))]

        monkeypatch.setattr(cam, "_argus_cmd", fake_cmd)

    yield script, runs
    cam.stop()
    time.sleep(0.1)


def test_argus_frames_are_published(argus_backend):
    script, _ = argus_backend
    script(
        "import os, time\n"
        "print('GST_ARGUS: Running with following settings')\n"
        f"os.write(FD, {JPEG_A!r} + {JPEG_B!r})\n"
        "time.sleep(30)\n"
    )
    before = cam._frame_counter
    cam.start()
    assert _wait_for(lambda: cam._frame_counter >= before + 2)
    assert cam.get_frame() == JPEG_B
    assert cam.available is True


def test_argus_pipeline_exit_drops_frame_and_restarts(argus_backend, monkeypatch):
    script, runs = argus_backend
    script(f"import os\nos.write(FD, {JPEG_A!r})\n")  # one frame, then exits
    states = []

    def record(value):
        # the frame must already be gone when the camera is marked unavailable
        states.append((value, cam._latest_frame))

    monkeypatch.setattr(cam, "_set_available", record)
    cam.start()
    assert _wait_for(lambda: len(runs) >= 2), "pipeline not restarted after exiting"
    assert _wait_for(lambda: len(states) >= 3)
    # connected → pipeline exited (frame dropped) → connected again
    assert [v for v, _ in states[:3]] == [True, False, True]
    assert states[1] == (False, None), "stale frame kept after the pipeline died"


def test_stop_terminates_argus_so_the_camera_is_freed(argus_backend):
    script, _ = argus_backend
    script(f"import os, time\nos.write(FD, {JPEG_A!r})\ntime.sleep(30)\n")
    cam.start()
    assert _wait_for(lambda: cam._argus_proc is not None and cam.get_frame() == JPEG_A)
    proc = cam._argus_proc
    cam.stop()
    assert _wait_for(lambda: proc.poll() is not None), "gst-launch kept running after stop()"


def test_argus_cmd_passes_colour_tuning(monkeypatch):
    monkeypatch.setattr(cam.settings, "camera_wb_mode", "daylight")
    monkeypatch.setattr(cam.settings, "camera_saturation", 0.8)
    monkeypatch.setattr(cam.settings, "camera_exposure_comp", 0.5)
    cmd = cam._argus_cmd(0, 30, 3)
    assert "wbmode=daylight" in cmd
    assert "saturation=0.8" in cmd
    assert "exposurecompensation=0.5" in cmd


def test_invalid_wb_mode_is_rejected(monkeypatch):
    from pydantic import ValidationError
    from app.config import Settings

    monkeypatch.setenv("CAMERA_WB_MODE", "pinkless")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_argus_stops_with_no_viewer_and_starts_again_on_demand(argus_backend, monkeypatch):
    script, runs = argus_backend
    script(f"import os, time\nos.write(FD, {JPEG_A!r})\ntime.sleep(30)\n")
    cam.start()
    assert _wait_for(lambda: cam.get_frame() == JPEG_A)
    proc = cam._argus_proc
    availability = []
    monkeypatch.setattr(cam, "_set_available", availability.append)

    monkeypatch.setattr(cam, "_IDLE_AFTER", 0.2)  # nobody reads frames any more
    cam._last_wanted = float("-inf")
    assert _wait_for(lambda: proc.poll() is not None), "pipeline kept running with no viewer"
    assert cam.idle() and cam._latest_frame is None
    assert False not in availability, "an idle camera must stay available"

    monkeypatch.setattr(cam, "_IDLE_AFTER", 1e9)
    assert cam.wait_for_frame(timeout=5) == JPEG_A, "camera didn't start again for a viewer"
    assert len(runs) == 2


def test_health_check_does_not_keep_the_camera_on():
    cam._last_wanted = float("-inf")
    cam.get_frame_if_new(-1, wanted=False)
    assert not cam.watched()
    cam.get_frame_if_new(-1)
    assert cam.watched()
