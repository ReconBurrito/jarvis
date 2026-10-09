"""Stand-ins the core's tests share: a scripted Ollama, and an audit log in a scratch folder."""
import asyncio
import json

import httpx
import pytest

from jarvis.audit import AuditLog


def run(coro):
    return asyncio.run(coro)


def ndjson(*chunks):
    return "\n".join(json.dumps(c) for c in chunks) + "\n"


def text_chunks(*parts, tokens=10):
    chunks = [{"model": "qwen3:8b", "message": {"role": "assistant", "content": p}, "done": False} for p in parts]
    chunks.append(
        {
            "model": "qwen3:8b",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 20,
            "eval_count": tokens,
            "eval_duration": 250_000_000,
        }
    )
    return chunks


def tool_chunks(name, arguments):
    return [
        {
            "model": "qwen3:8b",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"index": 0, "name": name, "arguments": arguments}}],
            },
            "done": False,
        },
        {"model": "qwen3:8b", "message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 5, "eval_duration": 100_000_000},
    ]


class FakeOllama:
    """Scripted Ollama: each /api/chat call pops the next scripted reply."""

    def __init__(self, replies=(), ps=None, down=False, models=("qwen3:8b",)):
        self.replies = list(replies)
        self.requests = []
        self.warmed = []
        self.ps = ps if ps is not None else {"models": []}
        self.down = down
        self.models = list(models)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.0-test"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": name} for name in self.models]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json=self.ps)
        if request.url.path == "/api/generate":
            self.warmed.append(json.loads(request.content))
            return httpx.Response(200, json={"done": True})
        if request.url.path == "/api/chat":
            self.requests.append(json.loads(request.content))
            reply = self.replies.pop(0)
            if isinstance(reply, httpx.Response):
                return reply
            return httpx.Response(200, text=ndjson(*reply))
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")
