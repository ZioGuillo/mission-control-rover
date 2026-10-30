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

from houston import calc, effects, nasa, prompts
from houston.config import Settings
from houston.sentences import Splitter

log = logging.getLogger(__name__)

HISTORY_TURNS = 8
STORY_PARTS = 4  # one answer came out 640 to 1000 words; six parts made ~2900 (19 min); four is ~10 min
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
HOMEWORK_TOOLS = TOOLS + [
    _tool("calcular", "Calcula exacto una cuenta: suma, resta, multiplicación, división (con cociente y residuo) o "
          "potencia. Úsala para toda cuenta, y antes de decir si una respuesta está bien o mal.",
          expresion={"type": "string", "description": "La cuenta con números y + - * / ( ), por ejemplo 7*8 o 123-47"})]


class BrainError(Exception):
    pass

# "Dame una demo de la charla", "da la charla como si fueras yo": Bentley gives it himself, from the script.
DEMO = re.compile(
    r"\b(demo\w*\b.*\bcharla|charla\b.*\bdemo|(da|dar|dame|haz|hacer|hazme|presenta|presentar|pres[eé]ntame)\s+(la|mi|tu)\s+charla|"
    r"como si (la )?(dieras|fueras yo|estuvieras en el escenario))", re.I)
# "Sigue", "¿puedes continuar?", "el siguiente acto": go on with the demo, only right after one.
# Live 2026-10-29: "¿Puedes continuar?" missed the old "sigue|continúa" and got a rushed summary.
GO_ON = re.compile(r"\b(sigue|segu[ií]|seguir|contin[uú]a|contin[uú]e|continuar|dale|y (despu[eé]s|luego)|"
                   r"lo que sigue|siguiente|el resto|otro acto)\b", re.I)
GO_ON_MAX_WORDS = 8  # "¿Qué sigue después de Marte en la exploración espacial?" is a real question


def is_demo(history: list[dict]) -> bool:
    """The last question asks for the talk demo, or says "sigue" right after one."""
    questions = [m["content"] for m in history if m.get("role") == "user"]
    for i in range(len(questions) - 1, -1, -1):
        if DEMO.search(questions[i]):
            return True
        if not (GO_ON.search(questions[i]) and len(questions[i].split()) <= GO_ON_MAX_WORDS):
            return False
    return False


# "Cuéntale una historia de dragones a Lucca", or "¡sí!" right after Bentley offered one.
# Live 2026-10-29: "Una historia para Luca, de dragones" has no verb; "la historia de Marte" is a question.
STORY = re.compile(r"\b(cu[eé]nta(me|le|nos|les)?|quiero (o[ií]r|escuchar)|queremos (o[ií]r|escuchar))\b"
                   r".*\b(historia|cuento)|\b(una|otra|un|otro) (historia|cuento)\b", re.I)
YES = re.compile(r"^\W*(\w+,?\s+)?(s[ií]+|claro|dale|obvio|bueno|ok|okay|yes|por favor|quiero)\b", re.I)


def is_story(history: list[dict]) -> bool:
    """The last question asks for a story, or says yes to the story Bentley just offered."""
    if not history or history[-1].get("role") != "user":
        return False
    question = history[-1]["content"]
    if STORY.search(question):
        return not re.search(r"\bpreg[uú]nta", question, re.I)  # "pregúntale si quiere…": ask first
    offer = history[-2]["content"] if len(history) > 1 and history[-2].get("role") == "assistant" else ""
    return bool(YES.search(question) and re.search(r"\b(historia|cuento)\b", offer, re.I) and "?" in offer)


# Homework: once a conversation is about tareas, it stays in tutor mode until it ends.
HOMEWORK = re.compile(r"\b(tareas?|deberes|homework|ay[uú]da(me|le)? (con|a hacer) (la|las|mi|mis|su|sus))\b", re.I)


