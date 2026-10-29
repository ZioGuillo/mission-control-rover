import io
import json
import threading
import socketserver
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from houston.stt import SttError, Transcriber


class _Server(HTTPServer):
    def server_bind(self):  # HTTPServer's getfqdn() reverse lookup stalls ~35 s here
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def _serve(reply: bytes, status: int = 200):
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["ctype"] = self.headers["Content-Type"]
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *a):
            pass

    srv = _Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


@pytest.fixture
def serve():
    servers = []

    def make(reply, status=200):
        srv, seen = _serve(json.dumps(reply).encode() if not isinstance(reply, bytes) else reply, status)
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}", seen

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


PCM = b"\x01\x00" * 1600


def test_transcribe_posts_16k_wav_and_fields(serve):
    url, seen = serve({"text": " Hola Fred. "})
    assert Transcriber(url).transcribe(PCM) == "Hola Fred."
    assert seen["path"] == "/inference"
    assert seen["ctype"].startswith("multipart/form-data; boundary=")
    body = seen["body"]
    for field in (b'name="language"\r\n\r\nes', b'name="response_format"\r\n\r\njson',
                  b'name="temperature"\r\n\r\n0.0', b'name="prompt"'):
        assert field in body
    assert "¿qué hay de nuevo en Marte?".encode() in body
    start = body.index(b"RIFF")
    with wave.open(io.BytesIO(body[start:start + 44 + len(PCM)])) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.readframes(w.getnframes()) == PCM


@pytest.mark.parametrize("text", ["[MÚSICA]", " (ruido) ", "...", " . ", ""])
def test_noise_markers_become_empty(serve, text):
    url, _ = serve({"text": text})
    assert Transcriber(url).transcribe(PCM) == ""


def test_http_error_raises(serve):
    url, _ = serve(b"boom", status=500)
    with pytest.raises(SttError):
        Transcriber(url).transcribe(PCM)


def test_connection_refused_raises():
    with pytest.raises(SttError):
        Transcriber("http://127.0.0.1:1").transcribe(PCM)


def test_bad_json_raises(serve):
    url, _ = serve(b"not json")
    with pytest.raises(SttError):
        Transcriber(url).transcribe(PCM)


def test_prompt_names_the_configured_rovers(serve):
    # Live 2026-10-29: the hint still said "Hey Fred" after the rover became Bentley, and Whisper
    # wrote "Hey Bentley" as "¿Qué tal vez le…".
    url, seen = serve({"text": "Hey Bentley."})
    Transcriber(url, names=["Bentley", "Eva"]).transcribe(PCM)
    assert "Hey Bentley".encode() in seen["body"] and b"Eva" in seen["body"]
    assert b"Fred" not in seen["body"]
