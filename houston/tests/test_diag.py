import time

from houston import diag


def test_inactive_without_window(tmp_path):
    d = diag.Diag(tmp_path / "until", tmp_path / "diag.log")
    d.record("Bentley", 1.0, "Hey Benli", "other")
    assert not (tmp_path / "diag.log").exists()


def test_records_short_utterances_only_while_window_open(tmp_path):
    until = tmp_path / "until"
    until.write_text(str(time.time() + 300))
    d = diag.Diag(until, tmp_path / "diag.log")
    d.record("Bentley", 1.1, "Ey, Benli.", "other")
    d.record("Bentley", 6.0, "una conversación larga de la sala", "other")  # long: never written
    log = (tmp_path / "diag.log").read_text()
    assert "Ey, Benli." in log and "1.1s" in log and "other" in log
    assert "conversación" not in log


def test_window_expires(tmp_path):
    until = tmp_path / "until"
    until.write_text(str(time.time() - 1))
    d = diag.Diag(until, tmp_path / "diag.log")
    d.record("Bentley", 1.0, "Hey", "other")
    assert not (tmp_path / "diag.log").exists()
