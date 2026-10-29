"""Rover voices as 48 kHz raw PCM, plus the quindar beeps.

A rover's voice is either a Kokoro voice name ("em_alex": ~20x faster than real time, chosen by Pablo
after the 2026-10-29 live test) or a .wav sample that Chatterbox Multilingual clones (~0.5x real time:
human, but Fred went silent mid-answer)."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
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


def _is_named(voice) -> bool:
    return isinstance(voice, str) and not voice.endswith(".wav")


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", text.lower()).strip("_")[:_SLUG_MAX]


class Voice:
    def __init__(self, device: str | None = None, model_factory: Callable = _default_factory,
                 cache_root: Path = CACHE_ROOT, kokoro_factory: Callable = _kokoro_factory):
        self.device = device
        self._factory = model_factory
        self._kokoro_factory = kokoro_factory
        self._kokoro = None
        self._cache_root = Path(cache_root)
        self._model = None
        self._lock = threading.Lock()

    def synth(self, text: str, voice_wav: Path | str) -> bytes:
        """Spanish speech in the given voice: 48 kHz mono S16_LE raw."""
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

    def _synth_kokoro(self, text: str, name: str) -> bytes:
        import numpy as np
        with self._lock:
            if self._kokoro is None:
                self._kokoro = self._kokoro_factory()
            chunks = [np.asarray(audio, dtype="<f4").reshape(-1)
                      for _, _, audio in self._kokoro(text, voice=name, speed=KOKORO_SPEED)]
        samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype="<f4")
        return _ffmpeg(["-f", "f32le", "-ar", str(KOKORO_RATE), "-ac", "1", "-i", "-",
                        "-ar", str(OUT_RATE), "-ac", "1", "-f", "s16le", "-"], samples.tobytes())

    def cached(self, text: str, voice_wav: Path | str) -> bytes:
        if _is_named(voice_wav):
            voice_id, stem = f"kokoro:{voice_wav}".encode(), voice_wav
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
