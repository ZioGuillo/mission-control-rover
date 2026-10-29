"""One answer's playback: a writer thread feeds a Speaker so its blocking writes never stall synthesis."""
from __future__ import annotations

import queue
import threading
from typing import Callable

from houston.rover_link import LinkError

BYTES_PER_S = 48000 * 2  # 48 kHz mono S16_LE


class Playback:
    def __init__(self, speaker, clock: Callable[[], float], prelude: int = 0) -> None:
        """`prelude`: bytes the Speaker already wrote while starting (the beep)."""
        self._speaker, self._clock = speaker, clock
        self._q: queue.Queue = queue.Queue()
        self.error: LinkError | None = None
        # clock when the first byte went to the speaker
        self.started: float | None = clock() if prelude else None
        self.first_answer: float | None = None  # clock when the first answer (not beep/filler) did
        self.written = prelude
        self._thread = threading.Thread(target=self._run, name="houston-playback", daemon=True)
        self._thread.start()

    @property
    def failed(self) -> bool:
        return self.error is not None

    def put(self, pcm: bytes, answer: bool = False) -> None:
        self._q.put((pcm, answer))

    def _run(self) -> None:
        while (item := self._q.get()) is not None:
            if self.error:
                continue  # link is gone: drain so finish() never waits on a dead pipe
            pcm, answer = item
            now = self._clock()
            if self.started is None:
                self.started = now
            if answer and self.first_answer is None:
                self.first_answer = now
            try:
                self._speaker.write(pcm)
            except LinkError as e:
                self.error = e
                continue
            self.written += len(pcm)

    def finish(self) -> float:
        """Wait for everything queued to play; return the clock when the rover went quiet.

        Raises LinkError if the speaker broke at any point (after closing it either way);
        `end` is set in both cases."""
        self._q.put(None)
        self._thread.join()
        try:
            self._speaker.close()  # waits for aplay to drain
        except LinkError as e:
            self.error = self.error or e
        end = self._clock()
        if self.started is not None:
            end = max(end, self.started + self.written / BYTES_PER_S)
        self.end = end
        if self.error:
            raise self.error
        return end
