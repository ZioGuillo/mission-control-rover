"""SSH link to a rover: mic and speaker via arecord/aplay, nothing installed there."""
from __future__ import annotations

import re
import subprocess
import time
from typing import Callable

_CARD = re.compile(r"^card \d+: (\S+) \[[^\]]*\], (.+)$")
_SSH_OPTS = ["ControlMaster=auto", "ControlPath=~/.ssh/houston-%C", "ControlPersist=10m",
             "ServerAliveInterval=5", "ConnectTimeout=5"]
_BUSY = "Device or resource busy"
_START_CHECK_S = 0.3     # aplay that is going to fail has exited by now
_POLL_S = 0.05
_MASTER_TIMEOUT_S = 6.0
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


def _reap(proc: subprocess.Popen) -> None:
    """Close the pipes of a finished/killed process and collect it."""
    for f in (proc.stdin, proc.stderr):
        try:
            if f:
                f.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def stop_mic(proc: subprocess.Popen) -> None:
    proc.kill()
    _reap(proc)


class Speaker:
    """One `ssh ... aplay` playback stream; the constructor starts it (retrying while busy)."""

    def __init__(self, start: Callable[[], subprocess.Popen],
                 sleep: Callable[[float], None] = time.sleep) -> None:
        for attempt in range(_START_TRIES):
            try:
                proc = start()
            except OSError as e:
                raise LinkError(f"speaker failed to start: {e}") from e
            waited = 0.0
            while waited < _START_CHECK_S and proc.poll() is None:
                sleep(_POLL_S)
                waited += _POLL_S
            if proc.poll() is None:
                self._proc = proc
                return
            err = self._stderr(proc)
            _reap(proc)
            if _BUSY not in err:
                raise LinkError(f"speaker failed to start: {err or proc.returncode}")
            if attempt < _START_TRIES - 1:
                sleep(_RETRY_S)
        raise LinkError(f"speaker busy after {_START_TRIES} tries")

    @staticmethod
    def _stderr(proc: subprocess.Popen) -> str:
        try:
            return proc.stderr.read().decode(errors="replace").strip()
        except (OSError, ValueError):
            return ""

    def write(self, pcm48k: bytes) -> None:
        try:
            self._proc.stdin.write(pcm48k)
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise LinkError(f"speaker pipe broken: {e}") from e

    def close(self) -> None:
        proc = self._proc
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=_CLOSE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            _reap(proc)
            return
        err = self._stderr(proc) if proc.returncode else ""
        _reap(proc)
        if proc.returncode:
            raise LinkError(f"aplay exited {proc.returncode}: {err}")


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
                          stdin=subprocess.DEVNULL, timeout=15)
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
        try:
            return self._popen(self.ssh_args() + [cmd], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE)
        except OSError as e:
            raise LinkError(f"{self.ssh}: cannot start mic: {e}") from e

    def _ensure_master(self) -> None:
        """A warm ControlMaster makes aplay's start fast and an unreachable host fail early."""
        args = self.ssh_args()
        try:
            if self._run(args[:-1] + ["-O", "check", args[-1]], capture_output=True,
                         stdin=subprocess.DEVNULL, timeout=_MASTER_TIMEOUT_S).returncode == 0:
                return
            r = self._run(args + ["true"], capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=_MASTER_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LinkError(f"{self.ssh}: unreachable: {e}") from e
        if r.returncode != 0:
            raise LinkError(f"{self.ssh}: unreachable")

    def speaker(self) -> Speaker:
        _, spk = self.detect()
        self._ensure_master()
        cmd = f"exec aplay -q -D plughw:CARD={spk},DEV=0 -t raw -f S16_LE -r 48000 -c 1 -"
        return Speaker(lambda: self._popen(self.ssh_args() + [cmd], stdin=subprocess.PIPE,
                                           stderr=subprocess.PIPE), sleep=self._sleep)
