"""The brain: gpt-oss streams the answer sentence by sentence; qwen3.8 looks through the camera.

Camera questions caught by LOOK go straight to the vision model (no gpt-oss hop). Any failure
(camera, Ollama, too many tool rounds, nothing to say) ends the answer with FAILURE, so the rover
never describes a scene it didn't photograph.
"""
from __future__ import annotations

import base64
import json
import logging
import re
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

LOOK = re.compile(
    r"\b((puedes|alcanzas a) ver|ves|veas|viendo|m[ií]ra(me|lo|la|nos)?|miras|mirando|c[aá]mara|"
    r"delante de ti|frente a ti|enfrente|alrededor|qu[eé] hay (aqu[ií]|ah[ií]|all[aá]|frente|delante))\b",
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

    def __init__(self, settings: Settings):
        self.settings = settings
        self.url = settings.ollama_url.rstrip("/") + "/api/chat"
        self.facts = prompts.load_facts()

    def answer(self, history: list[dict], name: str, camera) -> Iterator[str]:
        """Speakable sentences as they stream; ends with FAILURE if anything breaks."""
        said = False
        try:
            for sentence in self._answer(history, name, camera):
                said = True
                yield sentence
            if not said:
                raise BrainError("empty answer")
        except Exception as e:  # camera, Ollama, NASA-unrelated bugs: the rover must still speak
            log.warning("brain failure: %s", e)
            yield self.FAILURE

    def _answer(self, history: list[dict], name: str, camera) -> Iterator[str]:
        question = history[-1]["content"]
        if LOOK.search(question):
            yield from self._look(question, name, camera)
            return
        messages = [{"role": "system", "content": prompts.system(name, self.facts)}]
        messages += [dict(m) for m in history[-HISTORY_TURNS:]]
        think = "medium"
        for _ in range(MAX_TOOL_ROUNDS + 1):
            splitter, content, calls = Splitter(), "", []
            for msg in self._stream({"model": self.settings.llm, "think": think, "tools": TOOLS,
                                     "keep_alive": "24h", "messages": messages}):
                calls += msg.get("tool_calls") or []
                content += msg.get("content") or ""
                yield from splitter.push(msg.get("content") or "")
            yield from splitter.flush()
            if not calls:
                return
            for call in calls:
                fn = call.get("function") or {}
                if fn.get("name") == "mirar":
                    yield from self._look((fn.get("arguments") or {}).get("pregunta") or question, name, camera)
                    return
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
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

    def _look(self, question: str, name: str, camera) -> Iterator[str]:
        jpeg = camera.snapshot(timeout=_SNAPSHOT_TIMEOUT_S)  # CameraError -> FAILURE, no made-up scene
        splitter = Splitter()
        for msg in self._stream({"model": self.settings.vlm, "think": False, "keep_alive": "10m", "messages": [
                {"role": "system", "content": prompts.vision(name)},
                {"role": "user", "content": question, "images": [base64.b64encode(jpeg).decode()]}]}):
            yield from splitter.push(msg.get("content") or "")
        yield from splitter.flush()

    def _stream(self, body: dict) -> Iterator[dict]:
        """`message` of each NDJSON chunk from Ollama /api/chat (thinking is never spoken)."""
        req = urllib.request.Request(self.url, data=json.dumps({**body, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise BrainError(f"ollama: {chunk['error']}")
                yield chunk.get("message") or {}
                if chunk.get("done"):
                    return
        raise BrainError("ollama stream ended without done")
