"""Opt-in, time-boxed diagnostic: what Whisper wrote for SHORT utterances (wake-word tuning).

Off unless ~/.config/houston/diag_until holds a future epoch time. Only utterances up to
MAX_SECONDS are written — that is "Hey Bentley", not room conversation — to a separate file
that is deleted after reading. Never on by default (privacy rule)."""
from __future__ import annotations

import time
from pathlib import Path

MAX_SECONDS = 2.5
UNTIL_FILE = Path("~/.config/houston/diag_until").expanduser()
LOG_FILE = Path("~/Library/Logs/houston-diag.log").expanduser()


class Diag:
    def __init__(self, until_file: Path = UNTIL_FILE, log_file: Path = LOG_FILE) -> None:
        self.until_file, self.log_file = Path(until_file), Path(log_file)

    def active(self) -> bool:
        try:
            return time.time() < float(self.until_file.read_text().strip())
        except (OSError, ValueError):
            return False

    def record(self, name: str, seconds: float, text: str, kind: str) -> None:
        if seconds > MAX_SECONDS or not self.active():
            return
        stamp = time.strftime("%H:%M:%S")
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {name} {seconds:.1f}s {kind}: {text}\n")
