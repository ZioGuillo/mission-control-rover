"""Render the rover's mission-control voice lines to app/audio/*.wav.

Run on a computer, not the robot (the robot only plays the WAVs):

    python3.11 -m venv .venv-voice && .venv-voice/bin/pip install piper-tts scipy numpy
    .venv-voice/bin/python render_voice.py          # downloads the voice once

Voice: Piper "en_US-joe-medium" — its training data is CC0, so the rendered
audio can live in the repo with no strings attached. (en_US-ryan is
CC BY-NC-SA — avoid it.)

The NASA sound, on top of the voice (knobs below LINES' constants):
  - Quindar tones: the beeps that framed every Apollo capcom call —
    2525 Hz before a transmission, 2475 Hz after, 250 ms each.
  - Radio: voice pitched down 2.5 semitones, 200–3000 Hz band, soft
    clipping, hiss, crackle and a squelch burst at key-up/release.
The tones are separate files so the robot can frame any sequence of
lines: quindar_in, line, line…, quindar_out (app/hardware/voice.py).

Change LINES and re-run to change what it says.
"""
import io
import urllib.request
import wave
from pathlib import Path

import numpy as np
from scipy import signal

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "app" / "audio"
VOICE_DIR = Path(__file__).resolve().parent / ".voice-model"
VOICE = "en_US-joe-medium"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/joe/medium/"
RATE = 22050

# ── Sound knobs ───────────────────────────────────────────────────────
# Deeper voice: pitch down by resampling, with Piper speaking faster by the
# same factor first so the pace stays capcom-steady (resampling alone would
# also slow it down). Shifts the formants too, so it sounds bigger, not
# just lower.
PITCH_SEMITONES = -2.5
SPEECH_PACE = 1.08    # Piper length_scale before the pitch shift: >1 = slower
BAND = (200, 3000)    # radio voice band, Hz (200 keeps the deeper voice's body)
HISS = 0.05           # steady background noise, relative to the voice peak
CRACKLE = 0.25        # strength of random crackle pops
CRACKLES_PER_S = 9
SQUELCH_S = 0.12      # "kshh" noise burst when the mic keys up / releases

# Every call opens with a callsign clip, then the message. One callsign per
# unit number (units.txt), so each rover is called by its own number;
# app/hardware/voice.py falls back to plain "callsign" for others.
MAX_UNIT = 20
_NUMBERS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
            "fourteen fifteen sixteen seventeen eighteen nineteen twenty").split()

LINES = {
    "callsign": "RoboControl, this is Houston.",
    **{f"callsign_unit_{n:02d}": f"RoboControl, unit {_NUMBERS[n]}, this is Houston."
       for n in range(1, MAX_UNIT + 1)},
    "nominal": "All systems nominal. All systems connected. You are go for mission.",
    "check": "Systems check complete.",
    "camera_offline": "Camera, offline.",
    "vision_offline": "Vision, offline.",
    "motors_offline": "Motor control, offline.",
    "caution": "Proceed with caution.",
    "test": "Radio check. How do you read?",
}


def _voice():
    from piper import PiperVoice

    VOICE_DIR.mkdir(exist_ok=True)
    for name in (f"{VOICE}.onnx", f"{VOICE}.onnx.json"):
        path = VOICE_DIR / name
        if not path.exists():
            print("downloading", name)
            urllib.request.urlretrieve(VOICE_URL + name, path)
    return PiperVoice.load(VOICE_DIR / f"{VOICE}.onnx")


def synthesize(voice, text: str) -> np.ndarray:
    from piper.config import SynthesisConfig

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        # faster by the pitch factor: resampling below stretches it back
        voice.synthesize_wav(text, w, syn_config=SynthesisConfig(
            length_scale=SPEECH_PACE / _pitch_factor(), noise_scale=0.5))
    buf.seek(0)
    with wave.open(buf) as w:
        rate = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    # pitch down: stretch to more samples, then play at the same rate
    return signal.resample(audio, int(len(audio) * _pitch_factor() * RATE / rate))


def _pitch_factor() -> float:
    return 2 ** (-PITCH_SEMITONES / 12)


def _band_noise(rng, n: int, sos) -> np.ndarray:
    noise = signal.sosfilt(sos, rng.normal(0, 1, n))
    return noise / (np.abs(noise).max() + 1e-9)


def radio(audio: np.ndarray, seed: int = 0) -> np.ndarray:
    """Air-to-ground loop: voice band only, overdriven, hiss and crackle,
    with a squelch burst as the mic keys up and releases."""
    sos = signal.butter(4, list(BAND), btype="bandpass", fs=RATE, output="sos")
    rng = np.random.default_rng(seed)
    pad = np.zeros(int(0.08 * RATE))
    x = signal.sosfilt(sos, np.concatenate([pad, audio, pad]))
    x = np.tanh(2.2 * x / (np.abs(x).max() + 1e-9))  # soft clipping
    # hiss under the whole transmission
    x = x + HISS * _band_noise(rng, len(x), sos)
    # crackle: short decaying pops at random moments
    pop = np.exp(-np.arange(int(0.004 * RATE)) / (0.0008 * RATE))
    for at in rng.integers(0, len(x) - len(pop), int(CRACKLES_PER_S * len(x) / RATE)):
        x[at:at + len(pop)] += CRACKLE * rng.uniform(0.3, 1.0) * rng.choice([-1, 1]) * pop
    # squelch: a burst of louder noise at key-up and release
    sq = int(SQUELCH_S * RATE)
    burst = 0.35 * _band_noise(rng, sq, sos) * np.linspace(1, 0.2, sq)
    return np.concatenate([burst, x, burst[::-1]])


def tone(freq: float, seconds: float = 0.25) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    x = np.sin(2 * np.pi * freq * t)
    ramp = int(0.005 * RATE)  # 5 ms fades: no clicks
    env = np.ones_like(x)
    env[:ramp] = np.linspace(0, 1, ramp)
    env[-ramp:] = np.linspace(1, 0, ramp)
    return 0.5 * x * env


def save(name: str, audio: np.ndarray) -> None:
    audio = audio / (np.abs(audio).max() + 1e-9) * 0.89  # -1 dBFS peak
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    print(f"{name}.wav  {len(audio) / RATE:.1f} s")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    gap = np.zeros(int(0.12 * RATE))
    save("quindar_in", np.concatenate([tone(2525), gap]))
    save("quindar_out", np.concatenate([gap, tone(2475)]))
    voice = _voice()
    for i, (name, text) in enumerate(LINES.items()):
        save(name, radio(synthesize(voice, text), seed=i))


if __name__ == "__main__":
    main()