def is_homework(history: list[dict]) -> bool:
    """This conversation is about homework: asked for, or a yes to Bentley's offer, and still going."""
    for i, m in enumerate(history):
        if m.get("role") == "user" and HOMEWORK.search(m["content"]):
            return True
        if (m.get("role") == "assistant" and HOMEWORK.search(m["content"]) and "?" in m["content"]
                and i + 1 < len(history) and YES.search(history[i + 1]["content"])):
            return True
    return False


class Brain:
    FAILURE = "Base, tengo una falla en mis sistemas. Intenta de nuevo."
    # Said as-is when a NASA lookup fails: the error never goes back to the model, which once
    # turned an APOD timeout into an invented "photo of 28 October 2027".
    NASA_DOWN = "No pude comunicarme con la NASA ahora mismo. Intenta en un rato."
    DEADLINE_S = 30.0  # a whole answer, first byte to last sentence
    DEMO_DEADLINE_S = 120.0  # a talk demo is a whole act, about fifteen sentences
    STORY_DEADLINE_S = 300.0  # a ten-minute story for Lucca

    def __init__(self, settings: Settings):
        self.settings = settings
        self.url = settings.ollama_url.rstrip("/") + "/api/chat"
        self.facts = prompts.load_facts()
        self.script = prompts.load_script()
        self.deadline_s = self.DEADLINE_S
        self.demo_deadline_s = self.DEMO_DEADLINE_S
        self.story_deadline_s = self.STORY_DEADLINE_S

    def warm(self) -> None:
        """Load gpt-oss now and keep it loaded (an empty chat loads the model, generates nothing)."""
        body = {"model": self.settings.llm, "messages": [], "keep_alive": _LLM_KEEP_ALIVE, "stream": False}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_WARM_TIMEOUT_S) as resp:
            reply = json.loads(resp.read() or b"{}")
        if "error" in reply:
            raise BrainError(f"ollama: {reply['error']}")

    def answer(self, history: list[dict], name: str, camera, personality: str = "",
               note: str = "") -> Iterator[str]:
        """Speakable sentences as they stream; ends with FAILURE if anything breaks.

        The whole answer gets deadline_s: past it, FAILURE if nothing was said yet, else it just
        stops. The work runs in a thread so a stalled Ollama read can't hold the rover hostage."""
        out: queue.Queue = queue.Queue()
        stop = threading.Event()
        worker = threading.Thread(target=self._work, args=(history, name, camera, personality, note, out, stop),
                                  name="houston-brain", daemon=True)
        worker.start()
        long = self.demo_deadline_s if is_demo(history) else self.story_deadline_s if is_story(history) else None
        deadline = time.monotonic() + (long or self.deadline_s)
        said = False
        try:
            while True:
                try:
                    kind, value = out.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    log.warning("brain: answer cut at the deadline")
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

    def _work(self, history: list[dict], name: str, camera, personality: str, note: str,
              out: queue.Queue, stop: threading.Event) -> None:
        try:
            for sentence in self._answer(history, name, camera, personality, note, stop):
                out.put(("sentence", sentence))
            out.put(("end", None))
        except Exception as e:  # camera, Ollama, NASA-unrelated bugs: the rover must still speak
            out.put(("error", e))

    def _answer(self, history: list[dict], name: str, camera, personality: str, note: str,
                stop: threading.Event) -> Iterator[str]:
        question = history[-1]["content"]
        if LOOK.search(question):
            yield from self._look(question, name, camera, personality, stop)
            return
        demo = is_demo(history)
        story = not demo and is_story(history)
        homework = not (demo or story) and is_homework(history)
        messages = [{"role": "system", "content": prompts.system(name, self.facts, personality, demo, self.script,
                                                                 note, story, homework)}]
        messages += [dict(m) for m in history[-HISTORY_TURNS:]]
        think = self.settings.think
        if story:
            yield from self._story(messages, think, stop)
            return
        for _ in range(MAX_TOOL_ROUNDS + 1):
            splitter, content, thinking, calls = Splitter(), "", "", []
            for msg in self._stream({"model": self.settings.llm, "think": think,
                                     "tools": HOMEWORK_TOOLS if homework else TOOLS,
                                     "keep_alive": _LLM_KEEP_ALIVE, "messages": messages}, stop):
                calls += msg.get("tool_calls") or []
                content += msg.get("content") or ""
                thinking += msg.get("thinking") or ""  # echoed back to the model, never spoken
                for sentence in splitter.push(msg.get("content") or ""):
                    yield from effects.split(sentence)
            for sentence in splitter.flush():
                yield from effects.split(sentence)
            if not calls:
                return
            for call in calls:
                fn = call.get("function") or {}
                if fn.get("name") == "mirar":
                    yield from self._look((fn.get("arguments") or {}).get("pregunta") or question,
                                          name, camera, personality, stop)
                    return
            messages.append({"role": "assistant", "content": content, "thinking": thinking, "tool_calls": calls})
            for call in calls:
                tool, result = self._run_tool(call.get("function") or {})
                if tool in _NASA_TOOLS and "error" in result:
                    yield self.NASA_DOWN
                    return
                messages.append({"role": "tool", "tool_name": tool, "content": json.dumps(result, ensure_ascii=False)})
                if tool in _NASA_TOOLS:
                    think = "low"  # answering from NASA data, not reasoning
        raise BrainError(f"still calling tools after {MAX_TOOL_ROUNDS} rounds")

    def _story(self, messages: list[dict], think: str, stop: threading.Event) -> Iterator[str]:
        """The story in STORY_PARTS answers, each picking up where the last one stopped.
        gpt-oss writes a part in seconds and Bentley tells it in minutes, so there is no gap."""
        told = ""
        for part in range(1, STORY_PARTS + 1):
            ask = messages if part == 1 else messages + [
                {"role": "assistant", "content": told},
                {"role": "user", "content": f"Sigue con la parte {part} de {STORY_PARTS}."}]
            splitter, content = Splitter(), ""
            for msg in self._stream({"model": self.settings.llm, "think": think,
                                     "keep_alive": _LLM_KEEP_ALIVE, "messages": ask}, stop):
                content += msg.get("content") or ""
                for sentence in splitter.push(msg.get("content") or ""):
                    yield from effects.split(sentence)
            for sentence in splitter.flush():
                yield from effects.split(sentence)
            told = (told + " " + content).strip()

    def _run_tool(self, fn: dict) -> tuple[str, dict]:
        """NASA tool result, or {"error": ...}: a NASA error is spoken as NASA_DOWN, never improvised on."""
        name, args = fn.get("name", ""), fn.get("arguments") or {}
        if name == "calcular":
            try:
                return name, calc.calculate(str(args.get("expresion", "")))
            except calc.CalcError as e:
                return name, {"error": str(e)}
        try:
            if name == "estado_rover":
                return name, nasa.rover_status(str(args.get("rover", "")))
            if name == "foto_del_dia":
                return name, nasa.apod(nasa.key(self.settings.nasa_key_file))
        except nasa.NasaError as e:
            log.warning("nasa tool %s: %s", name, e)
            return name, {"error": f"no se pudo consultar a la NASA: {e}"}
        return name, {"error": f"herramienta desconocida: {name}"}

    def _look(self, question: str, name: str, camera, personality: str, stop: threading.Event) -> Iterator[str]:
        jpeg = camera.snapshot(timeout=_SNAPSHOT_TIMEOUT_S)  # CameraError -> FAILURE, no made-up scene
        splitter = Splitter()
        for msg in self._stream({"model": self.settings.vlm, "think": False, "keep_alive": _LLM_KEEP_ALIVE, "messages": [
                {"role": "system", "content": prompts.vision(name, personality)},
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
