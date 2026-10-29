from pathlib import Path

from houston.config import load

TOML = """
[settings]
llm = "other:1b"

[[rover]]
name = "Fred"
unit = 2
ssh = "u@h"
app_url = "http://h:8000"
app_user = "fred"
password_file = "~/.config/fred/pw"
voice = "voices/x.wav"
"""


def test_load(tmp_path):
    f = tmp_path / "rovers.toml"
    f.write_text(TOML)
    s = load(f)
    assert len(s.rovers) == 1
    r = s.rovers[0]
    assert r.name == "Fred" and r.unit == 2
    assert r.password_file == Path.home() / ".config/fred/pw"
    assert r.voice == (tmp_path / "voices/x.wav").resolve()
    assert s.llm == "other:1b"
    assert s.whisper_url == "http://127.0.0.1:8178"
    assert not hasattr(s, "whisper_model")  # install.sh names the model (whisper-server loads it)
    assert s.nasa_key_file == Path.home() / ".config/houston/nasa_key"


def test_camera_url_without_tunnel_is_the_app(tmp_path):
    f = tmp_path / "rovers.toml"
    f.write_text(TOML)
    assert load(f).rovers[0].camera_url == "http://h:8000"


def test_camera_url_goes_through_the_tunnel(tmp_path):
    f = tmp_path / "rovers.toml"
    f.write_text(TOML.replace('voice = "voices/x.wav"', 'voice = "voices/x.wav"\ntunnel_port = 18002'))
    r = load(f).rovers[0]
    assert r.tunnel_port == 18002
    assert r.camera_url == "http://127.0.0.1:18002"


def test_shipped_config_loads():
    s = load(Path(__file__).parents[1] / "rovers.toml")
    assert s.rovers[0].name == "Fred"
    assert s.rovers[0].voice.exists()
