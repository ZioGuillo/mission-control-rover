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
    # speech-like: short pauses (under end_silence) so quiet frames exist
    out = run(noise(1) + (tone(0.7) + noise(0.3)) * 20)
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
    assert seg.noise_floor == 50.0
    seg.feed(noise(5))
    assert 23 < seg.noise_floor < 35
    seg.reset()
    assert seg.noise_floor == 50.0


def test_reset_keep_floor_keeps_learned_floor_but_drops_partial_speech():
    seg = Segmenter()
    seg.feed(noise(5))
    floor = seg.noise_floor
    seg.feed(tone(0.6))  # mid-utterance
    seg.reset(keep_floor=True)
    assert seg.noise_floor == floor
    assert seg.feed(noise(1)) == []  # the half-heard tone is gone


def test_noisy_room_floor_survives_keep_floor_reset():
    # RMS ~300 swelling noise (TV, fan + traffic): over the seed threshold, too uneven to be "flat"
    seg = Segmenter()
    sig = fluctuating_noise(40, amp=520)
    assert len([u for i in range(0, len(sig), 3200) for u in seg.feed(sig[i:i + 3200])]) <= 1
    assert seg.noise_floor > 200
    for keep, expected in ((True, 0), (False, 1)):
        seg.reset(keep_floor=keep)
        sig = fluctuating_noise(20, amp=520, seed=4)
        out = [u for i in range(0, len(sig), 3200) for u in seg.feed(sig[i:i + 3200])]
        assert len(out) == expected  # a full reset relearns by emitting 15 s of noise


def test_floor_frozen_while_speaking():
    seg = Segmenter()
    seg.feed(noise(3))
    before = seg.noise_floor
    seg.feed(tone(1))
    assert seg.noise_floor < before * 1.5


def test_speech_at_stream_start_is_heard():
    assert len(run(tone(1.5) + noise(2) + tone(1.5) + noise(1))) == 2


def test_speech_right_after_reset_is_heard():
    seg = Segmenter()
    seg.feed(noise(2))
    seg.reset()
    sig = tone(1.5) + noise(2) + tone(1.5) + noise(1)
    out = [u for i in range(0, len(sig), 3200) for u in seg.feed(sig[i:i + 3200])]
    assert len(out) == 2


def test_sustained_noise_step_is_absorbed():
    seg = Segmenter()
    sig = noise(2) + noise(60, 400, seed=2)
    out = [u for i in range(0, len(sig), 3200) for u in seg.feed(sig[i:i + 3200])]
    assert out == []
    assert seg.noise_floor >= 150  # amplitude-400 uniform noise has RMS ~231


def test_short_pause_inside_speech_stays_one_utterance():
    assert len(run(noise(1) + tone(1) + noise(0.3) + tone(1) + noise(1))) == 1


def test_continuous_speech_with_short_gaps_is_not_mistaken_for_noise():
    seg = Segmenter()
    sig = noise(2) + (tone(0.6) + noise(0.05)) * 31 + noise(1)
    out = [u for i in range(0, len(sig), 3200) for u in seg.feed(sig[i:i + 3200])]
    assert len(out) == 2
    assert 14.9 < out[0].seconds <= 15.05
    assert seg.noise_floor < 100


def fluctuating_noise(seconds, amp=400, depth=0.3, seed=3):
    rng = random.Random(seed)
    n = int(RATE * seconds)
    return pcm([rng.randint(-a, a) for a in
                (int(amp * (1 + depth * math.sin(2 * math.pi * i / RATE))) for i in range(n))])


def test_fluctuating_noise_step_is_absorbed():
    seg = Segmenter()
    sig = noise(2) + fluctuating_noise(60)
    out = [u for i in range(0, len(sig), 3200) for u in seg.feed(sig[i:i + 3200])]
    assert len(out) <= 1
    assert seg.noise_floor >= 150
    sig = noise(2) + tone(1, 3000) + noise(2)
    out = [u for i in range(0, len(sig), 3200) for u in seg.feed(sig[i:i + 3200])]
    assert len(out) == 1


def test_thinking_pause_mid_question_stays_one_utterance():
    # Live 2026-10-29: "¿Me puedes decir algún…" and "¿Cuánto dura una transmisión…" were cut off at
    # a 0.5 s thinking pause. People pause ~0.6 s mid-question; the utterance must hold together.
    assert len(run(noise(1) + tone(1) + noise(0.6) + tone(1) + noise(1.5))) == 1
