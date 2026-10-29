"""Load houston/rovers.toml into frozen dataclasses."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_NASA_KEY_FILE = Path("~/.config/houston/nasa_key")


@dataclass(frozen=True)
class Rover:
    name: str
    unit: int
    ssh: str
    app_url: str
    app_user: str
    password_file: Path
    voice: Path | str  # a .wav sample (Chatterbox clones it) or a Kokoro voice name ("em_alex")
    # macOS Local Network privacy blocks launchd-started Python from the LAN, but not /usr/bin/ssh.
    # With a tunnel_port, install.sh runs an ssh master per rover forwarding this localhost port
    # to the rover app, and the camera goes through it.
    tunnel_port: int | None = None
    # Extra character for this rover, added to its prompts (e.g. Bentley = Pablo's Shiba Inu).
    personality: str = ""

    @property
    def camera_url(self) -> str:
        """Where the camera fetches the app: the localhost tunnel when there is one."""
        if self.tunnel_port is None:
            return self.app_url
        path = urlsplit(self.app_url).path
        return f"http://127.0.0.1:{self.tunnel_port}{path}"


@dataclass(frozen=True)
class Settings:
    rovers: list[Rover]
    ollama_url: str = "http://127.0.0.1:11434"
    whisper_url: str = "http://127.0.0.1:8178"
    llm: str = "gpt-oss:120b"
    vlm: str = "qwen3.8:27b"
    conversation_timeout: float = 60.0
    nasa_key_file: Path = field(default_factory=lambda: DEFAULT_NASA_KEY_FILE.expanduser())


def _resolve(value: str | Path, base: Path) -> Path:
    """Expand ~, and anchor relative paths to the TOML's folder, not the cwd."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load(path: Path) -> Settings:
    path = Path(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    rovers = []
    for raw in data.get("rover", []):
        raw = dict(raw)
        raw["password_file"] = _resolve(raw["password_file"], base)
        if str(raw["voice"]).endswith(".wav"):
            raw["voice"] = _resolve(raw["voice"], base)
        rovers.append(Rover(**raw))

    opts = dict(data.get("settings", {}))
    allowed = {f.name for f in fields(Settings)} - {"rovers"}
    unknown = set(opts) - allowed
    if unknown:
        raise ValueError(f"unknown settings in {path}: {sorted(unknown)}")
    if "nasa_key_file" in opts:
        opts["nasa_key_file"] = _resolve(opts["nasa_key_file"], base)
    return Settings(rovers=rovers, **opts)
