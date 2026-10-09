import httpx
import pytest
from fakes import FakeOllama, run, text_chunks, tool_chunks

from jarvis.llm import BackendDown, OllamaBackend


async def collect(backend, messages, tools=()):
    return [event async for event in backend.chat(messages, list(tools))]


def test_text_stream_and_request_shape():
    fake = FakeOllama([text_chunks("Good ", "morning.", tokens=10)])
    backend = OllamaBackend("http://ollama", "qwen3:8b", fake.client())
    events = run(collect(backend, [{"role": "user", "content": "hi"}]))
    assert [e["text"] for e in events if e["type"] == "text"] == ["Good ", "morning."]
    assert events[-1] == {"type": "done", "prompt_tokens": 20, "output_tokens": 10, "tokens_per_second": 40.0}
    sent = fake.requests[0]
    assert sent["think"] is False and sent["stream"] is True and sent["model"] == "qwen3:8b"
    assert "tools" not in sent


def test_tool_call_is_parsed():
    fake = FakeOllama([tool_chunks("local_status", {})])
    backend = OllamaBackend("http://ollama", "qwen3:8b", fake.client())
    tools = [{"type": "function", "function": {"name": "local_status"}}]
    events = run(collect(backend, [{"role": "user", "content": "status"}], tools))
    calls = [e for e in events if e["type"] == "tool_calls"][0]
    assert calls["calls"] == [{"name": "local_status", "arguments": {}}]
    assert fake.requests[0]["tools"] == tools


def test_unreachable_raises_backend_down():
    backend = OllamaBackend("http://ollama", "qwen3:8b", FakeOllama(down=True).client())
    with pytest.raises(BackendDown):
        run(collect(backend, [{"role": "user", "content": "hi"}]))
    assert run(backend.healthy()) is False


def test_error_status_raises_backend_down():
    fake = FakeOllama([httpx.Response(404, json={"error": "model 'qwen3:8b' not found"})])
    backend = OllamaBackend("http://ollama", "qwen3:8b", fake.client())
    with pytest.raises(BackendDown, match="404"):
        run(collect(backend, [{"role": "user", "content": "hi"}]))


def test_healthy():
    assert run(OllamaBackend("http://ollama", "qwen3:8b", FakeOllama().client()).healthy()) is True
