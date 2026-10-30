import dataclasses
import logging
import os
import re
import threading
from datetime import datetime
import time
from pathlib import Path

import pytest

from houston.brain import Brain
from houston.config import Rover, Settings
from houston.rover_link import LinkError
from houston.session import CLOSE_MIN_RMS, FILLERS, PAUSED_MAX_S, Airtime, RoverSession
from houston.stt import SttError
from houston.tests.test_vad import fluctuating_noise, noise, tone
from houston.vad import Utterance

FRED_WAV = Path("/voices/reed_mx.wav")
EVA_WAV = Path("/voices/paulina_mx.wav")
TAG = re.compile(r"<[^>]*>|\[[^\]]*\]")


# ---------------------------------------------------------------- fakes

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeSpeaker:
    def __init__(self, fail_on_write=None, on_write=None, prelude=b""):
        self.prelude = prelude
        self.writes: list[bytes] = [prelude] if prelude else []
        self.closed = False
        self._fail_on_write = fail_on_write
        self._on_write = on_write

    def write(self, pcm):
        if self._fail_on_write is not None and len(self.writes) + 1 >= self._fail_on_write:
            raise LinkError("speaker pipe broken: [Errno 32] Broken pipe")
        self.writes.append(pcm)
        if self._on_write:
            self._on_write(pcm)

    def close(self):
        self.closed = True

    def tags(self):
        return TAG.findall(b"".join(self.writes).decode())


class FakeProc:
    """`ssh ... arecord`: stdout is a pipe the test fills; kill() ends the stream."""

    def __init__(self, data=b"", keep_open=False):
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self._w = os.fdopen(w, "wb")
        self.stdin = self.stderr = None
        self.killed = False
        if data:
            threading.Thread(target=self._feed, args=(data, keep_open), daemon=True).start()
        elif not keep_open:
            self._w.close()

    def _feed(self, data, keep_open):
        try:
            self._w.write(data)
            self._w.flush()
            if not keep_open:
                self._w.close()
        except (OSError, ValueError):
            pass

    def kill(self):
        self.killed = True
        try:
            self._w.close()
        except OSError:
            pass

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0 if self.killed else None


class FakeLink:
    def __init__(self, speaker_fail_on_write=None, on_write=None, mics=()):
        self.speakers: list[FakeSpeaker] = []
        self.speaker_error = None
        self._fail = speaker_fail_on_write
        self._on_write = on_write
        self.mics = list(mics)  # FakeProc or exception, one per mic() call
        self.procs: list[FakeProc] = []
        self.boots = ["boot-1"]  # boot_id per call; the last one repeats

    def detect(self):
        return ("Device", "UACDemoV10")

    def boot_id(self):
        return self.boots.pop(0) if len(self.boots) > 1 else self.boots[0]

    def mic(self):
        if not self.mics:
            raise LinkError("ssh: connect to host: No route to host")
        m = self.mics.pop(0)
        if isinstance(m, Exception):
            raise m
        self.procs.append(m)
        return m

    def speaker(self, prelude=b""):
        if self.speaker_error:
            raise self.speaker_error
        s = FakeSpeaker(self._fail, self._on_write, prelude)
        self.speakers.append(s)
        return s

    def played(self):
        return [t for s in self.speakers for t in s.tags()]


class FakeVoice:
    def __init__(self):
        self.cached_calls: list[tuple[str, Path]] = []
        self.synth_calls: list[tuple[str, Path]] = []

    def synth(self, text, voice_wav):
        self.synth_calls.append((text, voice_wav))
        return f"[{text}]".encode()

    def cached(self, text, voice_wav):
        self.cached_calls.append((text, voice_wav))
        return f"[{text}]".encode()


def fake_beep(name):
    return f"<beep {name}>".encode()


class FakeTranscriber:
    """Utterances built by utter() carry their text; real PCM gets `default`."""

    def __init__(self, clock=None, stt_seconds=0.0):
        self.calls = 0
        self.default = ""
        self.error = None
        self._clock, self._stt = clock, stt_seconds

    def transcribe(self, pcm):
        self.calls += 1
        if self.error:
            raise self.error
        if self._clock:
            self._clock.advance(self._stt)
        if pcm.startswith(b"T:"):
            return pcm[2:].decode()
        return self.default


class FakeBrain:
    FAILURE = Brain.FAILURE

    def __init__(self, *answers, before=None):
        self.answers = list(answers)
        self.calls: list[tuple[list[dict], str]] = []
        self.personalities: list[str] = []
        self.notes: list[str] = []
        self._before = before

    def answer(self, history, name, camera, personality="", note=""):
        self.calls.append(([dict(m) for m in history], name))
        self.personalities.append(personality)
        self.notes.append(note)
        reply = self.answers.pop(0) if self.answers else ["Listo."]
        if self._before:
            self._before()
        yield from reply


