"""Camera frames: one JPEG from the rover app's MJPEG stream, logged in as the app user."""
from __future__ import annotations

import http.cookiejar
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_MAX_BYTES = 2 * 1024 * 1024
_CHUNK = 16 * 1024
_SOI, _EOI = b"\xff\xd8", b"\xff\xd9"


class CameraError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # surface 302s as HTTPError so we can inspect them
        return None


class AppCamera:
    def __init__(self, app_url: str, user: str, password_file: Path):
        self.app_url = app_url.rstrip("/")
        self.user = user
        self.password_file = Path(password_file)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect)

    def snapshot(self, timeout: float = 6.0) -> bytes:
        """One JPEG from /api/camera/stream. `timeout` bounds login + connect + first frame."""
        deadline = time.monotonic() + timeout
        jpeg = self._grab(deadline)
        if jpeg is None:  # not logged in or session expired: log in once, retry once
            self._login(deadline)
            jpeg = self._grab(deadline)
            if jpeg is None:
                raise CameraError("app keeps redirecting to /login after login")
        return jpeg

    def _open(self, req: urllib.request.Request, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CameraError("camera timed out")
        return self._opener.open(req, timeout=remaining)

    def _login(self, deadline: float) -> None:
        try:
            password = self.password_file.read_text().strip()
        except OSError as e:
            raise CameraError(f"password file: {e}") from e
        data = urllib.parse.urlencode({"username": self.user, "password": password}).encode()
        try:
            resp = self._open(urllib.request.Request(f"{self.app_url}/login", data=data), deadline)
        except urllib.error.HTTPError as e:
            resp = e
        except (urllib.error.URLError, OSError) as e:
            raise CameraError(f"app login: {e}") from e
        with resp:
            where = resp.headers.get("Location", "")
            if resp.code not in (302, 303) or urllib.parse.urlparse(where).path == "/login":
                raise CameraError("app login rejected (wrong user or password)")

    def _grab(self, deadline: float) -> bytes | None:
        """First JPEG, or None if the app bounced us to /login."""
        try:
            resp = self._open(urllib.request.Request(f"{self.app_url}/api/camera/stream"), deadline)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (302, 303, 307) and urllib.parse.urlparse(e.headers.get("Location", "")).path == "/login":
                return None
            raise CameraError(f"camera stream: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise CameraError(f"camera stream: {e}") from e
        buf = b""
        try:
            with resp:
                while len(buf) < _MAX_BYTES and time.monotonic() < deadline:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    buf += chunk
                    start = buf.find(_SOI)
                    if start >= 0:
                        end = buf.find(_EOI, start + 2)
                        if end >= 0:
                            return buf[start:end + 2]
        except OSError as e:
            raise CameraError(f"camera stream: {e}") from e
        raise CameraError("no complete JPEG in camera stream")
