"""One rover's voice session: mic -> utterances -> wake/conversation/pause -> spoken answers.

Privacy: only wake phrases, questions, answers, timings and errors are logged. Speech that is
not addressed to this rover is dropped without a trace.
"""
from __future__ import annotations

import itertools
import logging
import math
import queue
import threading
import time
from typing import Callable

from houston import voice as voice_mod
from houston.brain import Brain
from houston.camera import AppCamera
from houston.config import Rover, Settings
from houston.phrases import classify, is_phantom
from houston.playback import Playback
from houston.rover_link import LinkError, RoverLink, stop_mic
from houston.stt import SttError, Transcriber
from houston.vad import Segmenter, Utterance
from houston.voice import Voice

log = logging.getLogger(__name__)

CLOSE_MIN_RMS = 600.0      # a closing "gracias" must be at least this loud (peak frame RMS)...
CLOSE_FLOOR_RATIO = 3.0    # ...and this many times the room's noise floor (else: Whisper phantom)
HISTORY_MESSAGES = 8
FILLER_AFTER_S = 1.2       # no answer audio this long after the beep -> play a filler
MUTE_MARGIN_S = 0.5        # keep the mic deaf this long after the rover goes quiet
MIN_QUESTION_CHARS = 3     # "Hey Fred" plus less than this is just a wake
BACKOFF_START_S, BACKOFF_MAX_S = 2.0, 30.0
MIC_CHUNK = 3200           # 0.1 s of 16 kHz S16_LE
_POLL_S = 0.1
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


