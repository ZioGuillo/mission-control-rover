"""App side of the TensorRT worker (app/hardware/trt_worker.py).

The worker runs under JetPack's system Python, the only one with TensorRT
bindings; this talks to it over a pipe. See trt_worker.py for the protocol.
"""
import json
import logging
import subprocess
import threading
from pathlib import Path

import numpy as np

from app.hardware.trt_worker import HEADER

_log = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("trt_worker.py")
SYSTEM_PYTHON = "/usr/bin/python3"


class TrtError(RuntimeError):
    pass


class TrtWorker:
    def __init__(self, engine_paths: list[Path], python: str = SYSTEM_PYTHON,
                 worker: Path = WORKER, timeout: float = 120.0):
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            [python, str(worker), *map(str, engine_paths)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        # Engine loading takes a few seconds; read the handshake off a
        # thread so a hung worker can't block app startup forever.
        line: list[bytes] = []
        t = threading.Thread(target=lambda: line.append(self._proc.stdout.readline()), daemon=True)
        t.start()
        t.join(timeout)
        if not line or not line[0]:
            self.close()
            raise TrtError("TensorRT worker didn't start")
        hello = json.loads(line[0])
        if not hello.get("ok"):
            self.close()
            raise TrtError(hello.get("error", "TensorRT worker failed"))
        self.engines = hello["engines"]

    def input_size(self, index: int) -> int:
        return self.engines[index]["input"][2]  # NCHW [1, 3, S, S]

    def infer(self, index: int, image: np.ndarray) -> np.ndarray:
        """image: letterboxed RGB uint8 (S, S, 3). Returns the raw output."""
        payload = np.ascontiguousarray(image, dtype=np.uint8).tobytes()
        with self._lock:
            try:
                self._proc.stdin.write(HEADER.pack(index, len(payload)) + payload)
                self._proc.stdin.flush()
                status, n = HEADER.unpack(self._read(HEADER.size))
                body = self._read(n)
            except (BrokenPipeError, OSError, EOFError) as exc:
                raise TrtError(f"TensorRT worker died: {exc}") from exc
        if status != 0:
            raise TrtError(body.decode(errors="replace"))
        return np.frombuffer(body, dtype=np.float32).reshape(self.engines[index]["output"])

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                raise EOFError("worker closed its output")
            buf += chunk
        return buf

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
