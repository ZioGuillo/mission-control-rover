"""One rover's voice session: mic -> utterances -> wake/conversation/pause -> spoken answers.

Privacy: only wake phrases, questions, answers, timings and errors are logged. Speech that is
not addressed to this rover is dropped without a trace.
"""
from __future__ import annotations

import itertools
import logging
import math
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from houston import effects
from houston import voice as voice_mod
from houston.brain import HOMEWORK, Brain, is_demo, is_homework
from houston.diag import Diag
from houston.camera import AppCamera
from houston.config import Rover, Settings
from houston.phrases import Heard, classify, is_phantom
from houston.playback import Playback
from houston.rover_link import LinkError, RoverLink, stop_mic
from houston.stt import SttError, Transcriber
from houston.timekeeper import Timekeeper
from houston.vad import Segmenter, Utterance
from houston.voice import Voice

log = logging.getLogger(__name__)

CLOSE_MIN_RMS = 600.0      # a closing "gracias" must be at least this loud (peak frame RMS)...
CLOSE_FLOOR_RATIO = 3.0    # ...and this many times the room's noise floor (else: Whisper phantom)
HISTORY_MESSAGES = 8
FILLER_AFTER_S = 1.2       # no answer audio this long after the beep -> play a filler
MUTE_MARGIN_S = 0.15       # keep the mic deaf this long after the rover goes quiet (echo tail only:
                           # 0.5 s clipped people who answer right away — "Carlos" for "¿Cuántos vuelos")
MIN_QUESTION_CHARS = 3     # "Hey Fred" plus less than this is just a wake
MIN_FOLLOW_UP_WORDS = 2    # in a conversation, "Mmm." / "¿Qué?" is room noise, not a question
# "¿Cuál está, mamá?" is said to mom, not to the rover (live 2026-10-29, Bentley answered it).
_FAMILY = r"(mam[aá]|mami|pap[aá]|papi|abuel[ao]|stefania|pablo|lucca|luca|luka)"
TO_FAMILY = re.compile(rf"^\W*{_FAMILY}\s*,|,\s*{_FAMILY}\W*$", re.I)
# ...but "¡Sí!" right after the rover asked something is an answer ("¿Quieres oír una historia?").
SHORT_REPLY = re.compile(r"^\W*(s[ií]+|no|claro|dale|obvio|bueno|ok|okay|vale|yes)\W*$", re.I)
PAUSED_MAX_S = 2.5         # while resting, only utterances this short go to Whisper ("Fred, despierta")
BACKOFF_START_S, BACKOFF_MAX_S = 2.0, 30.0
# A rover that rebooted (new boot id) or was away this long comes back awake, not still resting
# (live 2026-10-29: paused at 15:15, Jetson rebooted at 19:18, "Hey Bentley" got nothing).
AWAY_CLEARS_PAUSE_S = 60.0
MIC_CHUNK = 3200           # 0.1 s of 16 kHz S16_LE
_POLL_S = 0.1
INBOX_DIR = Path("~/.config/houston/say").expanduser()  # say/bentley.txt: Bentley says it, unprompted
_INBOX_EVERY_S = 0.5
HOMEWORK_TIMEOUT_S = 60.0  # a kid doing homework thinks longer than 15 s before answering
_JOIN_S = 2.0

SAY_LISTENING = "¿Sí? Te escucho."
SAY_GOODBYE = "¡De nada! Cambio y fuera."
SAY_PAUSED = "Entendido, descansando."
SAY_RESUMED = "¡Aquí estoy!"
FILLERS = ("Mmm, déjame ver…", "Recibido, consultando con la base…")
FIXED = (SAY_LISTENING, SAY_GOODBYE, SAY_PAUSED, SAY_RESUMED, *FILLERS, Brain.FAILURE)

