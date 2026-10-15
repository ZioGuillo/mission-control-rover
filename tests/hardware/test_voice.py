"""Mission-control voice: what it says, when it can play, how it plays."""
import wave

import pytest

from app.hardware import voice


def test_all_nominal_call_names_the_unit():
    assert voice.sequence([], unit=2) == ["quindar_in", "callsign_unit_02", "nominal", "quindar_out"]


def test_offline_systems_are_named_between_the_beeps():
    assert voice.sequence(["camera", "motors"], unit=1) == [
        "quindar_in", "callsign_unit_01", "check", "camera_offline", "motors_offline",
        "caution", "quindar_out"]


@pytest.mark.parametrize("unit", [None, 0, 99])
def test_board_without_a_rendered_unit_gets_the_plain_callsign(unit):
    # no number in units.txt, or one past the rendered clips (MAX_UNIT)
    assert voice.callsign(unit) == "callsign"


def test_radio_check_uses_the_callsign():
    assert voice.radio_check(3) == ["quindar_in", "callsign_unit_03", "test", "quindar_out"]


@pytest.mark.parametrize("offline", [[], ["camera"], ["vision"], ["motors"], list(voice.COMPONENTS)])
@pytest.mark.parametrize("unit", [None, 1, 2, 20])
def test_every_clip_the_code_can_ask_for_exists(offline, unit):
    for clip in voice.sequence(offline, unit) + voice.radio_check(unit):
        path = voice.AUDIO_DIR / f"{clip}.wav"
        assert path.exists(), path
        with wave.open(str(path)) as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getnframes() > 0


def test_radio_check_clip_exists():
    assert (voice.AUDIO_DIR / "test.wav").exists()


def test_no_aplay_means_no_sound(monkeypatch):
    monkeypatch.setattr(voice.shutil, "which", lambda _c: None)
    assert "aplay" in voice.playback_problem()


def test_no_sound_card(monkeypatch, tmp_path):
    cards = tmp_path / "cards"
    cards.write_text("--- no soundcards ---\n")
    monkeypatch.setattr(voice.shutil, "which", lambda _c: "/usr/bin/aplay")
    real_path = voice.Path
    monkeypatch.setattr(voice, "Path", lambda p: cards if p == "/proc/asound/cards" else real_path(p))
    assert "USB speaker" in voice.playback_problem()


def test_play_runs_aplay_with_the_files_in_order(monkeypatch):
    calls = []

    class Done:
        returncode, stderr = 0, ""

    monkeypatch.setattr(voice, "playback_problem", lambda: None)
    monkeypatch.setattr(voice.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    monkeypatch.setattr(voice.settings, "voice_device", "plughw:1,0")
    assert voice.play(["quindar_in", "callsign", "nominal", "quindar_out"], wait=True) is True
    [cmd] = calls
    assert cmd[:4] == ["aplay", "-q", "-D", "plughw:1,0"]
    assert [p.rsplit("/", 1)[-1] for p in cmd[4:]] == [
        "quindar_in.wav", "callsign.wav", "nominal.wav", "quindar_out.wav"]
    assert voice.last_result == {"ok": True, "message": "played"}


def test_play_reports_the_problem_instead_of_failing(monkeypatch):
    monkeypatch.setattr(voice, "playback_problem", lambda: "no sound card found")
    assert voice.play(["test"], wait=True) is False
    assert voice.last_result["message"] == "no sound card found"


def test_systems_check(monkeypatch):
    from app.hardware import camera_driver, ml_driver, rrb3_driver

    monkeypatch.setattr(camera_driver, "available", True)
    monkeypatch.setattr(camera_driver, "_frame_counter", 10)
    monkeypatch.setattr(ml_driver, "available", True)
    monkeypatch.setattr(ml_driver, "backend", "tensorrt")
    ml_driver.backend_settled.set()
    monkeypatch.setattr(rrb3_driver, "available", False)
    assert voice.systems_check(timeout=1) == ["motors"]


def test_systems_check_gives_up_on_a_camera_with_no_frames(monkeypatch):
    from app.hardware import camera_driver, ml_driver, rrb3_driver

    monkeypatch.setattr(camera_driver, "available", True)
    monkeypatch.setattr(camera_driver, "_frame_counter", 0)
    monkeypatch.setattr(ml_driver, "available", False)
    monkeypatch.setattr(rrb3_driver, "available", True)
    assert voice.systems_check(timeout=0.05, poll=0.01) == ["camera", "vision"]


def test_voice_disabled_never_announces(monkeypatch):
    monkeypatch.setattr(voice.settings, "voice_enabled", False)
    monkeypatch.setattr(voice, "systems_check", lambda **kw: pytest.fail("checked anyway"))
    voice.announce_startup()


def test_radio_check_route_reports_why_it_is_silent(client, monkeypatch):
    monkeypatch.setattr(voice, "playback_problem", lambda: "no sound card found")
    assert client.post("/settings/voice-test").json() == {"ok": False, "message": "no sound card found"}
