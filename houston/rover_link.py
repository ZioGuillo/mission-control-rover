"""SSH link to a rover: mic and speaker via arecord/aplay, nothing installed there."""
from __future__ import annotations

import re
import subprocess
import time
from typing import Callable

_CARD = re.compile(r"^card \d+: (\S+) \[[^\]]*\], (.+)$")
_SSH_OPTS = ["ControlMaster=auto", "ControlPath=~/.ssh/houston-%r@%h:%p", "ControlPersist=10m",
             "ServerAliveInterval=5", "ConnectTimeout=5"]
_BUSY = "Device or resource busy"
_START_CHECK_S = 0.3     # aplay that is going to fail has exited by now
_RETRY_S = 0.5
_START_TRIES = 5
_CLOSE_TIMEOUT_S = 30.0  # let queued playback finish


class LinkError(Exception):
    pass


def parse_cards(listing: str) -> list[tuple[str, str]]:
    """(card_name, description) per card, first device line wins, in listing order."""
    cards: dict[str, str] = {}
    for line in listing.splitlines():
        m = _CARD.match(line)
        if m:
            cards.setdefault(m.group(1), m.group(2))
    return list(cards.items())


def pick_usb(listing: str) -> str | None:
    return next((n for n, d in parse_cards(listing) if "USB" in d), None)


class Speaker:
    """One `ssh ... aplay` playback stream; the constructor starts it (retrying while busy)."""

    def __init__(self, start: Callable[[], subprocess.Popen],
                 sleep: Callable[[float], None] = time.sleep) -> None:
        for attempt in range(_START_TRIES):
            proc = start()
            sleep(_START_CHECK_S)
            if proc.poll() is None:
                self._proc = proc
                return
            err = proc.stderr.read().decode(errors="replace").strip()
            if _BUSY not in err:
                raise LinkError(f"speaker failed to start: {err or proc.returncode}")
            if attempt < _START_TRIES - 1:
                sleep(_RETRY_S)
        raise LinkError(f"speaker busy after {_START_TRIES} tries")

    def write(self, pcm48k: bytes) -> None:
        try:
            self._proc.stdin.write(pcm48k)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise LinkError(f"speaker pipe broken: {e}") from e

    def close(self) -> None:
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=_CLOSE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class RoverLink:
    def __init__(self, ssh: str, run=subprocess.run, popen=subprocess.Popen,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.ssh = ssh
        self._run, self._popen, self._sleep = run, popen, sleep
        self._cards: tuple[str, str] | None = None

    def ssh_args(self) -> list[str]:
        args = ["ssh"]
        for o in _SSH_OPTS:
            args += ["-o", o]
        return args + [self.ssh]

    def _list(self, tool: str) -> str:
        try:
            r = self._run(self.ssh_args() + [f"{tool} -l"], capture_output=True, text=True,
                          timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LinkError(f"{self.ssh}: {tool} -l failed: {e}") from e
        if r.returncode != 0:
            raise LinkError(f"{self.ssh}: {tool} -l failed: {r.stderr.strip()}")
        return r.stdout

    def detect(self) -> tuple[str, str]:
        if self._cards is None:
            mic, spk = pick_usb(self._list("arecord")), pick_usb(self._list("aplay"))
            if not mic or not spk:
                raise LinkError(f"{self.ssh}: no USB mic/speaker (mic={mic}, speaker={spk})")
            self._cards = (mic, spk)
        return self._cards

    def mic(self) -> subprocess.Popen:
        mic, _ = self.detect()
        cmd = (f"exec arecord -q -D plughw:CARD={mic},DEV=0 -f S16_LE -r 16000 -c 1 -t raw")
        return self._popen(self.ssh_args() + [cmd], stdout=subprocess.PIPE)

    def speaker(self) -> Speaker:
        _, spk = self.detect()
        cmd = f"exec aplay -q -D plughw:CARD={spk},DEV=0 -t raw -f S16_LE -r 48000 -c 1 -"
        return Speaker(lambda: self._popen(self.ssh_args() + [cmd], stdin=subprocess.PIPE,
                                           stderr=subprocess.PIPE), sleep=self._sleep)
