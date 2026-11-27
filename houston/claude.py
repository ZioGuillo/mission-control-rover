"""Claude (Anthropic Messages API) behind the brain's Ollama-shaped calls.

The brain builds Ollama /api/chat bodies and reads Ollama `message` dicts; `stream` takes the same
body, sends it to Claude and yields the same shapes ({"content": ...}, {"tool_calls": [...]}),
so the brain's logic (tools, stories, camera) is shared by both backends. stdlib only.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

API_VERSION = "2023-06-01"
_TIMEOUT_S = 60.0


class ClaudeError(Exception):
    pass


def key(path: Path) -> str:
    try:
        value = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ClaudeError(f"no Anthropic key at {path}: {e}") from e
    if not value:
        raise ClaudeError(f"empty Anthropic key file {path}")
    return value


def _tools(tools: list[dict]) -> list[dict]:
    return [{"name": t["function"]["name"], "description": t["function"]["description"],
             "input_schema": t["function"]["parameters"]} for t in tools]


def _blocks(msg: dict, pending_ids: list[str]) -> tuple[str, list[dict]]:
    """One Ollama message -> (Claude role, content blocks)."""
    role = msg.get("role")
    if role == "tool":
        tool_id = pending_ids.pop(0) if pending_ids else "missing"
        return "user", [{"type": "tool_result", "tool_use_id": tool_id, "content": msg.get("content") or ""}]
    blocks: list[dict] = []
    for image in msg.get("images") or []:
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}})
    if (msg.get("content") or "").strip():
        blocks.append({"type": "text", "text": msg["content"]})
    if role == "assistant":
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            pending_ids.append(call["id"])
            blocks.append({"type": "tool_use", "id": call["id"], "name": fn.get("name", ""),
                           "input": fn.get("arguments") or {}})
        return "assistant", blocks
    return "user", blocks


def request(body: dict, model: str, max_tokens: int) -> dict:
    """The Claude request for an Ollama /api/chat body."""
    system, messages, pending = [], [], []
    for msg in body.get("messages", []):
        if msg.get("role") == "system":
            system.append(msg.get("content") or "")
            continue
        role, blocks = _blocks(msg, pending)
        if not blocks:
            continue
        if messages and messages[-1]["role"] == role:  # Claude wants user/assistant turns alternating
            messages[-1]["content"] += blocks
        else:
            messages.append({"role": role, "content": blocks})
    if messages and messages[0]["role"] == "assistant":  # e.g. the rover spoke first (inbox)
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "(hola)"}]})
    out = {"model": model, "max_tokens": max_tokens, "messages": messages, "stream": True}
    if system:
        out["system"] = "\n\n".join(system)
    if body.get("tools"):
        out["tools"] = _tools(body["tools"])
    return out


def stream(body: dict, *, url: str, api_key: str, model: str, max_tokens: int,
           stop: threading.Event) -> Iterator[dict]:
    """Ollama-shaped messages from Claude's SSE stream: text as it comes, tool calls when complete."""
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/messages", data=json.dumps(request(body, model, max_tokens)).encode(),
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": API_VERSION})
    tools: dict[int, dict] = {}
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        for raw in resp:
            if stop.is_set():
                raise ClaudeError("answer abandoned")
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:])
            kind = event.get("type")
            if kind == "error":
                raise ClaudeError(f"claude: {event.get('error')}")
            if kind == "content_block_start" and event["content_block"].get("type") == "tool_use":
                block = event["content_block"]
                tools[event["index"]] = {"id": block["id"], "name": block["name"], "json": ""}
            elif kind == "content_block_delta":
                delta = event["delta"]
                if delta.get("type") == "text_delta":
                    yield {"content": delta.get("text", "")}
                elif delta.get("type") == "input_json_delta" and event["index"] in tools:
                    tools[event["index"]]["json"] += delta.get("partial_json", "")
            elif kind == "content_block_stop" and event.get("index") in tools:
                t = tools.pop(event["index"])
                yield {"tool_calls": [{"id": t["id"], "function": {
                    "name": t["name"], "arguments": json.loads(t["json"]) if t["json"] else {}}}]}
            elif kind == "message_stop":
                return
    raise ClaudeError("claude stream ended without message_stop")
