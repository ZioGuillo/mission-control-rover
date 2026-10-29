import logging
import threading
import time
from pathlib import Path

import pytest

from houston import __main__ as hmain
from houston.session import Airtime

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
        warm_error = None

        def __init__(self, settings):
            self.settings = settings
            self.warmed = threading.Event()

        def warm(self):
            self.warmed.set()
            if self.warm_error:
                raise self.warm_error

    class FakeSession:
        crash_first = set()

        def __init__(self, rover, settings, brain, voice, transcriber, *, airtime):
            self.rover, self.brain, self.voice = rover, brain, voice
            self.airtime = airtime
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
    assert isinstance(made[0].airtime, Airtime) and made[0].airtime is made[1].airtime
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


def test_no_rovers_logs_error_and_exits_cleanly(env, tmp_path):
    cfg, _, _, log = env
    empty = tmp_path / "empty.toml"
    empty.write_text("[settings]\n")
    # exit 0: launchd (KeepAlive SuccessfulExit=false) must not restart it every 10 s
    assert hmain.main(["--config", str(empty), "--log-file", str(log)], stop=threading.Event()) == 0
    assert "no rovers" in log.read_text()


def test_llm_warmed_up_in_background(env):
    cfg, made, _, log = env
    stop = threading.Event()
    t, _ = run_main(cfg, log, stop)
    assert wait_for(lambda: len(made) == 2)
    assert made[0].brain.warmed.wait(3)
    stop.set()
    t.join(5)


def test_llm_warm_up_failure_is_logged_not_fatal(env):
    cfg, made, _, log = env
    hmain.Brain.warm_error = RuntimeError("ollama down")
    stop = threading.Event()
    t, _ = run_main(cfg, log, stop)
    assert wait_for(lambda: len(made) == 2 and made[0].brain.warmed.is_set())
    assert wait_for(lambda: "ollama down" in log.read_text())
    assert all(not s.stopped.is_set() for s in made)  # sessions keep running
    stop.set()
    t.join(5)
    assert not t.is_alive()


def test_startup_failure_is_logged_and_returns_1(env, monkeypatch):
    cfg, _, _, log = env

    def boom(settings):
        raise RuntimeError("no ollama")

    monkeypatch.setattr(hmain, "Brain", boom)
    assert hmain.main(["--config", str(cfg), "--log-file", str(log)], stop=threading.Event()) == 1
    assert "no ollama" in log.read_text()


def test_bad_config_is_logged_and_returns_1(env, tmp_path):
    _, _, _, log = env
    bad = tmp_path / "bad.toml"
    bad.write_text("[[rover]\n")
    assert hmain.main(["--config", str(bad), "--log-file", str(log)], stop=threading.Event()) == 1
    assert "cannot load" in log.read_text()
