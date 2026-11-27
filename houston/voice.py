"""Rover voices as 48 kHz raw PCM, plus the quindar beeps.

A rover's voice is either a Kokoro voice name ("em_alex": ~20x faster than real time, chosen by Pablo
after the 2026-10-29 live test) or a .wav sample that Chatterbox Multilingual clones (~0.5x real time:
human, but Fred went silent mid-answer)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Callable, Literal

FFMPEG = "/opt/homebrew/bin/ffmpeg"
TEMPO = 0.9
EXAGGERATION = 0.5
CFG_WEIGHT = 0.3
OUT_RATE = 48000
CACHE_ROOT = Path("~/Library/Caches/houston").expanduser()
AUDIO_DIR = Path(__file__).resolve().parent.parent / "app" / "audio"
_SLUG_MAX = 60
KOKORO_SPEED = 0.95
KOKORO_RATE = 24000
ESPEAK_LIB = "/opt/homebrew/lib/libespeak-ng.dylib"   # brew install espeak-ng
ESPEAK_DATA = "/opt/homebrew/share/espeak-ng-data"

# ElevenLabs: "elevenlabs:<voice_id>|<fallback>" — a cloned voice (Sofi = Sofía's real voice,
# 2026-11-26), and the Kokoro voice to use when the API fails (no internet, quota, outage).
ELEVEN_PREFIX = "elevenlabs:"
ELEVEN_URL = "https://api.elevenlabs.io"
ELEVEN_MODEL = "eleven_v4"   # Pablo picked v4 over flash by ear (~2.5 s vs ~1 s to first audio)
ELEVEN_SETTINGS = {"stability": 0.5, "similarity_boost": 0.8}
ELEVEN_RATE = 24000          # output_format=pcm_24000: raw S16_LE mono
ELEVEN_TIMEOUT_S = 20.0
ELEVEN_KEY_FILE = Path("~/.config/houston/elevenlabs_key").expanduser()

log = logging.getLogger(__name__)

_beeps: dict[str, bytes] = {}


def _ffmpeg(args: list[str], data: bytes) -> bytes:
    res = subprocess.run([FFMPEG, "-loglevel", "error", "-nostdin", *args], input=data,
                         capture_output=True)
    if res.returncode:
        raise RuntimeError(f"ffmpeg: {res.stderr.decode(errors='replace').strip()}")
    return res.stdout


def _default_factory(device: str | None):
    import torch  # heavy: only when the real model is needed
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    dev = device or ("mps" if torch.backends.mps.is_available() else "cpu")
    return ChatterboxMultilingualTTS.from_pretrained(device=dev)


def _kokoro_factory():
    # The espeakng-loader wheel points at its CI build path (/Users/runner/...): use Homebrew's espeak-ng.
    os.environ["ESPEAK_DATA_PATH"] = ESPEAK_DATA  # read directly by the espeak library
    from phonemizer.backend.espeak.wrapper import EspeakWrapper
    EspeakWrapper.set_library(ESPEAK_LIB)
    EspeakWrapper.set_data_path(ESPEAK_DATA)
    from kokoro import KPipeline
    return KPipeline(lang_code="e")  # Spanish


def _eleven(voice) -> tuple[str, str | None] | None:
    """"elevenlabs:ID|fallback" -> (ID, fallback or None); None for any other voice."""
    if not (isinstance(voice, str) and voice.startswith(ELEVEN_PREFIX)):
        return None
    voice_id, _, fallback = voice[len(ELEVEN_PREFIX):].partition("|")
    return voice_id.strip(), (fallback.strip() or None)


def _is_named(voice) -> bool:
    return isinstance(voice, str) and not voice.endswith(".wav") and _eleven(voice) is None


def _blend(spec: str) -> list[tuple[str, float]] | None:
    """"ef_dora:0.6,af_heart:0.4" -> [("ef_dora", 0.6), ("af_heart", 0.4)]; None for a plain name.
    Sofi (2026-11-26): Dora's Spanish with af_heart's younger timbre, picked by ear."""
    if ":" not in spec:
        return None
    parts = []
    for item in spec.split(","):
        name, _, weight = item.strip().partition(":")
        parts.append((name.strip(), float(weight)))
    return parts


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", text.lower()).strip("_")[:_SLUG_MAX]


class Voice:
    def __init__(self, device: str | None = None, model_factory: Callable = _default_factory,
                 cache_root: Path = CACHE_ROOT, kokoro_factory: Callable = _kokoro_factory,
                 eleven_url: str = ELEVEN_URL, eleven_key_file: Path = ELEVEN_KEY_FILE):
        self.device = device
        self._factory = model_factory
        self._kokoro_factory = kokoro_factory
        self._kokoro = None
        self._cache_root = Path(cache_root)
        self._model = None
        self._lock = threading.Lock()
        self._eleven_url = eleven_url.rstrip("/")
        self._eleven_key_file = Path(eleven_key_file)

    def synth(self, text: str, voice_wav: Path | str) -> bytes:
        """Spanish speech in the given voice: 48 kHz mono S16_LE raw."""
        eleven = _eleven(voice_wav)
        if eleven:
            voice_id, fallback = eleven
            try:
                return self._synth_eleven(text, voice_id)
            except Exception as e:
                if not fallback:
                    raise
                log.warning("elevenlabs failed, speaking with %s: %s", fallback, e)
                return self.synth(text, fallback)
        if _is_named(voice_wav):
            return self._synth_kokoro(text, voice_wav)
        with self._lock:
            if self._model is None:
                self._model = self._factory(self.device)
            wav = self._model.generate(text, language_id="es", exaggeration=EXAGGERATION, cfg_weight=CFG_WEIGHT,
                                       audio_prompt_path=str(voice_wav))
            samples = wav.detach().cpu().numpy().astype("<f4").reshape(-1)
            rate = int(self._model.sr)
        return _ffmpeg(["-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "-",
                        "-af", f"atempo={TEMPO}", "-ar", str(OUT_RATE), "-ac", "1",
                        "-f", "s16le", "-"], samples.tobytes())

    def _synth_eleven(self, text: str, voice_id: str) -> bytes:
        key = self._eleven_key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError(f"empty ElevenLabs key file {self._eleven_key_file}")
        body = {"text": text, "model_id": ELEVEN_MODEL, "language_code": "es", "voice_settings": ELEVEN_SETTINGS}
        req = urllib.request.Request(
            f"{self._eleven_url}/v1/text-to-speech/{voice_id}?output_format=pcm_{ELEVEN_RATE}",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "xi-api-key": key})
        with urllib.request.urlopen(req, timeout=ELEVEN_TIMEOUT_S) as resp:
            pcm = resp.read()
        if len(pcm) < 2:
            raise RuntimeError("elevenlabs returned no audio")
        return _ffmpeg(["-f", "s16le", "-ar", str(ELEVEN_RATE), "-ac", "1", "-i", "-",
                        "-ar", str(OUT_RATE), "-ac", "1", "-f", "s16le", "-"], pcm[: len(pcm) // 2 * 2])

    def _synth_kokoro(self, text: str, name: str) -> bytes:
        import numpy as np
        with self._lock:
            if self._kokoro is None:
                self._kokoro = self._kokoro_factory()
            blend = _blend(name)
            voice = name if blend is None else sum(w * self._kokoro.load_single_voice(v) for v, w in blend)
            chunks = [np.asarray(audio, dtype="<f4").reshape(-1)
                      for _, _, audio in self._kokoro(text, voice=voice, speed=KOKORO_SPEED)]
        samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype="<f4")
        return _ffmpeg(["-f", "f32le", "-ar", str(KOKORO_RATE), "-ac", "1", "-i", "-",
                        "-ar", str(OUT_RATE), "-ac", "1", "-f", "s16le", "-"], samples.tobytes())

    def cached(self, text: str, voice_wav: Path | str) -> bytes:
        eleven = _eleven(voice_wav)
        if eleven:
            voice_id, stem = f"elevenlabs:{eleven[0]}".encode(), _slug(f"elevenlabs_{eleven[0]}")
            tuning = f"{ELEVEN_MODEL}|{sorted(ELEVEN_SETTINGS.items())}".encode()
        elif _is_named(voice_wav):
            voice_id, stem = f"kokoro:{voice_wav}".encode(), voice_wav if _blend(voice_wav) is None else _slug(voice_wav)
            tuning = f"{KOKORO_SPEED}".encode()
        else:
            voice_wav = Path(voice_wav)
            try:
                voice_id = voice_wav.read_bytes()
            except OSError:
                voice_id = str(voice_wav).encode()
            stem = voice_wav.stem
            tuning = f"{TEMPO}|{EXAGGERATION}|{CFG_WEIGHT}".encode()  # retuned voice: new renders
        key = hashlib.sha1(text.encode() + b"\0" + voice_id + b"\0" + tuning).hexdigest()[:12]
        path = self._cache_root / stem / f"{_slug(text)}-{key}.raw"
        try:
            return path.read_bytes()
        except FileNotFoundError:
            pass
        if eleven:
            try:
                raw = self._synth_eleven(text, eleven[0])
            except Exception as e:  # never cache the fallback voice under the cloned voice's name
                if not eleven[1]:
                    raise
                log.warning("elevenlabs failed for a fixed phrase, using %s (not cached): %s", eleven[1], e)
                return self.cached(text, eleven[1])
        else:
            raw = self.synth(text, voice_wav)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}-{threading.get_ident()}")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
        return raw


def sound(path: Path) -> bytes:
    """A mono 16-bit WAV as 48 kHz raw PCM, cached in memory."""
    key = str(path)
    if key not in _beeps:
        with wave.open(key) as w:
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        _beeps[key] = _ffmpeg(["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
                               "-ar", str(OUT_RATE), "-f", "s16le", "-"], pcm)
    return _beeps[key]


def beep(name: Literal["in", "out"]) -> bytes:
    return sound(AUDIO_DIR / f"quindar_{name}.wav")
