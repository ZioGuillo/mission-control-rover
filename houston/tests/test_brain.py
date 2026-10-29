import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from houston import nasa, prompts
from houston.brain import LOOK, Brain, BrainError
from houston.camera import CameraError
from houston.config import Settings
from houston.tests import fakehttp

JPEG = b"\xff\xd8fake-frame\xff\xd9"


def say(text):
    return {"message": {"role": "assistant", "content": text}, "done": False}


def think(text):
    return {"message": {"role": "assistant", "content": "", "thinking": text}, "done": False}


def call(name, **args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]}, "done": False}


DONE = {"message": {"role": "assistant", "content": ""}, "done": True}


class FakeOllama:
    """Scripted /api/chat: one NDJSON reply per request. A threading.Event in a reply
    makes the server hold the connection open until the test sets it."""

    def __init__(self, *replies, status=200):
        self.replies = list(replies)
        self.status = status
        self.bodies = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                fake.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                reply = fake.replies.pop(0) if fake.replies else [DONE]
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for item in reply:
                    if isinstance(item, threading.Event):
                        item.wait(5)
                    else:
                        self.wfile.write(json.dumps(item).encode() + b"\n")
                        self.wfile.flush()

            def log_message(self, *a):
                pass

        self.srv, self.url = fakehttp.start(H)


class FakeCamera:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def snapshot(self, timeout=6.0):
        self.calls += 1
        if self.error:
            raise self.error
        return JPEG


@pytest.fixture
def ollama():
    made = []

    def make(*replies, status=200):
        fake = FakeOllama(*replies, status=status)
        made.append(fake)
        return fake

    yield make
    for f in made:
        fakehttp.stop(f.srv)


def brain_for(url, tmp_path=Path("/nonexistent")):
    return Brain(Settings(rovers=[], ollama_url=url, nasa_key_file=tmp_path / "nasa_key"))


def ask(text):
    return [{"role": "user", "content": text}]


def test_sentences_stream_before_ollama_finishes(ollama):
    hold = threading.Event()
    fake = ollama([think("pienso..."), say("Setenta y "), say("dos vuelos. "), say("Ingenuity "),
                   hold, say("voló en Marte."), DONE])
    answer = brain_for(fake.url).answer(ask("¿Cuántos vuelos hizo Ingenuity?"), "Fred", FakeCamera())
    assert next(answer) == "Setenta y dos vuelos."
    assert not hold.is_set()  # the fake is still holding the stream open
    hold.set()
    assert list(answer) == ["Ingenuity voló en Marte."]


def test_request_body(ollama):
    fake = ollama([say("Hola base."), DONE])
    history = [{"role": "user", "content": "Hola"}, {"role": "assistant", "content": "¡Hola!"},
               {"role": "user", "content": "¿Qué tal Marte?"}]
    before = json.dumps(history)
    assert list(brain_for(fake.url).answer(history, "Eva", FakeCamera())) == ["Hola base."]
    assert json.dumps(history) == before  # history is not mutated
    body = fake.bodies[0]
    assert body["model"] == "gpt-oss:120b"
    assert body["stream"] is True and body["think"] == "medium" and body["keep_alive"] == -1
    assert {t["function"]["name"] for t in body["tools"]} == {"mirar", "foto_del_dia", "estado_rover"}
    system = body["messages"][0]
    assert system["role"] == "system" and "Eva" in system["content"]
    assert "687" in system["content"]  # the fact sheet is in the prompt...
    assert "https://" not in system["content"]  # ...without its source URLs
    assert body["messages"][1:] == history


def test_mirar_tool_takes_photo_and_asks_vision_model(ollama):
    fake = ollama([call("mirar", pregunta="¿Quién está frente a ti?"), DONE],
                  [say("Veo a una persona. Está sonriendo."), DONE])
    cam = FakeCamera()
    out = list(brain_for(fake.url).answer(ask("¿Quién está contigo?"), "Fred", cam))
    assert out == ["Veo a una persona.", "Está sonriendo."]
    assert cam.calls == 1
    vision = fake.bodies[1]
    assert vision["model"] == "qwen3.8:27b" and vision["think"] is False and vision["keep_alive"] == -1
    user = vision["messages"][-1]
    assert user["content"] == "¿Quién está frente a ti?"
    assert base64.b64decode(user["images"][0]) == JPEG
    assert "Fred" in vision["messages"][0]["content"]


def test_camera_question_skips_gpt_oss(ollama):
    fake = ollama([say("Veo una mesa con un rover. "), DONE])
    cam = FakeCamera()
    out = list(brain_for(fake.url).answer(ask("Fred, ¿qué ves?"), "Fred", cam))
    assert out == ["Veo una mesa con un rover."]
    assert [b["model"] for b in fake.bodies] == ["qwen3.8:27b"]
    assert cam.calls == 1


