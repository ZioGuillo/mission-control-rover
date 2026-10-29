"""install.sh argument handling and plist rendering, sandboxed: fake HOME, and a PATH whose only
launchctl is a stub that records its calls (no brew/ollama/uv, so a real install can never start)."""
import plistlib
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"


@pytest.fixture
def sandbox(tmp_path):
    home, stub = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    stub.mkdir()
    calls = tmp_path / "launchctl.calls"
    (stub / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    (stub / "launchctl").chmod(0o755)

    def run(*args):
        env = {"HOME": str(home), "PATH": f"{stub}:/usr/bin:/bin"}
        return subprocess.run(["/bin/bash", str(INSTALL), *args], env=env, capture_output=True,
                              text=True, timeout=30)

    run.home, run.calls = home, calls
    return run


@pytest.mark.parametrize("args", [
    ["--bogus"], ["--purge"], ["--uninstall", "--bogus"], ["--uninstall", "--purge", "extra"],
    ["--purge", "--uninstall"], ["--render-only"], ["--render-only", "/nonexistent-dir"],
    ["--render-only", "DIR", "extra"]])
def test_bad_arguments_print_usage_and_exit_2(sandbox, tmp_path, args):
    args = [str(tmp_path) if a == "DIR" else a for a in args]
    res = sandbox(*args)
    assert res.returncode == 2, res.stderr
    assert "usage" in res.stderr.lower()
    assert not sandbox.calls.exists()  # nothing was booted out or loaded


def test_uninstall_purge_still_works(sandbox):
    runtime = sandbox.home / "Library/Application Support/houston/runtime"
    runtime.mkdir(parents=True)
    res = sandbox("--uninstall", "--purge")
    assert res.returncode == 0, res.stderr
    assert not runtime.exists()
    assert "bootout" in sandbox.calls.read_text()


def test_render_only_fills_every_placeholder(sandbox, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    res = sandbox("--render-only", str(out))
    assert res.returncode == 0, res.stderr
    for name in ("local.missioncontrol.houston", "local.missioncontrol.whisper"):
        path = out / f"{name}.plist"
        assert "@" not in path.read_text()
        assert subprocess.run(["/usr/bin/plutil", "-lint", str(path)], capture_output=True).returncode == 0
    houston = plistlib.loads((out / "local.missioncontrol.houston.plist").read_bytes())
    env = houston["EnvironmentVariables"]
    assert env["TQDM_DISABLE"] == "1" and env["HF_HUB_OFFLINE"] == "1"
    whisper = plistlib.loads((out / "local.missioncontrol.whisper.plist").read_bytes())
    args = whisper["ProgramArguments"]
    assert args[args.index("-m") + 1] == str(sandbox.home / "Library/Caches/houston/ggml-large-v3-turbo.bin")
