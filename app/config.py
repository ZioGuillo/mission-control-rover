import secrets
from pathlib import Path
from typing import Annotated
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GpioPin = Annotated[int, Field(ge=0, le=27)]


def _load_or_create_secret(data_dir: Path) -> str:
    """
    Persisted, per-install random secret used when SESSION_SECRET_KEY is not set
    in the environment. Avoids shipping a fixed fallback value that would let
    anyone who has read the (public) source code forge session cookies.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "session_secret_key"
    if key_file.exists():
        existing = key_file.read_text().strip()
        if len(existing) >= 32:
            return existing
    key = secrets.token_hex(32)
    key_file.write_text(key)
    key_file.chmod(0o600)
    return key


class Settings(BaseSettings):
    port: int = 8000
    base_url: str = ""
    camera_stream_url: str = ""
    # Camera without an IR-cut filter (NoIR / night-vision, e.g. Kuman SC15):
    # use the sensor's "_noir" colour tuning so daylight isn't pink.
    camera_noir: bool = False
    # CSI camera frame rate. Each frame is JPEG-encoded on the CPU, so keep it
    # low on a Pi Zero (10 fps ≈ a third of the CPU); raise it on faster boards.
    camera_fps: Annotated[int, Field(ge=1, le=60)] = 10
    motor_speed_default: Annotated[float, Field(ge=0.0, le=1.0)] = 0.75
    pan_servo_pin: GpioPin = 12
    tilt_servo_pin: GpioPin = 13
    buzzer_pin: GpioPin = 18
    obstacle_threshold_cm: Annotated[float, Field(gt=0.0)] = 20.0
    api_username: str = ""
    api_password: str = ""
    motor_rate_limit: Annotated[int, Field(ge=0)] = 20
    session_secret_key: str = ""
    data_dir: Path = Path.home() / ".local" / "share" / "robocar"
    # Run without any real GPIO/camera/rrb3 hardware — app/hardware/* drivers
    # simulate plausible values instead (see docker/README "Local testing
    # without hardware"). Off by default so a real Pi/Jetson never
    # accidentally ignores its actual hardware.
    simulate: bool = False
    # Which rover this is (login page "Unit 01"). Normally looked up from the
    # board's serial number in units.txt; set it here only to override.
    unit_number: Annotated[int, Field(ge=0, le=99)] = 0

    # .env is shared with the systemd services (WiFi portal, Cloudflare
    # provisioning, Jetson fan): ignore their keys instead of refusing to start.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("camera_stream_url")
    @classmethod
    def must_be_http(cls, v: str) -> str:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("camera_stream_url must start with http:// or https://")
        return v

    @model_validator(mode="after")
    def _ensure_session_secret_key(self) -> "Settings":
        if not self.session_secret_key:
            self.session_secret_key = _load_or_create_secret(self.data_dir)
        if len(self.session_secret_key) < 32:
            raise ValueError("session_secret_key must be at least 32 characters")
        return self


settings = Settings()
