"""Rover voices: Chatterbox Multilingual (Spanish) plus the quindar beeps, as 48 kHz raw PCM."""
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


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", text.lower()).strip("_")[:_SLUG_MAX]


class Voice:
    def __init__(self, device: str | None = None, model_factory: Callable = _default_factory,
                 cache_root: Path = CACHE_ROOT):
        self.device = device
        self._factory = model_factory
        self._cache_root = Path(cache_root)
        self._model = None
        self._lock = threading.Lock()

    def synth(self, text: str, voice_wav: Path) -> bytes:
        """Spanish speech in the given voice: 48 kHz mono S16_LE raw, slowed to TEMPO, pitch kept."""
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

    def cached(self, text: str, voice_wav: Path) -> bytes:
        voice_wav = Path(voice_wav)
        try:
            voice_id = voice_wav.read_bytes()
        except OSError:
            voice_id = str(voice_wav).encode()
        tuning = f"{TEMPO}|{EXAGGERATION}|{CFG_WEIGHT}".encode()  # retuned voice: new renders
        key = hashlib.sha1(text.encode() + b"\0" + voice_id + b"\0" + tuning).hexdigest()[:12]
        path = self._cache_root / voice_wav.stem / f"{_slug(text)}-{key}.raw"
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


def beep(name: Literal["in", "out"]) -> bytes:
    if name not in _beeps:
        path = AUDIO_DIR / f"quindar_{name}.wav"
        with wave.open(str(path)) as w:
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        _beeps[name] = _ffmpeg(["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
                                "-ar", str(OUT_RATE), "-f", "s16le", "-"], pcm)
    return _beeps[name]