def utter(text, rms=2000.0, seconds=1.0):
    return Utterance(b"T:" + text.encode(), seconds, rms)


def rover(name="Fred", wav=FRED_WAV):
    return Rover(name=name, unit=2, ssh=f"{name.lower()}@rover", app_url="http://rover:8000",
                 app_user=name.lower(), password_file=Path("/nonexistent"), voice=wav)


def make(name="Fred", brain=None, clock=None, link=None, transcriber=None, voice=None, sleep=None,
         airtime=None, beep=fake_beep):
    clock = clock or Clock()
    rovers = [rover("Fred", FRED_WAV), rover("Eva", EVA_WAV)]
    s = RoverSession(rover(name, FRED_WAV if name == "Fred" else EVA_WAV), Settings(rovers=rovers),
                     brain or FakeBrain(), voice or FakeVoice(), transcriber or FakeTranscriber(),
                     clock, link=link or FakeLink(), camera=object(), beep=beep, sleep=sleep,
                     airtime=airtime, wall=lambda: datetime(2026, 10, 29, 13, 5))
    return s


@pytest.fixture(autouse=True)
def no_leaked_threads():
    before = set(threading.enumerate())
    yield
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        extra = [t for t in threading.enumerate() if t not in before and t.name.startswith("houston")]
        if not extra:
            return
        time.sleep(0.01)
    pytest.fail(f"leaked threads: {extra}")


# ---------------------------------------------------------------- conversation

def test_wake_with_question_answers_between_beeps():
    brain = FakeBrain(["Son 687 días.", "Casi dos años."])
    s = make(brain=brain)
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    assert s.link.played() == ["<beep in>", "[Son 687 días.]", "[Casi dos años.]", "<beep out>"]
    assert s.link.speakers[0].closed
    assert len(s.link.speakers) == 1  # one Speaker per answer
    assert s.link.speakers[0].prelude == b"<beep in>"  # written as aplay starts, before its busy check
    assert s.state == "conversation"
    assert brain.calls[0] == ([{"role": "user", "content": "cuántos días tiene un año en Marte"}], "Fred")


def test_wake_alone_prompts_and_opens_conversation():
    s = make()
    s.handle(utter("Hey Fred."))
    assert s.link.played() == ["[¿Sí? Te escucho.]"]
    assert s.state == "conversation"
    s.handle(utter("¿Qué es Marte?"))
    assert s.brain.calls[0][0] == [{"role": "user", "content": "¿Qué es Marte?"}]


