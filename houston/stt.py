"""Speech to text: POST a WAV to a local whisper-server `/inference`."""
from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
import uuid
import wave

PROMPT = "Hey Fred, hola Eva, ¿qué hay de nuevo en Marte?"
_TIMEOUT_S = 15.0
_NOISE = re.compile(r"^(\[[^\]]*\]|\([^)]*\)|[\W_])*$")  # [MÚSICA], (ruido), punctuation only


class SttError(Exception):
    pass


def _wav(pcm16k: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm16k)
    return buf.getvalue()


def _multipart(fields: dict[str, str], wav: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n".encode() + wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Transcriber:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def transcribe(self, pcm16k: bytes) -> str:
        """16 kHz mono S16_LE PCM -> Spanish text; "" when whisper heard only noise."""
        body, ctype = _multipart(
            {"language": "es", "prompt": PROMPT, "response_format": "json", "temperature": "0.0"},
            _wav(pcm16k))
        req = urllib.request.Request(f"{self.url}/inference", data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                text = json.load(resp)["text"]
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
            raise SttError(f"whisper-server: {e}") from e
        text = str(text).strip()
        return "" if _NOISE.match(text) else text
