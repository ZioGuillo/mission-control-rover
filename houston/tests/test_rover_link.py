import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from houston.rover_link import LinkError, RoverLink, Speaker, parse_cards, pick_usb, stop_mic

FIX = Path(__file__).parent / "fixtures"
APLAY = (FIX / "jetson-aplay-l.txt").read_text()
ARECORD = (FIX / "jetson-arecord-l.txt").read_text()


def test_parse_cards_dedupes_and_keeps_description():
    cards = parse_cards(APLAY)
    assert [n for n, _ in cards] == ["tegrahda", "tegrasndt210ref", "UACDemoV10"]
    assert dict(cards)["UACDemoV10"] == "device 0: USB Audio [USB Audio]"


def test_pick_usb_on_real_listings():
    assert pick_usb(APLAY) == "UACDemoV10"
    assert pick_usb(ARECORD) == "Device"
    assert pick_usb("card 0: tegrahda [tegra-hda], device 3: HDMI 0 [HDMI 0]") is None


def test_ssh_args_has_control_master_options():
    args = RoverLink("ec2-user@rover").ssh_args()
    assert args[0] == "ssh" and args[-1] == "ec2-user@rover"
    joined = " ".join(args)
    for opt in ("ControlMaster=auto", "ControlPath=~/.ssh/houston-%C",
                "ControlPersist=10m", "ServerAliveInterval=5", "ConnectTimeout=5"):
        assert f"-o {opt}" in joined


def test_detect_returns_mic_and_speaker_and_caches():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = ARECORD if cmd[-1] == "arecord -l" else APLAY
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    link = RoverLink("r", run=run)
    assert link.detect() == ("Device", "UACDemoV10")
    assert link.detect() == ("Device", "UACDemoV10")
    assert len(calls) == 2


def test_detect_raises_without_usb_or_on_ssh_failure():
    link = RoverLink("r", run=lambda c, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    with pytest.raises(LinkError):
        link.detect()
    link = RoverLink("r", run=lambda c, **kw: SimpleNamespace(returncode=255, stdout="", stderr="down"))
    with pytest.raises(LinkError):
        link.detect()


def test_mic_command():
    started = []
    link = RoverLink("r", popen=lambda cmd, **kw: started.append((cmd, kw)) or "proc",
                     run=lambda c, **kw: SimpleNamespace(
                         returncode=0, stdout=ARECORD if c[-1] == "arecord -l" else APLAY, stderr=""))
    assert link.mic() == "proc"
    cmd, kw = started[0]
    assert cmd[-1] == "exec arecord -q -D plughw:CARD=Device,DEV=0 -f S16_LE -r 16000 -c 1 -t raw"
    assert kw["stdout"] == subprocess.PIPE


class FakeStdin:
    def __init__(self, fail=False):
        self.data, self.closed, self.fail = b"", False, fail

    def write(self, b):
        if self.fail:
            raise BrokenPipeError
        self.data += b

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, exit_code=None, err=b"", fail=False, exit_after=None):
        self.exit_code = exit_code if exit_after is not None else None
        self.returncode = exit_code if exit_after is None else None
        self.stdin = FakeStdin(fail)
        self.stderr = SimpleNamespace(read=lambda: err, close=lambda: None)
        self.waited = None
        self.exit_after = exit_after  # polls before a still-running proc exits
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.exit_after is not None and self.polls > self.exit_after:
            self.returncode = self.exit_code
        return self.returncode

    def wait(self, timeout=None):
        self.waited = timeout
        if self.exit_after is not None and self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def make_speaker(procs, sleeps):
    it = iter(procs)
    return Speaker(lambda: next(it), sleep=sleeps.append)


def test_speaker_retries_when_busy_then_writes():
    busy = FakeProc(1, b"aplay: main:831: audio open error: Device or resource busy\n")
    good = FakeProc()
    sleeps = []
    spk = make_speaker([busy, busy, good], sleeps)
    spk.write(b"abc")
    spk.close()
    assert good.stdin.data == b"abc" and good.stdin.closed and good.waited is not None
    assert sleeps.count(0.5) == 2


def test_speaker_gives_up_after_five_busy_starts():
    busy = lambda: FakeProc(1, b"Device or resource busy")
    sleeps = []
    with pytest.raises(LinkError, match="busy"):
        Speaker(busy, sleep=sleeps.append)
    assert sleeps.count(0.5) == 4


def test_speaker_other_early_exit_is_link_error_without_retry():
    n = []
    def start():
        n.append(1)
        return FakeProc(255, b"ssh: connect to host: No route to host")
    with pytest.raises(LinkError, match="No route"):
        Speaker(start, sleep=lambda s: None)
    assert len(n) == 1


def test_speaker_broken_pipe_is_link_error():
    spk = make_speaker([FakeProc(fail=True)], [])
    with pytest.raises(LinkError):
        spk.write(b"x")


def test_speaker_close_kills_on_timeout():
    p = FakeProc()
    def wait(timeout=None):
        raise subprocess.TimeoutExpired("ssh", timeout)
    p.wait = wait
    spk = make_speaker([p], [])
    spk.close()
    assert p.killed


def test_busy_exit_detected_by_polling_at_0_15s():
    slow_busy = FakeProc(1, b"Device or resource busy", exit_after=3)
    good = FakeProc()
    sleeps = []
    spk = make_speaker([slow_busy, good], sleeps)
    assert sleeps[:3] == [0.05, 0.05, 0.05] and sleeps[3] == 0.5
    spk.write(b"z")
    assert good.stdin.data == b"z"


def test_close_raises_if_aplay_exited_nonzero():
    p = FakeProc(exit_code=1, err=b"underrun", exit_after=100)
    spk = make_speaker([p], [])
    with pytest.raises(LinkError, match="underrun"):
        spk.close()


def test_popen_oserror_is_link_error():
    def boom(*a, **k):
        raise FileNotFoundError("ssh")
    with pytest.raises(LinkError):
        Speaker(boom, sleep=lambda s: None)
    link = RoverLink("r", popen=boom, run=_listing_run)
    with pytest.raises(LinkError):
        link.mic()


def _listing_run(c, **kw):
    out = ARECORD if c[-1] == "arecord -l" else APLAY
    return SimpleNamespace(returncode=0, stdout=out, stderr="")


def _link_with(master_rcs):
    cmds, started = [], []

    def run(c, **kw):
        if "-O" in c or c[-1] == "true":
            cmds.append(c)
            return SimpleNamespace(returncode=master_rcs.pop(0), stdout="", stderr="")
        return _listing_run(c, **kw)

    def popen(c, **kw):
        started.append(c)
        return FakeProc()

    return RoverLink("r", run=run, popen=popen, sleep=lambda s: None), cmds, started


def test_speaker_master_check_ok_skips_handshake():
    link, cmds, started = _link_with([0])
    link.speaker()
    assert len(cmds) == 1 and cmds[0][cmds[0].index("-O") + 1] == "check" and len(started) == 1


def test_speaker_master_check_fails_then_true_succeeds():
    link, cmds, started = _link_with([255, 0])
    link.speaker()
    assert cmds[1][-1] == "true" and len(started) == 1


def test_speaker_unreachable_never_starts_aplay():
    link, cmds, started = _link_with([255, 255])
    with pytest.raises(LinkError, match="unreachable"):
        link.speaker()
    assert started == []


def test_stop_mic_kills_and_reaps():
    p = FakeProc()
    p.stdout = SimpleNamespace(close=lambda: None)
    stop_mic(p)
    assert p.killed and p.waited is not None