class RoverSession:
    def __init__(self, rover: Rover, settings: Settings, brain: Brain, voice: Voice,
                 transcriber: Transcriber, clock: Callable[[], float] = time.monotonic, *,
                 link=None, camera=None, beep: Callable[[str], bytes] = voice_mod.beep,
                 sleep: Callable[[float], object] | None = None) -> None:
        self.rover, self.settings = rover, settings
        self.brain, self.voice, self.transcriber, self.clock = brain, voice, transcriber, clock
        self.link = link or RoverLink(rover.ssh)
        self.camera = camera or AppCamera(rover.app_url, rover.app_user, rover.password_file)
        self._beep = beep
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait  # backoff wait; stop() cuts it short
        self.segmenter = Segmenter()
        self.filler_after = FILLER_AFTER_S
        self.connected = False
        self.history: list[dict] = []
        self._state = WAITING
        self._last_turn = 0.0
        self._mute_until = -math.inf
        self._muted = False  # frames were dropped: reset the segmenter before the next one
        self._got_audio = False  # this connection delivered mic data (resets the backoff)
        self._fixed: dict[str, bytes] = {}
        self._fillers = itertools.cycle(FILLERS)
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ state

    @property
    def name(self) -> str:
        return self.rover.name

    @property
    def state(self) -> str:
        if (self._state == CONVERSATION
                and self.clock() - self._last_turn > self.settings.conversation_timeout):
            log.info("%s: conversation timed out", self.name)
            self._end_conversation()
        return self._state

    def _end_conversation(self) -> None:
        self.history.clear()
        if self._state == CONVERSATION:
            self._state = WAITING

    def _loud_enough(self, u: Utterance) -> bool:
        return u.peak_rms >= max(CLOSE_MIN_RMS, CLOSE_FLOOR_RATIO * self.segmenter.noise_floor)

    def handle(self, u: Utterance) -> None:
        """Transcribe one utterance and act on it. Raises SttError; never LinkError."""
        t0 = self.clock()
        text = self.transcriber.transcribe(u.pcm)
        stt = self.clock() - t0
        if not text or is_phantom(text):
            return
        heard = classify(text, self.name)
        state = self.state

        if state == PAUSED:
            if heard.kind == "resume":
                log.info("%s heard: %s", self.name, text)
                self._state = WAITING
                self._say(SAY_RESUMED)
            return
        if heard.kind in ("pause", "resume"):
            log.info("%s heard: %s", self.name, text)
            if heard.kind == "pause":
                self._end_conversation()
                self._state = PAUSED
            self._say(SAY_PAUSED if heard.kind == "pause" else SAY_RESUMED)
            return
        if heard.kind == "close":
            if state == CONVERSATION and self._loud_enough(u):
                log.info("%s heard: %s", self.name, text)
                self._end_conversation()
                self._say(SAY_GOODBYE)
            return
        if heard.kind == "wake":
            log.info("%s heard: %s", self.name, text)
            if len(heard.rest) < MIN_QUESTION_CHARS:
                self._state = CONVERSATION
                self._say(SAY_LISTENING)
                self._last_turn = self.clock()
                return
            self._answer(heard.rest, t0, stt)
            return
        if state == CONVERSATION:
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
        for b in ("in", "out"):
            try:
                self._beep(b)
            except Exception as e:
                log.error("%s: cannot load beep %s: %s", self.name, b, e)

    def _phrase(self, text: str) -> bytes:
        pcm = self._fixed.get(text)
        if pcm is None:
            pcm = self._fixed[text] = self.voice.cached(text, self.rover.voice)
        return pcm

    def _lost(self, e: Exception) -> None:
        log.warning("%s: speaker lost: %s", self.name, e)
        self.connected = False
        self._end_conversation()

    def _open(self) -> Playback | None:
        try:
            return Playback(self.link.speaker(), self.clock)
        except LinkError as e:
            self._lost(e)
            return None

    def _finish(self, pb: Playback) -> float | None:
        """Close the playback, mute the mic until the rover is quiet; None if the link broke."""
        try:
            end = pb.finish()
        except LinkError as e:
            self._mute_until = pb.end + MUTE_MARGIN_S
            self._lost(e)
            return None
        self._mute_until = end + MUTE_MARGIN_S
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
        self._cancel = cancel = threading.Event()
        audio: queue.Queue = queue.Queue()
        spoken: list[str] = []
        times: dict[str, float] = {}
        producer = threading.Thread(target=self._produce, name="houston-synth", daemon=True,
                                    args=(list(self.history), audio, spoken, times, cancel))
        producer.start()  # the brain starts thinking while the speaker opens (~0.3 s)

        pb = self._open()
        if pb is None:
            cancel.set()
            producer.join(_JOIN_S)
            return
        pb.put(self._beep("in"))
        try:
            item = audio.get(timeout=self.filler_after)
        except queue.Empty:
            try:
                pb.put(self._phrase(next(self._fillers)))
            except Exception as e:
                log.error("%s: cannot render filler: %s", self.name, e)
            item = self._next(audio, pb)
        while item is not _END and item is not None:
            pb.put(item, answer=True)
            item = self._next(audio, pb)
        if item is _END:
            pb.put(self._beep("out"))
        else:
            cancel.set()  # link lost or stopping: stop thinking and synthesising
        end = self._finish(pb)
        producer.join(None if item is _END else _JOIN_S)
        if end is None or item is not _END:
            return

        answer = " ".join(s for s in spoken if s != Brain.FAILURE)
        if answer:
            self.history.append({"role": "assistant", "content": answer})
            del self.history[:-HISTORY_MESSAGES]
        elif self.history and self.history[-1]["content"] == question:
            self.history.pop()  # failed turn: keep no dangling question
        log.info("%s answer: %s", self.name, " ".join(spoken))
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

    def _produce(self, history: list[dict], out: queue.Queue, spoken: list[str],
                 times: dict[str, float], cancel: threading.Event) -> None:
        """Brain sentences -> PCM, one at a time, so sentence N+1 is made while N plays."""
        answer = None
        try:
            answer = self.brain.answer(history, self.name, self.camera)
            for sentence in answer:
                if cancel.is_set():
                    return
                times.setdefault("first_sentence", self.clock())
                if sentence == Brain.FAILURE:
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
        """Mic PCM that arrived at clock time `t`; frames heard while the rover speaks are dropped."""
        t = self.clock() if t is None else t
        if t < self._mute_until:
            self._muted = True
            return
        if self._muted:
            self.segmenter.reset()  # forget a half-heard tail of the rover's own voice
            self._muted = False
        for u in self.segmenter.feed(chunk):
            self.handle(u)
            if self._mute_until > t:  # it spoke: the rest of this chunk overlapped it
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
                self._listen(proc)
            except (LinkError, SttError) as e:
                log.warning("%s: %s", self.name, e)
            except Exception:
                log.exception("%s: session error", self.name)
            self.connected = False
            if self._stop.is_set():
                break
            if self._got_audio:
                delay = BACKOFF_START_S
            log.info("%s: reconnecting in %.0f s", self.name, delay)
            self._sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

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
        finally:
            stop_mic(proc)
            reader.join(_JOIN_S)
            try:
                proc.stdout.close()
            except (OSError, AttributeError):
                pass

    def _read_mic(self, stream, chunks: queue.Queue) -> None:
        try:
            while chunk := stream.read1(MIC_CHUNK):
                chunks.put((self.clock(), chunk))
        except (OSError, ValueError):
            pass
        finally:
            chunks.put(None)
