import logging
import os
import re
import threading
import time
from pathlib import Path

import pytest

from houston.brain import Brain
from houston.config import Rover, Settings
from houston.rover_link import LinkError
from houston.session import CLOSE_MIN_RMS, FILLERS, RoverSession
from houston.stt import SttError
from houston.tests.test_vad import noise, tone
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
    def __init__(self, fail_on_write=None, on_write=None):
        self.writes: list[bytes] = []
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

    def detect(self):
        return ("Device", "UACDemoV10")

    def mic(self):
        if not self.mics:
            raise LinkError("ssh: connect to host: No route to host")
        m = self.mics.pop(0)
        if isinstance(m, Exception):
            raise m
        self.procs.append(m)
        return m

    def speaker(self):
        if self.speaker_error:
            raise self.speaker_error
        s = FakeSpeaker(self._fail, self._on_write)
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
        self._before = before

    def answer(self, history, name, camera):
        self.calls.append(([dict(m) for m in history], name))
        reply = self.answers.pop(0) if self.answers else ["Listo."]
        if self._before:
            self._before()
        yield from reply


def utter(text, rms=2000.0):
    return Utterance(b"T:" + text.encode(), 1.0, rms)


def rover(name="Fred", wav=FRED_WAV):
    return Rover(name=name, unit=2, ssh=f"{name.lower()}@rover", app_url="http://rover:8000",
                 app_user=name.lower(), password_file=Path("/nonexistent"), voice=wav)


def make(name="Fred", brain=None, clock=None, link=None, transcriber=None, voice=None, sleep=None):
    clock = clock or Clock()
    s = RoverSession(rover(name, FRED_WAV if name == "Fred" else EVA_WAV), Settings(rovers=[]),
                     brain or FakeBrain(), voice or FakeVoice(), transcriber or FakeTranscriber(),
                     clock, link=link or FakeLink(), camera=object(), beep=fake_beep, sleep=sleep)
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
    clock.advance(0.3)
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
    assert "¿cuántos días tiene un año en Marte" in text  # questions are logged
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
