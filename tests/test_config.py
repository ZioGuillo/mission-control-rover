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
