import logging
import threading
import time
from pathlib import Path

import pytest

from houston import __main__ as hmain

TOML = """
[[rover]]
name = "Fred"
unit = 2
ssh = "a@127.0.0.1"
app_url = "http://127.0.0.1:1"
app_user = "fred"
password_file = "pw"
voice = "voices/reed_mx.wav"

[[rover]]
name = "Eva"
unit = 1
ssh = "b@127.0.0.1"
app_url = "http://127.0.0.1:2"
app_user = "eva"
password_file = "pw"
voice = "voices/paulina_mx.wav"
"""


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "rovers.toml"
    cfg.write_text(TOML)
    made = []

    class FakeVoice:
        def __init__(self):
            self.synths = []

        def synth(self, text, wav):
            self.synths.append(wav)
            return b""

    class FakeBrain:
        def __init__(self, settings):
            self.settings = settings

    class FakeSession:
        crash_first = set()

        def __init__(self, rover, settings, brain, voice, transcriber):
            self.rover, self.brain, self.voice = rover, brain, voice
            self.transcriber = transcriber
            self.stopped = threading.Event()
            self.started = threading.Event()
            made.append(self)

        def run(self):
            self.started.set()
            if self.rover.name in self.crash_first:
                self.crash_first.discard(self.rover.name)
                raise RuntimeError("boom")
            self.stopped.wait()

        def stop(self):
            self.stopped.set()

    monkeypatch.setattr(hmain, "RoverSession", FakeSession)
    monkeypatch.setattr(hmain, "Voice", FakeVoice)
    monkeypatch.setattr(hmain, "Brain", FakeBrain)
    return cfg, made, FakeSession, tmp_path / "houston.log"


def run_main(cfg, log, stop, **kw):
    result = []
    t = threading.Thread(target=lambda: result.append(
        hmain.main(["--config", str(cfg), "--log-file", str(log)], stop=stop, **kw)))
    t.start()
    return t, result


def test_one_session_per_rover_with_shared_voice_and_brain(env):
    cfg, made, _, log = env
    stop = threading.Event()
    t, result = run_main(cfg, log, stop)
    assert wait_for(lambda: len(made) == 2 and all(s.started.is_set() for s in made))
    assert {s.rover.name for s in made} == {"Fred", "Eva"}
    assert made[0].voice is made[1].voice and made[0].brain is made[1].brain
    assert made[0].transcriber is not None
    stop.set()
    t.join(5)
    assert not t.is_alive() and result == [0]
    assert all(s.stopped.is_set() for s in made)


def test_voice_preloaded_in_background(env):
    cfg, made, _, log = env
    stop = threading.Event()
    t, _ = run_main(cfg, log, stop)
    assert wait_for(lambda: len(made) == 2)
    assert wait_for(lambda: made[0].voice.synths)
    stop.set()
    t.join(5)


def test_crashed_session_is_logged_and_restarted_others_keep_running(env):
    cfg, made, Fake, log = env
    Fake.crash_first = {"Fred"}
    stop = threading.Event()
    t, _ = run_main(cfg, log, stop, restart_delay=0.05)
    assert wait_for(lambda: len([s for s in made if s.rover.name == "Fred"]) == 2)
    eva = [s for s in made if s.rover.name == "Eva"]
    assert len(eva) == 1 and not eva[0].stopped.is_set()
    stop.set()
    t.join(5)
    assert not t.is_alive()
    for h in logging.getLogger().handlers:
        h.flush()
    text = log.read_text()
    assert "Fred" in text and "boom" in text and "restart" in text


def test_stop_during_restart_wait_exits_promptly(env):
    cfg, made, Fake, log = env
    Fake.crash_first = {"Fred"}
    stop = threading.Event()
    t, result = run_main(cfg, log, stop, restart_delay=60)
    assert wait_for(lambda: any(s.started.is_set() for s in made))
    time.sleep(0.1)
    stop.set()
    t.join(5)
    assert not t.is_alive() and result == [0]
    assert len([s for s in made if s.rover.name == "Fred"]) == 1


def test_no_rovers_is_an_error(env, tmp_path):
    cfg, _, _, log = env
    empty = tmp_path / "empty.toml"
    empty.write_text("[settings]\n")
    assert hmain.main(["--config", str(empty), "--log-file", str(log)], stop=threading.Event()) == 2
