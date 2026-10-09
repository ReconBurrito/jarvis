"""Model backends. For now the local one: a model served by Ollama on this machine."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Protocol

import httpx


class BackendDown(Exception):
    """The backend could not be reached or refused the request before any output."""


class Backend(Protocol):
    name: str
    model: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """Yield events: {"type": "text"}, {"type": "tool_calls"}, then {"type": "done"}."""
        ...

    async def healthy(self) -> bool: ...


KEEP_ALIVE = "30m"  # how long Ollama keeps the model in GPU memory after a request


class OllamaBackend:
    name = "local"

    def __init__(self, base_url: str, model: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        # No read timeout: a cold model load plus a long answer can take a while.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10, read=None))

    async def healthy(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/api/version", timeout=3)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def warm(self) -> bool:
        """Ask Ollama to load the model now, so the first question does not wait for it."""
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/generate", json={"model": self.model, "keep_alive": KEEP_ALIVE}, timeout=120
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], format: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": False,
            "keep_alive": KEEP_ALIVE,
        }
        if tools:
            payload["tools"] = tools
        if format:
            payload["format"] = format  # a JSON schema the reply must follow
        started = False
        try:
            async with self._client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise BackendDown(f"ollama answered {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise BackendDown(f"ollama error: {chunk['error']}")
                    started = True
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        yield {"type": "text", "text": msg["content"]}
                    if msg.get("tool_calls"):
                        calls = [
                            {
                                "name": c.get("function", {}).get("name", ""),
                                "arguments": c.get("function", {}).get("arguments") or {},
                            }
                            for c in msg["tool_calls"]
                        ]
                        yield {"type": "tool_calls", "calls": calls, "raw": msg["tool_calls"]}
                    if chunk.get("done"):
                        eval_ns = chunk.get("eval_duration") or 0
                        tokens = chunk.get("eval_count") or 0
                        yield {
                            "type": "done",
                            "prompt_tokens": chunk.get("prompt_eval_count") or 0,
                            "output_tokens": tokens,
                            "tokens_per_second": round(tokens / (eval_ns / 1e9), 1) if eval_ns else None,
                        }
                        return
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            if started:
                raise
            raise BackendDown(f"ollama unreachable: {type(exc).__name__}") from exc