WAITING, CONVERSATION, PAUSED = "waiting", "conversation", "paused"
_END = object()


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class Airtime:
    """When any rover is talking, shared by all sessions in the process: every mic is deaf while
    any rover speaks (and MUTE_MARGIN_S after), so two rovers in one room never answer each other."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._speaking = 0
        self._until = -math.inf

    def start(self) -> None:
        with self._lock:
            self._speaking += 1

    def stop(self, quiet_at: float) -> None:
        """One speaker went quiet at clock `quiet_at`: stay deaf until then plus the margin."""
        with self._lock:
            self._speaking = max(0, self._speaking - 1)
            self._until = max(self._until, quiet_at + MUTE_MARGIN_S)

    def busy(self, t: float) -> bool:
        with self._lock:
            return self._speaking > 0 or t < self._until


class RoverSession:
    def __init__(self, rover: Rover, settings: Settings, brain: Brain, voice: Voice,
                 transcriber: Transcriber, clock: Callable[[], float] = time.monotonic, *,
                 link=None, camera=None, beep: Callable[[str], bytes] = voice_mod.beep,
                 sleep: Callable[[float], object] | None = None,
                 airtime: Airtime | None = None,
                 load_sound: Callable[[Path], bytes] = voice_mod.sound,
                 wall: Callable[[], datetime] = datetime.now, inbox: Path | None = None) -> None:
        self.rover, self.settings = rover, settings
        self.brain, self.voice, self.transcriber, self.clock = brain, voice, transcriber, clock
        self.link = link or RoverLink(rover.ssh)
        self.camera = camera or AppCamera(rover.camera_url, rover.app_user, rover.password_file)
        self._beep = beep
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait  # backoff wait; stop() cuts it short
        self.segmenter = Segmenter()
        self.filler_after = FILLER_AFTER_S
        self.connected = False
        self.history: list[dict] = []
        self.time = Timekeeper()  # every duration the rover may say is measured here
        self.inbox = inbox
        self._inbox_checked = 0.0
        self._wall = wall
        self._state = WAITING
        self._down_since: float | None = None  # when the link to the rover last went down
        self._boot_id: str | None = None  # the rover's boot when we last connected
        self._last_turn = 0.0
        self.airtime = airtime or Airtime()  # __main__ shares one across all rovers
        self._muted = False  # frames were dropped: reset the segmenter before the next one
        self._got_audio = False  # this connection delivered mic data (resets the backoff)
        self._fixed: dict[str, bytes] = {}
        self._load_sound = load_sound
        self._diag = Diag()
        self._sounds: dict[Path, bytes] = {}
        # A rover with its own filler sound plays only that (Bentley barks; no "Mmm, déjame ver…").
        self._fillers = itertools.cycle((rover.filler_sound,) if rover.filler_sound else FILLERS)
        self._cancel = threading.Event()
        self._others = [r.name for r in settings.rovers if r.name.lower() != rover.name.lower()]

    # ------------------------------------------------------------------ state

    @property
    def name(self) -> str:
        return self.rover.name

    @property
    def state(self) -> str:
        # Doing homework, or Bentley just offered to help (live: "¿Quieres que te ayude…?" closed in 15 s).
        offered = bool(self.history) and HOMEWORK.search(self.history[-1]["content"]) is not None
        timeout = (HOMEWORK_TIMEOUT_S if offered or is_homework(self.history)
                   else self.settings.conversation_timeout)
        if self._state == CONVERSATION and self.clock() - self._last_turn > timeout:
            log.info("%s: conversation timed out", self.name)
            self._end_conversation()
        return self._state

    def _end_conversation(self) -> None:
        self.history.clear()
        self.time.end_conversation()
        if self._state == CONVERSATION:
            self._state = WAITING

    def _loud_enough(self, u: Utterance) -> bool:
        return u.peak_rms >= max(CLOSE_MIN_RMS, CLOSE_FLOOR_RATIO * self.segmenter.noise_floor)

    def _command(self, heard: Heard) -> Heard:
        """"Oye Fred, descansa." / "Hey Fred, gracias.": a wake whose rest is only a command."""
        if heard.kind == "wake" and heard.rest:
            for probe in (heard.rest, f"{self.name} {heard.rest}"):
                kind = classify(probe, self.name).kind
                if kind in ("pause", "resume", "close"):
                    return Heard(kind, "")
        return heard

    def _for_another_rover(self, text: str) -> bool:
        return any(classify(text, other).kind != "other" for other in self._others)

    def handle(self, u: Utterance) -> None:
        """Transcribe one utterance and act on it. Raises SttError; never LinkError.

        Logged: the command kind, or the question after the wake. Never the full transcript:
        whatever was said before "Hey Fred" in the same breath is room talk."""
        if self.state == PAUSED and u.seconds > PAUSED_MAX_S:
            return  # resting: room talk never reaches Whisper, only short "Fred, despierta"
        t0 = self.clock()
        text = self.transcriber.transcribe(u.pcm)
        stt = self.clock() - t0
        if not text or is_phantom(text):
            return
        heard = self._command(classify(text, self.name))
        self._diag.record(self.name, u.seconds, text, heard.kind)  # off unless a window is open
        state = self.state

        if state == PAUSED:
            if heard.kind == "resume":
                log.info("%s command: resume", self.name)
                self._state = WAITING
                self._say(SAY_RESUMED)
            return
        if heard.kind in ("pause", "resume"):
            log.info("%s command: %s", self.name, heard.kind)
            if heard.kind == "pause":
                self._end_conversation()
                self._state = PAUSED
            self._say(SAY_PAUSED if heard.kind == "pause" else SAY_RESUMED)
            return
        if heard.kind == "close":
            if state == CONVERSATION and self._loud_enough(u):
                log.info("%s command: close", self.name)
                self._end_conversation()
                self._say(SAY_GOODBYE)
            return
        if heard.kind == "wake":
            if len(heard.rest) < MIN_QUESTION_CHARS:
                log.info("%s wake: (sin pregunta)", self.name)  # a question is logged by _answer
                self._state = CONVERSATION
                self._say(SAY_LISTENING)
                self._last_turn = self.clock()
                return
            self._answer(heard.rest, t0, stt)
            return
        asked = bool(self.history) and self.history[-1]["content"].rstrip().endswith("?")
        if (state == CONVERSATION and not self._for_another_rover(text) and not TO_FAMILY.search(text)
                and (len(re.findall(r"\w+", text)) >= MIN_FOLLOW_UP_WORDS or (asked and SHORT_REPLY.match(text)))):
            self._answer(text, t0, stt)

    # ------------------------------------------------------------------ speaking

    def prepare(self) -> None:
        """Pre-render the fixed phrases (and beeps) so they play instantly."""
        for phrase in FIXED:
            if self._stop.is_set():
                return
            try:
                self._phrase(phrase)
            except Exception as e:
                log.error("%s: cannot pre-render %r: %s", self.name, phrase, e)
        if self.rover.filler_sound:
            try:
                self._filler(self.rover.filler_sound)
            except Exception as e:
                log.error("%s: cannot load filler sound: %s", self.name, e)
        for b in ("in", "out"):
            try:
                self._beep(b)
            except Exception as e:
                log.error("%s: cannot load beep %s: %s", self.name, b, e)

    def _filler(self, item: str | Path) -> bytes:
        if isinstance(item, Path):
            if item not in self._sounds:
                self._sounds[item] = self._load_sound(item)
            return self._sounds[item]
        return self._phrase(item)

    def _phrase(self, text: str) -> bytes:
        pcm = self._fixed.get(text)
        if pcm is None:
            pcm = self._fixed[text] = self.voice.cached(text, self.rover.voice)
        return pcm

    def _lost(self, e: Exception) -> None:
        log.warning("%s: speaker lost: %s", self.name, e)
        self.connected = False
        self._end_conversation()

    def _open(self, prelude: bytes = b"") -> Playback | None:
        """Every opened Playback must go through _finish (it releases the shared airtime)."""
        self.airtime.start()
        try:
            return Playback(self.link.speaker(prelude=prelude), self.clock, len(prelude))
        except LinkError as e:
            self.airtime.stop(self.clock())
            self._lost(e)
            return None

    def _finish(self, pb: Playback) -> float | None:
        """Close the playback, mute the mic until the rover is quiet; None if the link broke."""
        try:
            end = pb.finish()
        except LinkError as e:
            self.airtime.stop(pb.end)
            self._lost(e)
            return None
        self.airtime.stop(end)
        return end

    def _say(self, phrase: str) -> None:
        try:
            pcm = self._phrase(phrase)
        except Exception as e:
            log.error("%s: cannot render %r: %s", self.name, phrase, e)
            return
        pb = self._open()
        if pb:
            pb.put(pcm)
            self._finish(pb)

    def _answer(self, question: str, t0: float, stt: float) -> None:
        log.info("%s question: %s", self.name, question)
        self.history.append({"role": "user", "content": question})
        del self.history[:-HISTORY_MESSAGES]
        self._state = CONVERSATION
        demo = is_demo(self.history)
        wall = self._wall()
        exact = self.time.question(question, t0, wall, self.name)  # the time, the stopwatch: said as-is
        note = self.time.note(self.clock(), wall)
        self._cancel = cancel = threading.Event()
        audio: queue.Queue = queue.Queue()
        spoken: list[str] = []
        times: dict[str, float] = {}
        producer = threading.Thread(target=self._produce, name="houston-synth", daemon=True,
                                    args=(list(self.history), note, exact, audio, spoken, times, cancel))
        producer.start()  # the brain starts thinking while the speaker opens (~0.3 s)

        try:
            beep_in = self._beep("in")
        except Exception as e:
            log.error("%s: cannot load beep in: %s", self.name, e)
            beep_in = b""
        pb = self._open(beep_in)  # the beep plays the moment aplay starts
        if pb is None:
            cancel.set()
            producer.join(_JOIN_S)
            return
        try:
            item = audio.get(timeout=self.filler_after)
        except queue.Empty:
            try:
                pb.put(self._filler(next(self._fillers)))
            except Exception as e:
                log.error("%s: cannot render filler: %s", self.name, e)
            item = self._next(audio, pb)
        while item is not _END and item is not None:
            pb.put(item, answer=True)
            item = self._next(audio, pb)
        if item is _END:
            try:
                pb.put(self._beep("out"))
            except Exception as e:
                log.error("%s: cannot load beep out: %s", self.name, e)
        else:
            cancel.set()  # link lost or stopping: stop thinking and synthesising
        end = self._finish(pb)
        producer.join(None if item is _END else _JOIN_S)
        if end is None or item is not _END:
            return

        answer = " ".join(s for s in spoken if s != Brain.FAILURE and not effects.sound(s))
        if answer:
            self.history.append({"role": "assistant", "content": answer})
            del self.history[:-HISTORY_MESSAGES]
            self.time.answered(end - t0, demo)
        elif self.history and self.history[-1]["content"] == question:
            self.history.pop()  # failed turn: keep no dangling question
        log.info("%s answer: %s", self.name, " ".join(spoken).replace(effects.MARK, "♪"))
        self._last_turn = self.clock()
        first_sentence = times.get("first_sentence", end)
        first_audio = pb.first_answer if pb.first_answer is not None else end
        log.info("%s timing stt=%d first_sentence=%d first_audio=%d total=%d", self.name,
                 _ms(stt), _ms(first_sentence - t0), _ms(first_audio - t0), _ms(end - t0))

    def _next(self, audio: queue.Queue, pb: Playback):
        """Next synthesised sentence, _END, or None if the speaker broke or we're stopping."""
        while not pb.failed and not self._stop.is_set():
            try:
                return audio.get(timeout=_POLL_S)
            except queue.Empty:
                continue
        return None

    def _produce(self, history: list[dict], note: str, exact: str | None, out: queue.Queue, spoken: list[str],
                 times: dict[str, float], cancel: threading.Event) -> None:
        """Brain sentences -> PCM, one at a time, so sentence N+1 is made while N plays."""
        answer = None
        try:
            answer = (iter([exact]) if exact else
                      self.brain.answer(history, self.name, self.camera, self.rover.personality, note))
            for sentence in answer:
                times.setdefault("first_sentence", self.clock())
                if cancel.is_set():
                    return  # dead answer: don't hold the Voice lock for it
                if effects.sound(sentence):
                    pcm = self._filler(effects.sound(sentence))  # a story's roar: a sound, not words
                elif sentence == Brain.FAILURE:
                    pcm = self._phrase(sentence)
                else:
                    pcm = self.voice.synth(sentence, self.rover.voice)
                spoken.append(sentence)
                out.put(pcm)
        except Exception as e:  # voice/ffmpeg broke: the rover still says something
            log.error("%s: voice failure: %s", self.name, e)
            try:
                out.put(self._phrase(Brain.FAILURE))
                spoken.append(Brain.FAILURE)
            except Exception as e2:
                log.error("%s: cannot render failure line: %s", self.name, e2)
        finally:
            close = getattr(answer, "close", None)
            if close:
                close()  # stop the Ollama stream early on cancel
            out.put(_END)

    # ------------------------------------------------------------------ listening

    def feed(self, chunk: bytes, t: float | None = None) -> None:
        """Mic PCM that arrived at clock time `t`; frames heard while any rover speaks are dropped."""
        t = self.clock() if t is None else t
        if self.airtime.busy(t):
            self._muted = True
            return
        if self._muted:
            self.segmenter.reset(keep_floor=True)  # forget a half-heard tail, keep the room
            self._muted = False
        for u in self.segmenter.feed(chunk):
            self.handle(u)
            if self.airtime.busy(t):  # a rover spoke: the rest of this chunk overlapped it
                self._muted = True
                return

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()

    def run(self) -> None:
        """Listen until stop(): reconnect with backoff on any failure; never raises."""
        self.prepare()
        delay = BACKOFF_START_S
        while not self._stop.is_set():
            self._got_audio = False
            try:
                self.link.detect()
                proc = self.link.mic()
                self._back()
                self._listen(proc)
            except (LinkError, SttError) as e:
                log.warning("%s: %s", self.name, e)
            except Exception:
                log.exception("%s: session error", self.name)
            self.connected = False
            if self._down_since is None:
                self._down_since = self.clock()
            if self._stop.is_set():
                break
            if self._got_audio:
                delay = BACKOFF_START_S
            log.info("%s: reconnecting in %.0f s", self.name, delay)
            self._sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    def _back(self) -> None:
        """The rover answers again: if it rebooted, or was away long, it is no longer resting."""
        away = None if self._down_since is None else self.clock() - self._down_since
        self._down_since = None
        try:
            boot = self.link.boot_id()
        except LinkError as e:
            log.warning("%s: no boot id: %s", self.name, e)
            boot = None
        rebooted = bool(boot and self._boot_id and boot != self._boot_id)
        self._boot_id = boot or self._boot_id
        if self._state == PAUSED and (rebooted or (away is not None and away >= AWAY_CLEARS_PAUSE_S)):
            log.info("%s: back (%s), no longer resting", self.name,
                     "rebooted" if rebooted else f"{away:.0f} s away")
            self._state = WAITING

    def _listen(self, proc) -> None:
        chunks: queue.Queue = queue.Queue()
        reader = threading.Thread(target=self._read_mic, args=(proc.stdout, chunks),
                                  name="houston-mic", daemon=True)
        self.connected = True
        self.segmenter.reset()
        self._muted = False
        reader.start()
        log.info("%s: listening", self.name)
        try:
            while self.connected and not self._stop.is_set():
                try:
                    item = chunks.get(timeout=_POLL_S)
                except queue.Empty:
                    continue
                if item is None:
                    log.warning("%s: mic stream ended", self.name)
                    return
                self._got_audio = True
                self.feed(item[1], item[0])
                self._check_inbox()
        finally:
            stop_mic(proc)
            reader.join(_JOIN_S)
            try:
                proc.stdout.close()
            except (OSError, AttributeError):
                pass

    def _check_inbox(self) -> None:
        """Something Houston was asked to say unprompted ("pregúntale a Lucca…"), at most every 0.5 s."""
        if self.inbox is None or time.monotonic() - self._inbox_checked < _INBOX_EVERY_S:
            return
        self._inbox_checked = time.monotonic()
        try:
            text = self.inbox.read_text(encoding="utf-8").strip()
            self.inbox.unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            log.error("%s: inbox: %s", self.name, e)
            return
        if text:
            self.announce(text)

    def announce(self, text: str) -> None:
        """Speak first, between beeps; the reply that follows needs no wake word."""
        log.info("%s says: %s", self.name, text)
        try:
            pcm = self.voice.synth(text, self.rover.voice)
            beep_in, beep_out = self._beep("in"), self._beep("out")
        except Exception as e:
            log.error("%s: cannot say %r: %s", self.name, text, e)
            return
        pb = self._open(beep_in)
        if pb is None:
            return
        pb.put(pcm, answer=True)
        pb.put(beep_out)
        if self._finish(pb) is None:
            return
        self.history.append({"role": "assistant", "content": text})
        del self.history[:-HISTORY_MESSAGES]
        self._state = CONVERSATION
        self._last_turn = self.clock()

    def _read_mic(self, stream, chunks: queue.Queue) -> None:
        try:
            while chunk := stream.read1(MIC_CHUNK):
                chunks.put((self.clock(), chunk))
        except (OSError, ValueError):
            pass
        finally:
            chunks.put(None)
