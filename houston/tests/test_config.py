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
    assert s.rovers[0].name == "Bentley"
    assert s.conversation_timeout == 15.0
    assert s.rovers[0].voice == "em_alex"  # Kokoro voice name (no file)


def test_voice_name_stays_a_name_and_wav_becomes_a_path(tmp_path):
    f = tmp_path / "rovers.toml"
    f.write_text(TOML + TOML.split("\n", 3)[3].replace('"Fred"', '"Eva"').replace('"voices/x.wav"', '"em_alex"'))
    fred, eva = load(f).rovers
    assert fred.voice == (tmp_path / "voices/x.wav").resolve()
    assert eva.voice == "em_alex"


def test_personality_is_optional(tmp_path):
    f = tmp_path / "rovers.toml"
    f.write_text(TOML)
    assert load(f).rovers[0].personality == ""
    f.write_text(TOML.replace('voice = "voices/x.wav"', 'voice = "voices/x.wav"\npersonality = "Shiba"'))
    assert load(f).rovers[0].personality == "Shiba"


def test_filler_sound_is_a_path_next_to_the_toml(tmp_path):
    f = tmp_path / "rovers.toml"
    f.write_text(TOML)
    assert load(f).rovers[0].filler_sound is None
    f.write_text(TOML.replace('voice = "voices/x.wav"', 'voice = "voices/x.wav"\nfiller_sound = "audio/b.wav"'))
    assert load(f).rovers[0].filler_sound == (tmp_path / "audio/b.wav").resolve()


def test_reasoning_level_is_low_by_default_and_configurable(tmp_path):
    # Measured 2026-10-29: low = ~0.6 s to the first word vs ~3.9 s at medium, 18/18 numbers right
    # once the sheets spell numbers out in words.
    f = tmp_path / "rovers.toml"
    f.write_text(TOML)
    assert load(f).think == "low"
    f.write_text(TOML.replace('llm = "other:1b"', 'llm = "other:1b"\nthink = "medium"'))
    assert load(f).think == "medium"
