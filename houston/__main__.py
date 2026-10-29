"""python -m houston [--config PATH]: one voice session per rover, supervised."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from houston import config
from houston.brain import Brain
from houston.session import RoverSession
from houston.stt import Transcriber
from houston.voice import Voice

log = logging.getLogger("houston")

DEFAULT_CONFIG = Path(__file__).resolve().parent / "rovers.toml"
DEFAULT_LOG = Path("~/Library/Logs/houston.log").expanduser()
RESTART_DELAY_S = 5.0
JOIN_S = 10.0


def _setup_logging(path: Path) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "huggingface_hub", "chatterbox"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handler


def _preload(voice: Voice, wav: Path) -> None:
    """Load the TTS model now (first synth costs ~50 s); sessions listen meanwhile."""
    try:
        voice.synth("Hola.", wav)
        log.info("voice model ready")
    except Exception:
        log.exception("voice preload failed")


def _supervise(rover, make_session, sessions: dict, stop: threading.Event, delay: float) -> None:
    while not stop.is_set():
        session = make_session(rover)
        sessions[rover.name] = session
        if stop.is_set():
            session.stop()
            return
        try:
            session.run()
        except Exception:
            log.exception("%s: session crashed", rover.name)
        if stop.is_set():
            return
        log.info("%s: restarting in %.0f s", rover.name, delay)
        stop.wait(delay)


def main(argv: list[str] | None = None, *, stop: threading.Event | None = None,
         restart_delay: float = RESTART_DELAY_S) -> int:
    ap = argparse.ArgumentParser(prog="houston")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--log-file", type=Path, default=DEFAULT_LOG)
    args = ap.parse_args(argv)

    handler = _setup_logging(args.log_file)
    own_signals = stop is None
    stop = stop or threading.Event()
    try:
        settings = config.load(args.config)
        if not settings.rovers:
            log.error("no rovers in %s", args.config)
            return 2
        if own_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())

        log.info("houston starting: %s", ", ".join(r.name for r in settings.rovers))
        brain, voice = Brain(settings), Voice()
        transcriber = Transcriber(settings.whisper_url)
        threading.Thread(target=_preload, args=(voice, settings.rovers[0].voice),
                         name="houston-preload", daemon=True).start()

        def make_session(rover):
            return RoverSession(rover, settings, brain, voice, transcriber)

        sessions: dict[str, RoverSession] = {}
        threads = [threading.Thread(target=_supervise,
                                    args=(r, make_session, sessions, stop, restart_delay),
                                    name=f"houston-{r.name}", daemon=True)
                   for r in settings.rovers]
        for t in threads:
            t.start()
        stop.wait()
        log.info("houston stopping")
        for s in list(sessions.values()):
            s.stop()
        for t in threads:
            t.join(JOIN_S)
        return 0
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    sys.exit(main())
