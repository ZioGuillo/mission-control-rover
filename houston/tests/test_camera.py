import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from houston.camera import AppCamera, CameraError

JPEG1 = b"\xff\xd8" + b"first-frame" + b"\xff\xd9"
JPEG2 = b"\xff\xd8" + b"second-frame" + b"\xff\xd9"


class _Server(HTTPServer):
    def server_bind(self):  # HTTPServer's getfqdn() reverse lookup stalls ~35 s here
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class FakeApp:
    """Fake RoboControl: cookie-gated MJPEG stream, form login that 302s like the real app."""

    def __init__(self, password="s3cret", stream_body=None):
        self.password = password
        self.sessions = set()
        self.logins = 0
        self.stream_hits = 0
        self.stream_body = stream_body
        app = self

        class H(BaseHTTPRequestHandler):
            def _session(self):
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "session":
                        return v
                return None

            def _redirect(self, where, cookie=None):
                self.send_response(302)
                self.send_header("Location", where)
                if cookie:
                    self.send_header("Set-Cookie", f"session={cookie}; Path=/")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                app.logins += 1
                ok = (self.path == "/login" and form.get("username") == ["rover"]
                      and form.get("password") == [app.password])
                if ok:
                    token = f"tok{app.logins}"
                    app.sessions.add(token)
                    self._redirect("/", cookie=token)
                else:
                    self._redirect("/login?status=invalid")

            def do_GET(self):
                if self.path != "/api/camera/stream":
                    self.send_error(404)
                    return
                app.stream_hits += 1
                if self._session() not in app.sessions:
                    self._redirect("/login")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                body = app.stream_body or b"".join(
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + j + b"\r\n" for j in (JPEG1, JPEG2))
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = _Server(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake(tmp_path):
    apps = []

    def make(**kw):
        a = FakeApp(**kw)
        apps.append(a)
        return a

    yield make
    for a in apps:
        a.close()


@pytest.fixture
def pwfile(tmp_path):
    p = tmp_path / "pw"
    p.write_text("s3cret\n")
    return p


def test_logs_in_then_returns_first_jpeg(fake, pwfile):
    app = fake()
    jpeg = AppCamera(app.url, "rover", pwfile).snapshot()
    assert jpeg == JPEG1
    assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
    assert app.logins == 1


def test_reuses_session_without_logging_in_again(fake, pwfile):
    app = fake()
    cam = AppCamera(app.url, "rover", pwfile)
    cam.snapshot()
    cam.snapshot()
    assert app.logins == 1


def test_expired_session_relogs_in_once_and_succeeds(fake, pwfile):
    app = fake()
    cam = AppCamera(app.url, "rover", pwfile)
    cam.snapshot()
    app.sessions.clear()  # app restarted / cookie invalidated
    assert cam.snapshot() == JPEG1
    assert app.logins == 2


def test_wrong_password_raises_without_looping(fake, tmp_path):
    app = fake()
    bad = tmp_path / "bad"
    bad.write_text("nope\n")
    with pytest.raises(CameraError):
        AppCamera(app.url, "rover", bad).snapshot()
    assert app.logins == 1


def test_second_bounce_after_relogin_raises(fake, pwfile):
    app = fake()
    app.sessions = _NeverValid()
    with pytest.raises(CameraError):
        AppCamera(app.url, "rover", pwfile).snapshot()
    assert app.logins == 1


class _NeverValid(set):
    def __contains__(self, item):
        return False


def test_password_read_at_login_time(fake, tmp_path):
    app = fake()
    p = tmp_path / "pw"
    cam = AppCamera(app.url, "rover", p)  # file does not exist yet
    p.write_text("s3cret\n")
    assert cam.snapshot() == JPEG1


def test_server_down_raises_within_timeout(pwfile):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t = time.monotonic()
    with pytest.raises(CameraError):
        AppCamera(f"http://127.0.0.1:{port}", "rover", pwfile).snapshot(timeout=2)
    assert time.monotonic() - t < 2.5


def test_unresponsive_app_bounded_by_timeout(pwfile):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)  # accepts connections, never answers
    try:
        t = time.monotonic()
        with pytest.raises(CameraError):
            AppCamera(f"http://127.0.0.1:{srv.getsockname()[1]}", "rover", pwfile).snapshot(timeout=1)
        assert time.monotonic() - t < 2.0
    finally:
        srv.close()


def test_stream_without_complete_jpeg_raises_at_cap(fake, pwfile):
    app = fake(stream_body=b"--frame\r\n\r\n\xff\xd8" + b"x" * (3 * 1024 * 1024))
    with pytest.raises(CameraError):
        AppCamera(app.url, "rover", pwfile).snapshot()


def test_frame_split_across_chunks(fake, pwfile):
    big = b"\xff\xd8" + b"y" * 300_000 + b"\xff\xd9"
    app = fake(stream_body=b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + big + b"\r\n--frame\r\n")
    assert AppCamera(app.url, "rover", pwfile).snapshot() == big