def test_follow_up_within_timeout_then_ignored_after():
    clock = Clock()
    s = make(clock=clock, brain=FakeBrain(["Son 687 días."], ["En Júpiter, casi 12 años."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    clock.advance(30)
    s.handle(utter("¿y en Júpiter?"))
    assert len(s.brain.calls) == 2
    assert s.state == "conversation"
    clock.advance(61)
    played = len(s.link.played())
    s.handle(utter("¿y en Saturno?"))
    assert len(s.brain.calls) == 2
    assert len(s.link.played()) == played
    assert s.state == "waiting"


def test_follow_up_history_holds_previous_turn():
    s = make(brain=FakeBrain(["Son 687 días."], ["Casi 12 años."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("¿y en Júpiter?"))
    assert s.brain.calls[1][0] == [
        {"role": "user", "content": "cuántos días tiene un año en Marte"},
        {"role": "assistant", "content": "Son 687 días."},
        {"role": "user", "content": "¿y en Júpiter?"},
    ]


def test_history_keeps_last_eight_messages():
    s = make(brain=FakeBrain(*[[f"Respuesta {i}."] for i in range(10)]))
    s.handle(utter("Hey Fred, pregunta 0"))
    for i in range(1, 7):
        s.handle(utter(f"pregunta {i}"))
    history = s.brain.calls[-1][0]
    assert len(history) == 8
    assert history[-1] == {"role": "user", "content": "pregunta 6"}
    assert history[0] == {"role": "assistant", "content": "Respuesta 2."}


def test_failure_line_not_added_to_history():
    s = make(brain=FakeBrain([Brain.FAILURE], ["Hola."]))
    s.handle(utter("Hey Fred, ¿qué ves?"))
    assert s.link.played()[1] == f"[{Brain.FAILURE}]"
    assert (Brain.FAILURE, FRED_WAV) not in s.voice.synth_calls  # pre-rendered, not synthesised
    s.handle(utter("¿y ahora?"))
    assert s.brain.calls[1][0] == [{"role": "user", "content": "¿y ahora?"}]


def test_history_cleared_after_close():
    s = make(brain=FakeBrain(["Son 687 días."], ["Hola."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("Gracias.", rms=CLOSE_MIN_RMS + 100))
    s.handle(utter("Hey Fred, ¿qué es Marte?"))
    assert s.brain.calls[1][0] == [{"role": "user", "content": "qué es Marte"}]


def test_history_cleared_after_timeout():
    clock = Clock()
    s = make(clock=clock, brain=FakeBrain(["Son 687 días."], ["Hola."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    clock.advance(61)
    s.handle(utter("Hey Fred, ¿qué es Marte?"))
    assert s.brain.calls[1][0] == [{"role": "user", "content": "qué es Marte"}]


def test_loud_thanks_closes_conversation():
    s = make(brain=FakeBrain(["Son 687 días."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("Gracias", rms=600))
    assert s.link.played()[-1] == "[¡De nada! Cambio y fuera.]"
    assert s.state == "waiting"


def test_quiet_thanks_is_ignored():
    s = make(brain=FakeBrain(["Son 687 días."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    played = len(s.link.played())
    s.handle(utter("Gracias.", rms=200))
    assert len(s.link.played()) == played
    assert s.state == "conversation"


def test_thanks_must_beat_noise_floor_too():
    s = make(brain=FakeBrain(["Son 687 días."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.segmenter._floor = 400.0  # noisy room: 3 x 400 = 1200 needed
    s.handle(utter("Gracias.", rms=900))
    assert s.state == "conversation"
    s.handle(utter("Gracias.", rms=1300))
    assert s.state == "waiting"


def test_thanks_in_waiting_is_ignored():
    s = make()
    s.handle(utter("Gracias.", rms=3000))
    assert s.link.played() == []
    assert s.state == "waiting"


@pytest.mark.parametrize("state", ["waiting", "conversation", "paused"])
def test_phantom_ignored_in_every_state(state):
    s = make()
    if state == "conversation":
        s.handle(utter("Hey Fred."))
    elif state == "paused":
        s.handle(utter("Fred, descansa"))
    played = len(s.link.played())
    s.handle(utter("Gracias por ver el video."))
    assert len(s.link.played()) == played
    assert s.brain.calls == []
    assert s.state == state


def test_empty_transcript_ignored():
    s = make()
    s.handle(utter(""))
    assert s.link.played() == []


# ---------------------------------------------------------------- two rovers

def test_eva_ignores_hey_fred():
    eva = make("Eva")
    eva.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    assert eva.link.played() == []
    assert eva.brain.calls == []
    assert eva.state == "waiting"


def test_sessions_are_independent():
    clock = Clock()
    fred = make("Fred", clock=clock, brain=FakeBrain(["Son 687 días.", ], ["Casi 12 años."]))
    eva = make("Eva", clock=clock)
    for u in (utter("Hey Fred, ¿cuántos días tiene un año en Marte?"), utter("¿y en Júpiter?")):
        fred.handle(u)
        eva.handle(u)  # same room: Eva hears everything Fred hears
    assert fred.state == "conversation" and len(fred.brain.calls) == 2
    assert eva.link.played() == [] and eva.brain.calls == []
    assert eva.state == "waiting"


def test_eva_answers_her_own_name_in_her_voice():
    eva = make("Eva", brain=FakeBrain(["Hola, soy Eva."]))
    eva.handle(utter("Hola Eva, ¿quién eres?"))
    assert eva.link.played() == ["<beep in>", "[Hola, soy Eva.]", "<beep out>"]
    assert eva.voice.synth_calls == [("Hola, soy Eva.", EVA_WAV)]
    assert eva.brain.calls[0][1] == "Eva"


# ---------------------------------------------------------------- pause

def test_pause_and_resume():
    s = make()
    s.handle(utter("Fred, descansa"))
    assert s.link.played() == ["[Entendido, descansando.]"]
    assert s.state == "paused"
    s.handle(utter("Hey Fred, ¿qué es Marte?"))
    s.handle(utter("Gracias", rms=3000))
    s.handle(utter("Fred, descansa"))
    assert s.link.played() == ["[Entendido, descansando.]"]
    assert s.brain.calls == []
    s.handle(utter("Fred, despierta"))
    assert s.link.played()[-1] == "[¡Aquí estoy!]"
    assert s.state == "waiting"
    s.handle(utter("Hey Fred, ¿qué es Marte?"))
    assert len(s.brain.calls) == 1


def test_pause_ends_conversation():
    s = make(brain=FakeBrain(["Son 687 días."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("Fred, descansa"))
    assert s.state == "paused"
    s.handle(utter("Fred, despierta"))
    s.handle(utter("¿y en Júpiter?"))  # conversation is gone: no name, no answer
    assert len(s.brain.calls) == 1


# ---------------------------------------------------------------- fixed phrases

def test_fixed_phrases_prerendered_in_rover_voice():
    s = make("Eva")
    s.prepare()
    expected = ["¿Sí? Te escucho.", "¡De nada! Cambio y fuera.", "Entendido, descansando.",
                "¡Aquí estoy!", "Mmm, déjame ver…", "Recibido, consultando con la base…", Brain.FAILURE]
    assert sorted(s.voice.cached_calls) == sorted((p, EVA_WAV) for p in expected)
    s.handle(utter("Hola Eva."))
    assert len(s.voice.cached_calls) == len(expected)  # served from memory, not re-read
    assert s.voice.synth_calls == []


# ---------------------------------------------------------------- latency masking

def test_filler_when_answer_is_slow_and_rotates():
    filler_played = threading.Event()

    def on_write(pcm):
        if any(f.encode() in pcm for f in FILLERS):
            filler_played.set()

    def slow():
        assert filler_played.wait(5)
        filler_played.clear()

    s = make(brain=FakeBrain(["Son 687 días."], ["Casi 12 años."], before=slow),
             link=FakeLink(on_write=on_write))
    s.filler_after = 0.05
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("¿y en Júpiter?"))
    assert s.link.speakers[0].tags() == ["<beep in>", "[Mmm, déjame ver…]", "[Son 687 días.]", "<beep out>"]
    assert s.link.speakers[1].tags() == ["<beep in>", "[Recibido, consultando con la base…]",
                                         "[Casi 12 años.]", "<beep out>"]


def test_no_filler_when_answer_is_ready():
    s = make(brain=FakeBrain(["Son 687 días."]))
    assert s.filler_after == 1.2
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    assert s.link.played() == ["<beep in>", "[Son 687 días.]", "<beep out>"]


def test_voice_failure_says_failure_line():
    class BrokenVoice(FakeVoice):
        def synth(self, text, voice_wav):
            raise RuntimeError("ffmpeg: boom")

    s = make(voice=BrokenVoice(), brain=FakeBrain(["Son 687 días."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    assert s.link.played() == ["<beep in>", f"[{Brain.FAILURE}]", "<beep out>"]


# ---------------------------------------------------------------- self-hearing

def speech():
    return noise(0.5) + tone(1.0) + noise(1.0)


def test_mic_frames_while_speaking_are_dropped():
    clock = Clock()
    tr = FakeTranscriber()
    tr.default = "Hey Fred, ¿qué es Marte?"
    s = make(clock=clock, transcriber=tr, brain=FakeBrain(["Un planeta."], ["Rojo."]))
    s.feed(speech())
    assert tr.calls == 1 and len(s.brain.calls) == 1
    # Fred's own voice comes back through the mic while (and just after) he speaks
    s.feed(speech(), t=clock())
    clock.advance(0.1)  # inside MUTE_MARGIN_S (echo tail)
    s.feed(speech())
    assert tr.calls == 1
    clock.advance(10)
    tr.default = "¿de qué color es?"
    s.feed(speech())
    assert tr.calls == 2 and len(s.brain.calls) == 2


def test_half_heard_tail_does_not_merge():
    clock = Clock()
    tr = FakeTranscriber()
    tr.default = "Hey Fred, ¿qué es Marte?"
    s = make(clock=clock, transcriber=tr)
    # one mic chunk: the wake question ends, then a new sound starts before Fred answers
    s.feed(speech() + tone(0.8))
    assert tr.calls == 1
    clock.advance(10)
    s.feed(noise(1.0))  # segmenter was reset: that half-heard tone must not come back
    assert tr.calls == 1


# ---------------------------------------------------------------- privacy

def test_unaddressed_speech_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    s = make(clock=clock, brain=FakeBrain(["Son 687 días."]))
    eva = make("Eva", clock=clock)
    s.handle(utter("Mañana vamos al súper con la abuela"))
    eva.handle(utter("Hey Fred, el cumpleaños de la tía es secreto"))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("Gracias.", rms=100))
    clock.advance(61)
    s.handle(utter("La contraseña del wifi es marte123"))
    s.handle(utter("Fred, descansa"))
    s.handle(utter("Oigan, ya está la cena"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("súper", "cumpleaños", "contraseña", "cena"):
        assert secret not in text
    assert "cuántos días tiene un año en Marte" in text  # questions are logged
    assert "Son 687 días." in text  # and answers


# ---------------------------------------------------------------- link loss

def test_speaker_link_error_mid_answer():
    s = make(brain=FakeBrain(["Uno.", "Dos.", "Tres."]), link=FakeLink(speaker_fail_on_write=2))
    s.connected = True
    s.handle(utter("Hey Fred, cuenta hasta tres"))
    assert s.connected is False
    assert s.link.speakers[0].closed
    assert s.state == "waiting"
    assert s.history == []


def test_speaker_cannot_start():
    s = make()
    s.link.speaker_error = LinkError("speaker busy after 5 tries")
    s.connected = True
    s.handle(utter("Hey Fred, ¿qué es Marte?"))
    assert s.connected is False
    s.handle(utter("Fred, descansa"))  # fixed phrases survive it too


# ---------------------------------------------------------------- timing

def test_timing_line_per_answered_turn(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    s = make(clock=clock, transcriber=FakeTranscriber(clock, stt_seconds=0.25),
             brain=FakeBrain(["Son 687 días."], ["Casi 12 años."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("Hey Fred."))  # fixed phrase: no timing
    s.handle(utter("¿y en Júpiter?"))
    lines = [r.getMessage() for r in caplog.records if "timing" in r.getMessage()]
    assert len(lines) == 2
    assert re.search(r"timing stt=250 first_sentence=\d+ first_audio=\d+ total=\d+$", lines[0])


# ---------------------------------------------------------------- run loop

def run_until(s, sleeps):
    """Fake backoff sleep: record the delay, stop the session after `sleeps` calls."""
    delays = []

    def sleep(d):
        delays.append(d)
        if len(delays) >= sleeps:
            s.stop()
    s._sleep = sleep
    t = threading.Thread(target=s.run, name="test-run")
    t.start()
    t.join(5)
    assert not t.is_alive()
    return delays


def test_run_backs_off_on_link_errors():
    s = make()
    assert run_until(s, 7) == [2, 4, 8, 16, 30, 30, 30]


def test_run_answers_then_reconnects_after_stream_ends():
    tr = FakeTranscriber()
    tr.default = "Hey Fred, ¿qué es Marte?"
    proc = FakeProc(speech())
    s = make(transcriber=tr, brain=FakeBrain(["Un planeta."]), link=FakeLink(mics=[proc]))
    delays = run_until(s, 2)
    assert s.link.played() == ["<beep in>", "[Un planeta.]", "<beep out>"]
    assert proc.killed
    assert delays == [2, 4]  # backoff restarts after a connection that delivered audio


def test_run_reconnects_on_speaker_loss():
    tr = FakeTranscriber()
    tr.default = "Hey Fred, ¿qué es Marte?"
    proc = FakeProc(speech(), keep_open=True)
    s = make(transcriber=tr, link=FakeLink(speaker_fail_on_write=1, mics=[proc]))
    run_until(s, 1)
    assert proc.killed
    assert s.connected is False


def test_run_survives_unexpected_errors():
    tr = FakeTranscriber()
    tr.error = RuntimeError("bug")
    link = FakeLink(mics=[FakeProc(speech(), keep_open=True)])
    s = make(transcriber=tr, link=link)
    assert len(run_until(s, 2)) == 2
    assert link.procs[0].killed


def test_run_stt_error_reconnects():
    tr = FakeTranscriber()
    tr.error = SttError("whisper-server: connection refused")
    proc = FakeProc(speech(), keep_open=True)
    s = make(transcriber=tr, link=FakeLink(mics=[proc]))
    run_until(s, 1)
    assert proc.killed


def test_stop_ends_run_while_listening():
    proc = FakeProc(keep_open=True)
    s = make(link=FakeLink(mics=[proc]))
    t = threading.Thread(target=s.run, name="test-run")
    t.start()
    deadline = time.monotonic() + 2
    while not s.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s.connected
    s.stop()
    t.join(2)
    assert not t.is_alive()
    assert proc.killed


def test_stop_ends_backoff_wait():
    s = make()
    t = threading.Thread(target=s.run, name="test-run")
    t.start()
    time.sleep(0.1)  # now in the 2 s backoff wait
    s.stop()
    t.join(1)
    assert not t.is_alive()


def run_paused(clock, link, away):
    """Pause the rover, then run: each backoff sleep is `away` seconds; stops after two."""
    s = make(clock=clock, link=link)
    s.handle(utter("Fred, descansa"))
    assert s.state == "paused"
    sleeps = []

    def sleep(d):
        sleeps.append(d)
        clock.advance(away)
        if len(sleeps) >= 2:
            s.stop()
    s._sleep = sleep
    t = threading.Thread(target=s.run, name="test-run")
    t.start()
    t.join(5)
    assert not t.is_alive()
    return s


@pytest.mark.parametrize("boots, away, state", [
    (["boot-1", "boot-2"], 5, "waiting"),   # rebooted, even quickly: awake again
    (["boot-1"], 90, "waiting"),            # switched off a while (or no boot id): awake again
    (["boot-1"], 5, "paused")])             # a Wi-Fi blip: still resting
def test_rover_back_from_a_reboot_is_no_longer_resting(boots, away, state):
    # Live 2026-10-29: paused in the afternoon, Jetson rebooted at night, "Hey Bentley" got nothing.
    link = FakeLink(mics=[FakeProc(), FakeProc()])  # connected, lost, connected again
    link.boots = list(boots)
    s = run_paused(Clock(), link, away)
    assert s.state == state


# ---------------------------------------------------------------- fix round 1

def test_speech_before_the_wake_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    s = make(brain=FakeBrain(["Son dos."]))
    s.handle(utter("bla bla la clave es 123. Hey Fred, ¿cuántas lunas tiene Marte?"))
    s.handle(utter("Gracias.", rms=3000))
    s.handle(utter("la tarjeta termina en 4455. Hey Fred."))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "123" not in text and "clave" not in text and "4455" not in text
    assert "cuántas lunas tiene Marte" in text
    assert len(s.brain.calls) == 1 and s.brain.calls[0][0][-1]["content"] == "cuántas lunas tiene Marte"


def test_conversation_ignores_questions_for_another_rover(caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    fred = make("Fred", clock=clock, brain=FakeBrain(["Son 687 días."], ["Casi 12 años."]))
    eva = make("Eva", clock=clock, brain=FakeBrain(["Soy Eva."]))
    for u in (utter("Hey Fred, ¿cuántos días tiene un año en Marte?"), utter("Hey Eva, ¿quién eres?"),
              utter("Eva, descansa"), utter("Gracias, Eva"), utter("¿y en Júpiter?")):
        fred.handle(u)
        eva.handle(u)
    assert [c[0][-1]["content"] for c in fred.brain.calls] == [
        "cuántos días tiene un año en Marte", "¿y en Júpiter?"]
    assert fred.state == "conversation"
    assert len(eva.brain.calls) == 1 and eva.state == "paused"  # Eva acted on her own name only
    fred_logs = "\n".join(r.getMessage() for r in caplog.records if r.getMessage().startswith("Fred"))
    assert "quién eres" not in fred_logs


@pytest.mark.parametrize("text", ["Oye Fred, descansa.", "Hey Fred descansa"])
def test_wake_plus_pause_is_pause(text):
    s = make()
    s.handle(utter(text))
    assert s.state == "paused"
    assert s.brain.calls == []
    s.handle(utter("Oye Fred, despierta."))
    assert s.state == "waiting"
    assert s.link.played() == ["[Entendido, descansando.]", "[¡Aquí estoy!]"]


def test_wake_plus_thanks_closes_only_in_conversation_and_loud():
    s = make(brain=FakeBrain(["Son 687 días."]))
    s.handle(utter("Hey Fred, gracias.", rms=3000))  # waiting: nothing to close, not a question
    assert s.link.played() == [] and s.brain.calls == []
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("Hey Fred, gracias.", rms=200))
    assert s.state == "conversation" and len(s.brain.calls) == 1
    s.handle(utter("Hey Fred, gracias, Fred.", rms=3000))
    assert s.state == "waiting"
    assert s.link.played()[-1] == "[¡De nada! Cambio y fuera.]"


def test_wake_plus_thanks_inside_a_question_is_a_question():
    s = make(brain=FakeBrain(["Casi 12 años."]))
    s.handle(utter("Hey Fred, gracias, ¿y en Júpiter?", rms=3000))
    assert len(s.brain.calls) == 1


def test_noisy_room_no_garbage_utterance_after_answer():
    clock = Clock()
    tr = FakeTranscriber()  # default "": whatever the VAD hands over while learning is ignored
    s = make(clock=clock, transcriber=tr, brain=FakeBrain(["Un planeta."]))
    s.feed(fluctuating_noise(40, amp=520))  # RMS ~300: the VAD learns the room
    learned = tr.calls
    tr.default = "Hey Fred, ¿qué es Marte?"
    s.feed(tone(1.0) + fluctuating_noise(1.0, amp=520, seed=5))
    assert tr.calls == learned + 1 and len(s.brain.calls) == 1
    clock.advance(5)  # Fred is quiet again; the room is still noisy
    tr.default = "garbage"
    sig = fluctuating_noise(20, amp=520, seed=6)
    for i in range(0, len(sig), 3200):
        s.feed(sig[i:i + 3200])
    assert tr.calls == learned + 1


def test_cancelled_answer_does_not_synthesise():
    s = make(link=FakeLink(speaker_fail_on_write=1))

    def wait_for_cancel():
        assert s._cancel.wait(2)  # speaker broke while the brain was still thinking
    s.brain = FakeBrain(["Uno.", "Dos."], before=wait_for_cancel)
    s.filler_after = 0.01
    s.handle(utter("Hey Fred, cuenta hasta dos"))
    assert s.connected is False
    assert s.voice.synth_calls == []


# ---------------------------------------------------------------- final review fixes

def test_paused_rover_does_not_transcribe_long_utterances():
    tr = FakeTranscriber()
    s = make(transcriber=tr)
    s.handle(utter("Fred, descansa"))
    calls = tr.calls
    s.handle(utter("Fred, despierta", seconds=PAUSED_MAX_S + 0.5))  # room talk length: never sent
    assert tr.calls == calls and s.state == "paused"
    s.handle(utter("Fred, despierta", seconds=PAUSED_MAX_S))
    assert tr.calls == calls + 1 and s.state == "waiting"
    s.handle(utter("Hey Fred, ¿qué es Marte?", seconds=PAUSED_MAX_S + 5))  # awake: any length
    assert len(s.brain.calls) == 1


def test_rovers_sharing_airtime_never_hear_each_other():
    clock = Clock()
    airtime = Airtime()
    eva_tr = FakeTranscriber()
    eva_tr.default = "Hey Eva, ¿qué dijo Fred?"
    eva = make("Eva", clock=clock, transcriber=eva_tr, airtime=airtime)
    heard_during = []

    def fred_speaking(pcm):  # Fred's voice reaches Eva's mic while he talks
        eva.feed(speech(), t=clock())
        heard_during.append(eva_tr.calls)

    fred = make("Fred", clock=clock, airtime=airtime, link=FakeLink(on_write=fred_speaking),
                brain=FakeBrain(["Son 687 días."]))
    fred.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    assert heard_during and set(heard_during) == {0}
    clock.advance(0.1)  # just after Fred went quiet: still inside the margin
    eva.feed(speech())
    assert eva_tr.calls == 0 and eva.brain.calls == []
    clock.advance(10)
    eva.feed(speech())  # the room is quiet again: Eva listens normally (segmenter was reset)
    assert eva_tr.calls == 1 and len(eva.brain.calls) == 1


def test_private_airtime_by_default():
    clock = Clock()
    fred = make("Fred", clock=clock, brain=FakeBrain(["Son 687 días."]))
    eva_tr = FakeTranscriber()
    eva_tr.default = "Hey Eva, hola"
    eva = make("Eva", clock=clock, transcriber=eva_tr)
    fred.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    eva.feed(speech())
    assert eva_tr.calls == 1


def test_beep_out_failure_still_finishes_the_turn():
    def beep(name):
        if name == "out":
            raise OSError("quindar_out.wav missing")
        return fake_beep(name)

    clock = Clock()
    airtime = Airtime()
    s = make(clock=clock, beep=beep, airtime=airtime, brain=FakeBrain(["Son 687 días."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    assert s.link.played() == ["<beep in>", "[Son 687 días.]"]
    assert s.link.speakers[0].closed
    assert s.history[-1] == {"role": "assistant", "content": "Son 687 días."}
    clock.advance(1)
    assert not airtime.busy(clock())


@pytest.mark.parametrize("junk", ["Mmm.", "¿Qué?", "Eh", "  ¡Ah!  "])
def test_one_word_junk_ignored_in_conversation(junk):
    s = make(brain=FakeBrain(["Son 687 días."], ["Casi 12 años."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter(junk))
    assert len(s.brain.calls) == 1 and s.state == "conversation"
    s.handle(utter("¿Y Júpiter?"))
    assert len(s.brain.calls) == 2


def test_one_word_commands_still_work_in_conversation():
    s = make(brain=FakeBrain(["Son 687 días."]))
    s.handle(utter("Hey Fred, ¿cuántos días tiene un año en Marte?"))
    s.handle(utter("Hey Fred."))  # a bare wake inside the conversation
    assert s.link.played()[-1] == "[¿Sí? Te escucho.]"
    s.handle(utter("¡Gracias!", rms=3000))  # one word, but a command
    assert s.state == "waiting"


def test_wake_question_logged_once(caplog):
    caplog.set_level(logging.DEBUG)
    s = make(brain=FakeBrain(["Son las tres."]))
    s.handle(utter("Hey Fred, ¿qué hora es?"))
    lines = [r.getMessage() for r in caplog.records if "qué hora es" in r.getMessage()]
    assert lines == ["Fred question: qué hora es"]
    s.handle(utter("Gracias.", rms=3000))
    s.handle(utter("Hey Fred."))
    assert any(r.getMessage() == "Fred wake: (sin pregunta)" for r in caplog.records)


def test_speech_right_after_the_rover_goes_quiet_is_heard():
    # Live test 2026-10-29: "¿Cuántos vuelos hizo Ingenuity?" right after "¿Sí? Te escucho."
    # lost its start and became "Carlos hizo Ingenuity" — the mic stayed deaf 0.5 s too long.
    airtime = Airtime()
    airtime.start()
    airtime.stop(quiet_at=10.0)
    assert airtime.busy(10.1)        # the speaker's own tail / room echo is still ignored
    assert not airtime.busy(10.2)    # a person answering right away is heard


def test_session_passes_the_rover_personality_to_the_brain():
    brain = FakeBrain(["Hola."])
    r = dataclasses.replace(rover("Bentley"), personality="Eres un Shiba Inu.")
    s = RoverSession(r, Settings(rovers=[r]), brain, FakeVoice(), FakeTranscriber(), Clock(),
                     link=FakeLink(), camera=object(), beep=fake_beep)
    s.handle(utter("Hey Bentley, ¿qué tal?"))
    assert brain.personalities == ["Eres un Shiba Inu."]


def test_filler_sound_replaces_every_filler_line():
    # Bentley (a Shiba Inu) only barks softly while he thinks — no "Mmm, déjame ver…" (Pablo, live test).
    filler_played = threading.Event()

    def on_write(pcm):
        if b"<barks>" in pcm or any(f.encode() in pcm for f in FILLERS):
            filler_played.set()

    def slow():
        assert filler_played.wait(5)
        filler_played.clear()

    barks = Path("/audio/barks_soft.wav")
    r = dataclasses.replace(rover("Bentley"), filler_sound=barks)
    loaded = []
    s = RoverSession(r, Settings(rovers=[r]), FakeBrain(["Uno."], ["Dos."], before=slow), FakeVoice(),
                     FakeTranscriber(), Clock(), link=FakeLink(on_write=on_write), camera=object(),
                     beep=fake_beep, load_sound=lambda p: loaded.append(p) or b"<barks>")
    s.filler_after = 0.05
    s.handle(utter("Bentley, ¿qué es Marte?"))
    s.handle(utter("¿y la Luna?"))
    assert s.link.speakers[0].tags() == ["<beep in>", "<barks>", "[Uno.]", "<beep out>"]
    assert s.link.speakers[1].tags() == ["<beep in>", "<barks>", "[Dos.]", "<beep out>"]
    assert loaded == [barks]  # loaded once, then served from memory


def test_bentley_knows_how_long_his_talk_demo_took():
    # Live 2026-10-29: asked how long his demo took, Bentley said "tres horas", then 54 minutes.
    clock = Clock()
    took = iter([131.4, 205.0, 0.0, 0.0])
    brain = FakeBrain(["Buenas tardes."], ["Levanten la mano."], ["Unos cinco minutos."], ["Hola."],
                      before=lambda: clock.advance(next(took)))
    s = make(clock=clock, brain=brain)
    s.handle(utter("Hey Fred, dame un demo de la charla"))
    s.handle(utter("¿Puedes continuar?"))
    s.handle(utter("¿Cuánto duró tu demo?"))
    assert "No has dado ninguna demo" in brain.notes[0]
    assert "una parte (dos minutos y once segundos)" in brain.notes[1]
    assert "a ti te tomó en total cinco minutos y treinta y seis segundos" in brain.notes[2]
    assert "dos partes" in brain.notes[2] and "cinco minutos y treinta y seis segundos" in brain.notes[2]
    assert "Tu respuesta anterior duró tres minutos y veinticinco segundos" in brain.notes[2]
    s.handle(utter("Dame un demo de la charla"))  # a new demo: the count starts again
    assert "No has dado ninguna demo" in brain.notes[3]


def test_every_answer_knows_the_date_time_and_conversation_length():
    clock = Clock()
    brain = FakeBrain(["Dos."], ["Hoy es jueves."], before=lambda: clock.advance(10))
    s = make(clock=clock, brain=brain)
    s.handle(utter("Hey Fred, ¿cuántas lunas tiene Marte?"))
    s.handle(utter("¿Qué día es hoy?"))
    assert "jueves 29 de octubre de 2026" in brain.notes[0] and "son la una y cinco de la tarde" in brain.notes[0]
    assert "Esta conversación empezó hace cero segundos" in brain.notes[0]
    assert "Esta conversación empezó hace diez segundos" in brain.notes[1]
    assert "No has dado ninguna demo" in brain.notes[1]


def test_stopwatch_times_a_rehearsal_and_houston_says_it_exactly():
    # Live: gpt-oss repeated an old stopwatch reading, so Houston says these itself.
    clock = Clock()
    brain = FakeBrain(["Suerte, Pablo."])
    s = make(clock=clock, brain=brain)
    s.handle(utter("Hey Fred, cronométrame, voy a ensayar la charla"))
    clock.advance(61)
    s.handle(utter("Hey Fred, ¿cuánto llevo?"))
    clock.advance(44 * 60 - 61 + 30)
    s.handle(utter("Hey Fred, para el cronómetro"))
    s.handle(utter("¿Cómo me fue?"))
    assert s.link.played() == [
        "<beep in>", "[Cronómetro corriendo. Cuando termines, dime: Fred, para el cronómetro.]", "<beep out>",
        "<beep in>", "[Llevas un minuto y un segundo.]", "<beep out>",
        "<beep in>", "[Cronómetro parado: cuarenta y cuatro minutos y treinta segundos.]", "<beep out>",
        "<beep in>", "[Suerte, Pablo.]", "<beep out>"]
    assert len(brain.calls) == 1  # only the open question went to the model...
    assert "la última vez marcó cuarenta y cuatro minutos y treinta segundos" in brain.notes[0]  # ...knowing it
    assert brain.calls[0][0][-2] == {"role": "assistant",
                                     "content": "Cronómetro parado: cuarenta y cuatro minutos y treinta segundos."}


def test_what_time_is_it_is_said_by_houston():
    brain = FakeBrain()
    s = make(brain=brain)
    s.handle(utter("Hey Fred, ¿qué hora es?"))
    assert "[Son la una y cinco de la tarde.]" in s.link.played()
    assert brain.calls == []
