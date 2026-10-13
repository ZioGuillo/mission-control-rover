from app.config import Settings


def test_session_secret_key_auto_generated_when_unset(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path)
    assert len(s.session_secret_key) >= 32


def test_session_secret_key_persisted_across_instances(tmp_path):
    """A restart must not invalidate every existing session cookie, so the
    generated secret has to be written to disk and reused on the next boot."""
    first = Settings(_env_file=None, data_dir=tmp_path)
    second = Settings(_env_file=None, data_dir=tmp_path)
    assert first.session_secret_key == second.session_secret_key


def test_session_secret_key_respects_explicit_value(tmp_path):
    explicit = "x" * 40
    s = Settings(_env_file=None, data_dir=tmp_path, session_secret_key=explicit)
    assert s.session_secret_key == explicit
    # explicit key must not be written into the auto-generated key file
    assert not (tmp_path / "session_secret_key").exists()


def test_session_secret_key_too_short_raises(tmp_path):
    import pytest
    with pytest.raises(Exception):
        Settings(_env_file=None, data_dir=tmp_path, session_secret_key="short")


def test_env_file_keys_meant_for_other_services_are_ignored(tmp_path):
    """.env is shared with the systemd services (WiFi portal, Cloudflare
    provisioning, Jetson fan). Filling in one of their settings must not
    stop the web app from starting."""
    env = tmp_path / ".env"
    env.write_text(
        "FAN_MODE=2\n"
        "PORTAL_SSID=MiRed\n"
        "PORTAL_PASSPHRASE=secreta123\n"
        "CF_API_TOKEN=abc\n"
        "MOTOR_SPEED_DEFAULT=0.5\n"
    )
    s = Settings(_env_file=env, data_dir=tmp_path)
    assert s.motor_speed_default == 0.5