@pytest.mark.parametrize("question, first_reply", [
    ("¿Qué ves?", None),  # fast path
    ("¿Quién está contigo?", [call("mirar", pregunta="¿Quién está?"), DONE]),  # tool path
])
def test_camera_failure_yields_only_failure(ollama, question, first_reply):
    fake = ollama(*([first_reply] if first_reply else []), [say("Veo un gato inventado."), DONE])
    cam = FakeCamera(error=CameraError("app keeps redirecting to /login after login"))
    out = list(brain_for(fake.url).answer(ask(question), "Fred", cam))
    assert out == [Brain.FAILURE]
    assert all(b["model"] != "qwen3.8:27b" for b in fake.bodies)


def test_estado_rover_tool_round_trip(ollama, monkeypatch):
    asked = []

    def fake_status(rover):
        asked.append(rover)
        return {"sol": 2022, "total_photos": 1054650, "status": "activo"}

    monkeypatch.setattr(nasa, "rover_status", fake_status)
    fake = ollama([call("estado_rover", rover="perseverance"), DONE],
                  [say("Va en el sol dos mil veintidós. "), DONE])
    out = list(brain_for(fake.url).answer(ask("¿Cómo va Perseverance?"), "Fred", FakeCamera()))
    assert out == ["Va en el sol dos mil veintidós."]
    assert asked == ["perseverance"]
    second = fake.bodies[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["function"]["name"] == "estado_rover"
    assert second[-1]["role"] == "tool" and second[-1]["tool_name"] == "estado_rover"
    assert json.loads(second[-1]["content"])["sol"] == 2022
    assert fake.bodies[1]["think"] == "low"  # answered from NASA data


def test_foto_del_dia_uses_key_file(ollama, monkeypatch, tmp_path):
    keys = []
    monkeypatch.setattr(nasa, "apod", lambda key: keys.append(key) or {"title": "Jezero"})
    (tmp_path / "nasa_key").write_text("k-123\n")
    fake = ollama([call("foto_del_dia"), DONE], [say("Hoy es Jezero."), DONE])
    out = list(brain_for(fake.url, tmp_path).answer(ask("¿Cuál es la foto del día?"), "Eva", FakeCamera()))
    assert out == ["Hoy es Jezero."] and keys == ["k-123"]


def test_nasa_error_goes_back_to_model(ollama, monkeypatch):
    def boom(rover):
        raise nasa.NasaError("HTTP 503")

    monkeypatch.setattr(nasa, "rover_status", boom)
    fake = ollama([call("estado_rover", rover="curiosity"), DONE],
                  [say("No pude contactar a la NASA. "), DONE])
    out = list(brain_for(fake.url).answer(ask("¿Cómo va Curiosity?"), "Fred", FakeCamera()))
    assert out == ["No pude contactar a la NASA."]
    assert "error" in json.loads(fake.bodies[1]["messages"][-1]["content"])


def test_tool_loop_is_capped(ollama, monkeypatch):
    monkeypatch.setattr(nasa, "rover_status", lambda rover: {"sol": 1})
    loop = [call("estado_rover", rover="curiosity"), DONE]
    fake = ollama(*[loop] * 6)
    out = list(brain_for(fake.url).answer(ask("¿Cómo va Curiosity?"), "Fred", FakeCamera()))
    assert out == [Brain.FAILURE]
    assert len(fake.bodies) == 4  # first answer + 3 tool rounds


def test_unpunctuated_answer_is_flushed(ollama):
    fake = ollama([say("**Claro** que "), say("sí 🚀"), DONE])
    assert list(brain_for(fake.url).answer(ask("¿Te gusta Marte?"), "Fred", FakeCamera())) == ["Claro que sí"]


def test_empty_answer_is_failure(ollama):
    fake = ollama([think("hmm"), say("  "), DONE])
    assert list(brain_for(fake.url).answer(ask("¿Hola?"), "Fred", FakeCamera())) == [Brain.FAILURE]


def test_ollama_http_error_is_failure(ollama):
    fake = ollama([{"error": "model not found"}], status=404)
    assert list(brain_for(fake.url).answer(ask("¿Hola?"), "Fred", FakeCamera())) == [Brain.FAILURE]


def test_ollama_error_line_mid_stream_is_failure(ollama):
    fake = ollama([{"error": "out of memory"}])
    assert list(brain_for(fake.url).answer(ask("¿Hola?"), "Fred", FakeCamera())) == [Brain.FAILURE]


def test_ollama_down_is_failure():
    assert list(brain_for("http://127.0.0.1:1").answer(ask("¿Hola?"), "Fred", FakeCamera())) == [Brain.FAILURE]


def test_system_prompt_rules():
    text = prompts.system("Eva", "Un año en Marte dura 687 días.")
    assert "Eva" in text and "687 días" in text
    assert "mirar" in text and " tú" in text
    assert "muy corta" in text


def test_fact_sheet_loads_without_urls_or_header():
    facts = prompts.load_facts()
    assert "Revisado por Pablo" not in facts and "http" not in facts
    assert "687" in facts and "72" in facts
    assert 15 <= len(facts.splitlines()) <= 30


@pytest.mark.parametrize("question", [
    "¿Qué ves?", "¿qué estás viendo?", "¿Puedes ver algo?", "Mírame, Fred", "¿Qué hay frente a ti?",
    "qué ves", "¿Qué ves, Fred?", "Dime qué ves.", "¿Qué ves ahora?", "¿qué estás viendo ahí?",
    "¿Qué ves aquí?", "¿Qué ves frente a ti?", "¿Qué ves delante de ti?", "¿Qué ves alrededor de ti?"])
def test_look_catches_deictic_camera_questions(question):
    assert LOOK.search(question)


@pytest.mark.parametrize("question", [
    "¿Cuánto tarda Marte en dar la vuelta alrededor del Sol?",
    "Mira, Fred, ¿cuántas lunas tiene Marte?",
    "¿Qué cámara tiene Perseverance?",
    "¿Cómo ves el futuro de la exploración de Marte?",
    "¿A veces ves estrellas?",
    "¿Qué hay frente a tierra firme?", "¿Qué vestido usaban los astronautas?",
    "¿Qué ves en el cielo de Marte?", "¿Qué ves cuando miras las estrellas?",
    "¿Qué estás viendo en tus datos de la NASA?"])
def test_look_leaves_talk_questions_to_gpt_oss(question):
    assert not LOOK.search(question)


def test_tool_round_echoes_thinking_but_never_speaks_it(ollama, monkeypatch):
    monkeypatch.setattr(nasa, "rover_status", lambda rover: {"sol": 5057})
    fake = ollama([think("Necesito datos. "), think("Llamo la herramienta."), call("estado_rover", rover="curiosity"), DONE],
                  [think("Ya tengo el sol."), say("Va en el sol cinco mil cincuenta y siete. "), DONE])
    out = list(brain_for(fake.url).answer(ask("¿Cómo va Curiosity?"), "Fred", FakeCamera()))
    assert out == ["Va en el sol cinco mil cincuenta y siete."]
    echoed = fake.bodies[1]["messages"][-2]
    assert echoed["role"] == "assistant" and echoed["thinking"] == "Necesito datos. Llamo la herramienta."


def test_warm_loads_gpt_oss_and_keeps_it(ollama):
    fake = ollama([DONE])
    brain_for(fake.url).warm()
    assert fake.bodies == [{"model": "gpt-oss:120b", "messages": [], "keep_alive": -1, "stream": False}]


def test_warm_raises_when_ollama_fails(ollama):
    fake = ollama([{"error": "model not found"}], status=404)
    with pytest.raises((BrainError, OSError)):
        brain_for(fake.url).warm()
    with pytest.raises((BrainError, OSError)):
        brain_for("http://127.0.0.1:1").warm()


def test_slow_answer_with_nothing_said_is_failure_at_deadline(ollama):
    hold = threading.Event()
    fake = ollama([think("pienso"), hold, say("Tarde."), DONE])
    brain = brain_for(fake.url)
    brain.deadline_s = 0.3
    t0 = time.monotonic()
    out = list(brain.answer(ask("¿Qué es Marte?"), "Fred", FakeCamera()))
    assert out == [Brain.FAILURE]
    assert time.monotonic() - t0 < 2
    hold.set()


def test_slow_answer_after_first_sentence_stops_quietly(ollama):
    hold = threading.Event()
    fake = ollama([say("Primera frase. "), hold, say("Segunda."), DONE])
    brain = brain_for(fake.url)
    brain.deadline_s = 0.3
    assert brain.deadline_s < Brain.DEADLINE_S == 30.0
    out = list(brain.answer(ask("¿Qué es Marte?"), "Fred", FakeCamera()))
    assert out == ["Primera frase."]
    hold.set()


def test_personality_goes_into_both_prompts(ollama):
    fake = ollama([call("mirar", pregunta="¿Qué hay?"), DONE], [say("Veo un sofá."), DONE])
    shiba = "Tienes la personalidad de un perro Shiba Inu."
    list(brain_for(fake.url).answer([{"role": "user", "content": "¿Hay alguien contigo?"}], "Bentley", FakeCamera(),
                                    personality=shiba))
    assert shiba in fake.bodies[0]["messages"][0]["content"]
    assert "Bentley" in fake.bodies[0]["messages"][0]["content"]
    assert shiba in fake.bodies[1]["messages"][0]["content"]  # the vision prompt too
