import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

import pytest

from houston import nasa
from houston.tests import fakehttp


@pytest.fixture
def serve():
    servers = []

    def make(routes):
        """routes: path -> (status, body bytes or JSON-able)."""
        seen = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                seen.append((url.path, parse_qs(url.query)))
                status, body = routes.get(url.path, (404, b"no such path"))
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv, url = fakehttp.start(H)
        servers.append(srv)
        return url, seen

    yield make
    for s in servers:
        fakehttp.stop(s)


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_perseverance_status_from_raw_image_feed(serve):
    url, seen = serve({"/rss/api/": (200, {
        "total_images": 1054650, "total_results": 578789,
        "images": [{"sol": 2022, "date_taken_utc": _ago(1)[:-1]}]})})
    assert nasa.rover_status("Perseverance", base=url) == {
        "sol": 2022, "total_photos": 1054650, "status": "activo"}
    path, q = seen[0]
    assert q["category"] == ["mars2020"] and q["order"] == ["sol desc"] and q["num"] == ["1"]


def test_curiosity_status_from_raw_image_items(serve):
    url, seen = serve({"/api/v1/raw_image_items/": (200, {
        "total": 1514349, "items": [{"sol": 5057, "date_taken": _ago(2)}]})})
    assert nasa.rover_status("curiosity", base=url) == {
        "sol": 5057, "total_photos": 1514349, "status": "activo"}
    assert seen[0][1]["condition_1"] == ["msl:mission"]


def test_rover_without_recent_photos_is_not_called_active(serve):
    url, _ = serve({"/api/v1/raw_image_items/": (200, {
        "total": 10, "items": [{"sol": 3, "date_taken": _ago(90)}]})})
    assert nasa.rover_status("curiosity", base=url)["status"] == "sin fotos en los últimos 30 días"


@pytest.mark.parametrize("routes", [
    {"/rss/api/": (500, b"boom")},
    {"/rss/api/": (200, b"<html>not json</html>")},
    {"/rss/api/": (200, {"images": []})},
])
def test_bad_feed_raises(serve, routes):
    url, _ = serve(routes)
    with pytest.raises(nasa.NasaError):
        nasa.rover_status("perseverance", base=url)


def test_unknown_rover_raises():
    with pytest.raises(nasa.NasaError):
        nasa.rover_status("opportunity")


def test_apod_sends_key_and_trims(serve):
    url, seen = serve({"/planetary/apod": (200, {
        "date": "2026-10-28", "title": "Jezero", "media_type": "image",
        "explanation": "x" * 2000, "url": "https://apod.nasa.gov/a.jpg"})})
    out = nasa.apod("abc123", base=url)
    assert seen[0] == ("/planetary/apod", {"api_key": ["abc123"]})
    assert out["title"] == "Jezero" and out["date"] == "2026-10-28" and out["media_type"] == "image"
    assert len(out["explanation"]) <= 600


def test_apod_error_raises(serve):
    url, _ = serve({"/planetary/apod": (429, {"error": {"code": "OVER_RATE_LIMIT"}})})
    with pytest.raises(nasa.NasaError):
        nasa.apod("DEMO_KEY", base=url)


def test_key_from_file_or_demo(tmp_path):
    f = tmp_path / "nasa_key"
    assert nasa.key(f) == "DEMO_KEY"
    f.write_text("  real-key\n")
    assert nasa.key(f) == "real-key"
    f.write_text("\n")
    assert nasa.key(f) == "DEMO_KEY"
