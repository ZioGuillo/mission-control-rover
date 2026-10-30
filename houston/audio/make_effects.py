"""Synthesise the story sound effects in effects/ (python3 houston/audio/make_effects.py).

Made from noise and sine waves, so there is nothing to download or license. The bark is
Bentley's own barks_soft.wav (see CREDITS.txt).
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

RATE = 24000
OUT = Path(__file__).parent / "effects"
rng = np.random.default_rng(7)


def t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * RATE)) / RATE


def noise(seconds: float) -> np.ndarray:
    return rng.standard_normal(int(seconds * RATE))


def band(x: np.ndarray, low: float | None, high: float | None) -> np.ndarray:
    if low and high:
        sos = butter(4, [low, high], "bandpass", fs=RATE, output="sos")
    elif high:
        sos = butter(4, high, "lowpass", fs=RATE, output="sos")
    else:
        sos = butter(4, low, "highpass", fs=RATE, output="sos")
    return sosfilt(sos, x)


def envelope(n: int, attack: float, release: float) -> np.ndarray:
    a, r = int(attack * RATE), int(release * RATE)
    env = np.ones(n)
    env[:a] = np.linspace(0, 1, a)
    env[n - r:] *= np.linspace(1, 0, r) ** 2
    return env


def roar() -> np.ndarray:
    """A big, friendly growl: a low sweeping tone roughened by noise."""
    tt = t(2.4)
    f = 110 - 45 * tt / tt[-1] + 6 * np.sin(2 * np.pi * 7 * tt)  # sweeps down, with a wobble
    phase = 2 * np.pi * np.cumsum(f) / RATE
    tone = sum(np.sin(k * phase) / k for k in range(1, 12))
    grit = band(noise(2.4), 80, 900) * (0.6 + 0.4 * np.sin(2 * np.pi * 23 * tt))
    return (tone * (0.7 + 0.5 * band(noise(2.4), None, 30)) + 1.2 * grit) * envelope(len(tt), 0.25, 0.9)


def wings() -> np.ndarray:
    """Four slow wing beats: soft whooshes of low noise."""
    out = np.zeros(int(2.2 * RATE))
    for start in (0.0, 0.5, 1.0, 1.5):
        beat = band(noise(0.45), 60, 700) * envelope(int(0.45 * RATE), 0.08, 0.3)
        i = int(start * RATE)
        out[i:i + len(beat)] += beat
    return out


def fire() -> np.ndarray:
    """A breath of fire: a swelling whoosh with crackles."""
    n = int(2.4 * RATE)
    swell = np.sin(np.linspace(0, np.pi, n)) ** 1.5
    whoosh = band(noise(2.4), 300, 3500) * swell
    crackle = np.zeros(n)
    for i in rng.integers(0, n - 200, 60):
        crackle[i:i + 120] += rng.uniform(0.5, 1.5) * np.exp(-np.arange(120) / 15) * rng.choice([-1, 1])
    return whoosh + 0.8 * band(crackle, 1500, None)


def thunder() -> np.ndarray:
    """A crack, then a long low rumble."""
    n = int(3.2 * RATE)
    crack = band(noise(3.2), 400, None) * np.exp(-t(3.2) / 0.06)
    rumble = band(noise(3.2), None, 160) * (0.5 + 0.5 * band(noise(3.2), None, 4) * 8)
    return 1.5 * crack + 2.5 * rumble * envelope(n, 0.05, 2.2)


def magic() -> np.ndarray:
    """A rising sparkle of bell notes."""
    out = np.zeros(int(1.8 * RATE))
    for k, freq in enumerate((1047, 1319, 1568, 2093, 2637)):
        tt = t(1.2)
        bell = (np.sin(2 * np.pi * freq * tt) + 0.3 * np.sin(2 * np.pi * 2.76 * freq * tt)) * np.exp(-tt / 0.35)
        i = int(k * 0.12 * RATE)
        out[i:i + len(bell)] += bell
    return out


def write(name: str, x: np.ndarray, peak: float = 0.6) -> None:
    x = x / np.max(np.abs(x)) * peak
    pcm = (x * 32767).astype("<i2").tobytes()
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    write("rugido", roar())
    write("alas", wings())
    write("fuego", fire())
    write("trueno", thunder(), peak=0.7)
    write("magia", magic(), peak=0.45)
    print(sorted(p.name for p in OUT.glob("*.wav")))
