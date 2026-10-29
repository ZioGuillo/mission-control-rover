"""The brain: gpt-oss streams the answer sentence by sentence; qwen3.8 looks through the camera.

Camera questions caught by LOOK go straight to the vision model (no gpt-oss hop). Any failure
(camera, Ollama, too many tool rounds, nothing to say) ends the answer with FAILURE, so the rover
never describes a scene it didn't photograph.
"""
from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading
import time
import urllib.request
from collections.abc import Iterator

from houston import nasa, prompts
from houston.config import Settings
from houston.sentences import Splitter

log = logging.getLogger(__name__)

HISTORY_TURNS = 8
MAX_TOOL_ROUNDS = 3
_TIMEOUT_S = 120.0  # per socket read: a cold 120b load can take a while to send its first byte
_SNAPSHOT_TIMEOUT_S = 6.0
_WARM_TIMEOUT_S = 600.0  # loading gpt-oss:120b from disk
_LLM_KEEP_ALIVE = -1     # gpt-oss stays loaded for as long as Ollama runs

# Only deictic "look at this" forms: "alrededor del Sol", "¿qué cámara tiene...?" are talk questions.
# "¿Qué ves?" only when it ends the question (maybe plus a name) or points here and now;
# "¿Qué ves en el cielo de Marte?" is left to gpt-oss (its `mirar` tool decides).
LOOK = re.compile(
    r"\b(qu[eé] (ves|est[aá]s viendo)"
    r"(?=\s*(,\s*\w+)?[\s?!.]*$|\s+(ahora|ah[ií]|aqu[ií]|frente|delante|alrededor de ti))|"
    r"(puedes|alcanzas a) ver|m[ií]ra(me|nos)|"
    r"(frente a|delante de|enfrente de) ti|qu[eé] hay (aqu[ií]|ah[ií]|all[aá]|frente a ti|delante de ti))\b",
    re.I)


def _tool(name: str, description: str, **params: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "required": list(params), "properties": params}}}


TOOLS = [
    _tool("mirar", "Toma una foto ahora con tu cámara y responde la pregunta sobre lo que se ve.",
          pregunta={"type": "string", "description": "Qué quieres saber de la imagen, en español"}),
    _tool("estado_rover", "Estado de hoy de un rover de la NASA en Marte: último sol, fotos tomadas y si sigue activo.",
          rover={"type": "string", "enum": ["perseverance", "curiosity"]}),
    _tool("foto_del_dia", "La foto astronómica del día de la NASA (APOD): título, fecha y explicación en inglés."),
]
_NASA_TOOLS = {"estado_rover", "foto_del_dia"}


class BrainError(Exception):
    pass


