import json
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from houston import claude
from houston.brain import Brain
from houston.config import Settings
from houston.tests import fakehttp
from houston.tests.test_brain import DONE, FakeCamera, FakeOllama, ask, say


def sse(*events):
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events]


def text(*parts):
    events = [{"type": "message_start", "message": {}},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
    events += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}} for p in parts]
    events += [{"type": "content_block_stop", "index": 0}, {"type": "message_stop"}]
    return sse(*events)


def tool_use(name, args, tool_id="toolu_1"):
    raw = json.dumps(args)
    return sse({"type": "message_start", "message": {}},
               {"type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": raw[:5]}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": raw[5:]}},
               {"type": "content_block_stop", "index": 0}, {"type": "message_stop"})


class FakeClaude:
    def __init__(self, *replies, status=200):
        self.replies, self.status, self.bodies, self.headers = list(replies), status, [], []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                fake.headers.append(dict(self.headers))
                fake.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(fake.status)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in (fake.replies.pop(0) if fake.replies else []):
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()

            def log_message(self, *a):
                pass

        self.srv, self.url = fakehttp.start(H)


@pytest.fixture
def servers():
    made = []

    def make(cls, *replies, status=200):
        fake = cls(*replies, status=status)
        made.append(fake)
        return fake

    yield make
    for f in made:
        fakehttp.stop(f.srv)


def brain(tmp_path, claude_url, ollama_url="http://127.0.0.1:9", key="sk-test"):
    key_file = tmp_path / "anthropic_key"
    if key is not None:
        key_file.write_text(key + "\n")
    return Brain(Settings(rovers=[], ollama_url=ollama_url, anthropic_url=claude_url,
                          anthropic_key_file=key_file, nasa_key_file=tmp_path / "nasa_key"))


def test_request_maps_system_tools_images_and_tool_results():
    body = {"model": "gpt-oss", "tools": [{"type": "function", "function": {
        "name": "mirar", "description": "Mira", "parameters": {"type": "object", "properties": {}}}}],
        "messages": [{"role": "system", "content": "Eres Sofi."},
                     {"role": "assistant", "content": "Hola, ¿qué tal?"},  # the rover spoke first
                     {"role": "user", "content": "¿Qué ves?", "images": ["QUJD"]},
                     {"role": "assistant", "content": "", "thinking": "x",
                      "tool_calls": [{"id": "t1", "function": {"name": "mirar", "arguments": {"pregunta": "q"}}}]},
                     {"role": "tool", "tool_name": "mirar", "content": "{}"},
                     {"role": "user", "content": "¿Y ahora?"}]}
    req = claude.request(body, "claude-haiku-5-5", 100)
    assert req["system"] == "Eres Sofi." and req["model"] == "claude-haiku-5-5" and req["max_tokens"] == 100
    assert req["tools"] == [{"name": "mirar", "description": "Mira", "input_schema": {"type": "object", "properties": {}}}]
    roles = [m["role"] for m in req["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]  # alternating, user first
    image = req["messages"][2]["content"][0]
    assert image["type"] == "image" and image["source"]["data"] == "QUJD"
    assert req["messages"][3]["content"] == [{"type": "tool_use", "id": "t1", "name": "mirar", "input": {"pregunta": "q"}}]
    # The tool result and the next question share one user turn.
    assert req["messages"][4]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}
    assert req["messages"][4]["content"][1] == {"type": "text", "text": "¿Y ahora?"}


def test_claude_answer_streams_sentences(tmp_path, servers):
    fake = servers(FakeClaude, text("Setenta y ", "dos vuelos. ", "Ingenuity voló en Marte."))
    out = list(brain(tmp_path, fake.url).answer(ask("¿Cuántos vuelos?"), "Sofi", FakeCamera(), llm="claude"))
    assert out == ["Setenta y dos vuelos.", "Ingenuity voló en Marte."]
    assert {k.lower(): v for k, v in fake.headers[0].items()}["x-api-key"] == "sk-test"
    assert "Sofi" in fake.bodies[0]["system"]


def test_claude_tool_call_runs_the_tool_and_answers(tmp_path, servers):
    fake = servers(FakeClaude, tool_use("calcular", {"expresion": "7*8"}), text("Siete por ocho es cincuenta y seis."))
    history = ask("Ayúdame con la tarea: ¿cuánto es siete por ocho?")
    out = list(brain(tmp_path, fake.url).answer(history, "Sofi", FakeCamera(), llm="claude"))
    assert out == ["Siete por ocho es cincuenta y seis."]
    result = fake.bodies[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1" and "56" in result["content"]


def test_claude_looks_through_the_camera(tmp_path, servers):
    fake = servers(FakeClaude, text("Veo una mesa con legos."))
    out = list(brain(tmp_path, fake.url).answer(ask("Hey Sofi, ¿qué ves?"), "Sofi", FakeCamera(), llm="claude"))
    assert out == ["Veo una mesa con legos."]
    assert fake.bodies[0]["messages"][0]["content"][0]["type"] == "image"


def test_claude_down_falls_back_to_local(tmp_path, servers):
    down = servers(FakeClaude, status=529)
    local = servers(FakeOllama, [say("Respondo desde la Mac."), DONE])
    out = list(brain(tmp_path, down.url, local.url).answer(ask("¿Hola?"), "Sofi", FakeCamera(), llm="claude"))
    assert out == ["Respondo desde la Mac."]
    assert len(local.bodies) == 1


def test_missing_key_falls_back_to_local(tmp_path, servers):
    unused = servers(FakeClaude)
    local = servers(FakeOllama, [say("Sin llave, pero aquí estoy."), DONE])
    out = list(brain(tmp_path, unused.url, local.url, key=None).answer(ask("¿Hola?"), "Sofi", FakeCamera(),
                                                                         llm="claude"))
    assert out == ["Sin llave, pero aquí estoy."] and unused.bodies == []


def test_local_rover_never_calls_claude(tmp_path, servers):
    cloud = servers(FakeClaude)
    local = servers(FakeOllama, [say("Guau."), DONE])
    assert list(brain(tmp_path, cloud.url, local.url).answer(ask("¿Hola?"), "Bentley", FakeCamera())) == ["Guau."]
    assert cloud.bodies == []
