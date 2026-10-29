import math
import random
import struct

from houston.vad import Segmenter, Utterance

RATE = 16000


def pcm(samples) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def noise(seconds, amp=40, seed=1):
    rng = random.Random(seed)
    return pcm([rng.randint(-amp, amp) for _ in range(int(RATE * seconds))])


def ramp_noise(seconds, lo, hi, seed=1):
    rng = random.Random(seed)
    n = int(RATE * seconds)
    return pcm([rng.randint(-a, a) for a in
                (int(lo + (hi - lo) * i / n) for i in range(n))])


def tone(seconds, amp=3000, hz=300):
    n = int(RATE * seconds)
    return pcm([int(amp * math.sin(2 * math.pi * hz * i / RATE)) for i in range(n)])


def run(signal: bytes, chunk=3200) -> list[Utterance]:
    seg = Segmenter()
    out = []
    for i in range(0, len(signal), chunk):
        out += seg.feed(signal[i:i + chunk])
    return out


def test_one_burst_one_utterance():
    out = run(noise(1) + tone(1) + noise(1))
    assert len(out) == 1
    assert 0.9 < out[0].seconds < 1.8
    assert out[0].seconds == len(out[0].pcm) / 2 / RATE
    assert out[0].peak_rms > 1500


def test_short_silence_does_not_end_utterance():
    assert run(noise(1) + tone(1) + noise(0.3)) == []


def test_continuous_tone_cut_at_max():
    out = run(noise(1) + tone(20))
    assert out and 14.9 < out[0].seconds <= 15.05


def test_click_below_min_seconds_ignored():
    assert run(noise(1) + tone(0.2) + noise(1)) == []


def test_floor_adapts_to_gradual_noise_rise():
    assert run(noise(1) + ramp_noise(10, 40, 400) + noise(1, 400)) == []


def test_preroll_included():
    out = run(noise(1) + tone(1) + noise(1))
    first_loud = next(i for i, s in enumerate(struct.unpack(f"<{len(out[0].pcm) // 2}h", out[0].pcm))
                      if abs(s) > 1000)
    assert first_loud / RATE >= 0.25


def test_any_chunking_gives_identical_result():
    sig = noise(1) + tone(1) + noise(1) + tone(0.8, 2000) + noise(1)
    big = run(sig, chunk=len(sig))
    assert len(big) == 2
    assert run(sig, chunk=7) == big
    assert run(sig, chunk=1) == big


def test_noise_floor_property_and_reset():
    seg = Segmenter()
    assert seg.noise_floor == 0.0
    seg.feed(noise(1))
    assert 15 < seg.noise_floor < 40
    seg.reset()
    assert seg.noise_floor == 0.0


def test_floor_frozen_while_speaking():
    seg = Segmenter()
    seg.feed(noise(1))
    before = seg.noise_floor
    seg.feed(tone(1))
    assert seg.noise_floor < before * 3