class Brain:
    FAILURE = "Base, tengo una falla en mis sistemas. Intenta de nuevo."
    DEADLINE_S = 30.0  # a whole answer, first byte to last sentence

    def __init__(self, settings: Settings):
        self.settings = settings
        self.url = settings.ollama_url.rstrip("/") + "/api/chat"
        self.facts = prompts.load_facts()
        self.deadline_s = self.DEADLINE_S

    def warm(self) -> None:
        """Load gpt-oss now and keep it loaded (an empty chat loads the model, generates nothing)."""
        body = {"model": self.settings.llm, "messages": [], "keep_alive": _LLM_KEEP_ALIVE, "stream": False}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_WARM_TIMEOUT_S) as resp:
            reply = json.loads(resp.read() or b"{}")
        if "error" in reply:
            raise BrainError(f"ollama: {reply['error']}")

    def answer(self, history: list[dict], name: str, camera) -> Iterator[str]:
        """Speakable sentences as they stream; ends with FAILURE if anything breaks.

        The whole answer gets deadline_s: past it, FAILURE if nothing was said yet, else it just
        stops. The work runs in a thread so a stalled Ollama read can't hold the rover hostage."""
        out: queue.Queue = queue.Queue()
        stop = threading.Event()
        worker = threading.Thread(target=self._work, args=(history, name, camera, out, stop),
                                  name="houston-brain", daemon=True)
        worker.start()
        deadline = time.monotonic() + self.deadline_s
        said = False
        try:
            while True:
                try:
                    kind, value = out.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    log.warning("brain: answer cut at %.0f s", self.deadline_s)
                    if not said:
                        yield self.FAILURE
                    return
                if kind == "sentence":
                    said = True
                    yield value
                    continue
                if kind == "error" or not said:
                    log.warning("brain failure: %s", value or "empty answer")
                    yield self.FAILURE
                return
        finally:
            stop.set()  # also on close(): the worker drops the Ollama stream at its next line

    def _work(self, history: list[dict], name: str, camera, out: queue.Queue,
              stop: threading.Event) -> None:
        try:
            for sentence in self._answer(history, name, camera, stop):
                out.put(("sentence", sentence))
            out.put(("end", None))
        except Exception as e:  # camera, Ollama, NASA-unrelated bugs: the rover must still speak
            out.put(("error", e))

    def _answer(self, history: list[dict], name: str, camera,
                stop: threading.Event) -> Iterator[str]:
        question = history[-1]["content"]
        if LOOK.search(question):
            yield from self._look(question, name, camera, stop)
            return
        messages = [{"role": "system", "content": prompts.system(name, self.facts)}]
        messages += [dict(m) for m in history[-HISTORY_TURNS:]]
        think = "medium"
        for _ in range(MAX_TOOL_ROUNDS + 1):
            splitter, content, thinking, calls = Splitter(), "", "", []
            for msg in self._stream({"model": self.settings.llm, "think": think, "tools": TOOLS,
                                     "keep_alive": _LLM_KEEP_ALIVE, "messages": messages}, stop):
                calls += msg.get("tool_calls") or []
                content += msg.get("content") or ""
                thinking += msg.get("thinking") or ""  # echoed back to the model, never spoken
                yield from splitter.push(msg.get("content") or "")
            yield from splitter.flush()
            if not calls:
                return
            for call in calls:
                fn = call.get("function") or {}
                if fn.get("name") == "mirar":
                    yield from self._look((fn.get("arguments") or {}).get("pregunta") or question,
                                          name, camera, stop)
                    return
            messages.append({"role": "assistant", "content": content, "thinking": thinking, "tool_calls": calls})
            for call in calls:
                tool, result = self._run_tool(call.get("function") or {})
                messages.append({"role": "tool", "tool_name": tool, "content": json.dumps(result, ensure_ascii=False)})
                if tool in _NASA_TOOLS:
                    think = "low"  # answering from NASA data, not reasoning
        raise BrainError(f"still calling tools after {MAX_TOOL_ROUNDS} rounds")

    def _run_tool(self, fn: dict) -> tuple[str, dict]:
        """NASA tool result, or {"error": ...} for the model to explain (one honest sentence)."""
        name, args = fn.get("name", ""), fn.get("arguments") or {}
        try:
            if name == "estado_rover":
                return name, nasa.rover_status(str(args.get("rover", "")))
            if name == "foto_del_dia":
                return name, nasa.apod(nasa.key(self.settings.nasa_key_file))
        except nasa.NasaError as e:
            log.warning("nasa tool %s: %s", name, e)
            return name, {"error": f"no se pudo consultar a la NASA: {e}"}
        return name, {"error": f"herramienta desconocida: {name}"}

    def _look(self, question: str, name: str, camera, stop: threading.Event) -> Iterator[str]:
        jpeg = camera.snapshot(timeout=_SNAPSHOT_TIMEOUT_S)  # CameraError -> FAILURE, no made-up scene
        splitter = Splitter()
        for msg in self._stream({"model": self.settings.vlm, "think": False, "keep_alive": _LLM_KEEP_ALIVE, "messages": [
                {"role": "system", "content": prompts.vision(name)},
                {"role": "user", "content": question, "images": [base64.b64encode(jpeg).decode()]}]},
                stop):
            yield from splitter.push(msg.get("content") or "")
        yield from splitter.flush()

    def _stream(self, body: dict, stop: threading.Event) -> Iterator[dict]:
        """`message` of each NDJSON chunk from Ollama /api/chat (thinking is never spoken)."""
        req = urllib.request.Request(self.url, data=json.dumps({**body, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            for line in resp:
                if stop.is_set():
                    raise BrainError("answer abandoned")
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise BrainError(f"ollama: {chunk['error']}")
                yield chunk.get("message") or {}
                if chunk.get("done"):
                    return
        raise BrainError("ollama stream ended without done")
