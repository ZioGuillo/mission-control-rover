"""Energy-based speech segmenter: raw S16_LE mono PCM in, whole utterances out."""
from __future__ import annotations

import array
import math
import sys
from collections import deque
from dataclasses import dataclass

_MIN_THRESHOLD = 150.0   # absolute RMS floor for "voiced" (16-bit sample units)
_FLOOR_RATIO = 3.0       # voiced = louder than this multiple of the noise floor
_FLOOR_DECAY = 0.995     # noise-floor EMA weight on the old value
_ONSET_FRAMES = 3        # consecutive voiced frames that start an utterance
_PREROLL_FRAMES = 10     # quiet frames kept before the onset
_SEED_FLOOR = _MIN_THRESHOLD / _FLOOR_RATIO  # start/reset value of the noise floor
_STUCK_RATIO = 2.0       # a max-length clip whose quiet frames beat floor*this is noise
_FLAT_SPREAD = 0.1       # frame-RMS std/mean below this is stationary noise, not speech dips
_TAIL_FRAMES = 7         # trailing quiet frames kept after the last speech


@dataclass
class Utterance:
    pcm: bytes
    seconds: float
    peak_rms: float  # loudest single frame, same units as the samples


def _rms(frame: bytes) -> float:
    samples = array.array("h", frame)
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class Segmenter:
    """Feed arbitrary-size chunks; completed utterances come back from feed()."""

    def __init__(self, rate: int = 16000, frame_ms: int = 30, end_silence: float = 0.5,
                 max_seconds: float = 15.0, min_seconds: float = 0.4) -> None:
        self._rate = rate
        self._frame_bytes = rate * frame_ms // 1000 * 2
        self._end_frames = math.ceil(end_silence * 1000 / frame_ms)
        self._max_bytes = int(max_seconds * rate) * 2
        self._min_bytes = int(min_seconds * rate) * 2
        self.reset()

    @property
    def noise_floor(self) -> float:
        return self._floor

    def reset(self, keep_floor: bool = False) -> None:
        """Forget buffered and partial speech. keep_floor=True keeps what was learned about the
        room (noise floor, stuck-noise state): use it after a mute; a full reset on a new stream."""
        self._pending = bytearray()
        if not keep_floor:
            self._floor = _SEED_FLOOR
            self._after_cut = False  # previous utterance ended on a max-length cut
        self._speaking = False
        self._voiced = 0
        self._quiet = 0
        self._peak = 0.0
        self._rmss: list[float] = []
        self._lead = 0
        self._buf = bytearray()
        # onset frames are part of the ring, so keep quiet pre-roll plus the onset
        self._pre: deque[tuple[bytes, float]] = deque(maxlen=_PREROLL_FRAMES + _ONSET_FRAMES)

    def feed(self, chunk: bytes) -> list[Utterance]:
        self._pending += chunk
        out: list[Utterance] = []
        n = self._frame_bytes
        usable = len(self._pending) // n * n
        for i in range(0, usable, n):
            u = self._frame(bytes(self._pending[i:i + n]))
            if u:
                out.append(u)
        del self._pending[:usable]
        return out

    def _frame(self, frame: bytes) -> Utterance | None:
        e = _rms(frame)
        thr = max(self._floor * _FLOOR_RATIO, _MIN_THRESHOLD)
        if e <= thr:  # speech never drags the floor up
            self._floor = self._floor * _FLOOR_DECAY + e * (1 - _FLOOR_DECAY)

        if not self._speaking:
            self._pre.append((frame, e))
            self._voiced = self._voiced + 1 if e > thr else 0
            if self._voiced >= _ONSET_FRAMES:
                self._speaking, self._quiet = True, 0
                self._lead = (len(self._pre) - _ONSET_FRAMES) * self._frame_bytes
                self._buf = bytearray(b"".join(f for f, _ in self._pre))
                self._peak = max(r for _, r in self._pre)
                self._rmss = [r for _, r in list(self._pre)[-_ONSET_FRAMES:]]
            return None

        n = self._frame_bytes
        self._buf += frame
        self._peak = max(self._peak, e)
        self._rmss.append(e)
        self._quiet = self._quiet + 1 if e < thr else 0
        cut = len(self._buf) >= self._max_bytes
        if cut and self._quiet < self._end_frames:
            ranked = sorted(self._rmss)
            quiet_level = ranked[len(ranked) // 5]
            loud_level = ranked[len(ranked) * 9 // 10]
            mean = sum(ranked) / len(ranked)
            spread = math.sqrt(sum((r - mean) ** 2 for r in ranked) / len(ranked)) / mean
            flat_noise = (quiet_level > self._floor * _STUCK_RATIO
                          and loud_level < quiet_level * _STUCK_RATIO and spread < _FLAT_SPREAD)
            if flat_noise or self._after_cut:
                # flat loud energy, or over 15 s of unbroken sound twice running: noise rose, not speech
                self._floor = quiet_level
                self._after_cut = True
                self._speaking, self._voiced, self._buf = False, 0, bytearray()
                self._pre.clear()
                return None
        if self._quiet >= self._end_frames or cut:
            # min_seconds counts speech only, not pre-roll or the trailing silence
            speech = len(self._buf) - self._lead - self._quiet * n
            drop = max(0, self._quiet - _TAIL_FRAMES) * n
            pcm = bytes(self._buf[:len(self._buf) - drop])
            peak = self._peak
            self._after_cut = cut and self._quiet < self._end_frames
            self._speaking, self._voiced, self._buf = False, 0, bytearray()
            self._pre.clear()
            if speech >= self._min_bytes:
                return Utterance(pcm, len(pcm) / 2 / self._rate, peak)
        return None
