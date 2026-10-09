"""Tool registry and dispatcher.

Every tool declares a tier. The dispatcher enforces it; the model does not. Only tier 0 (read-only) tools
run so far. Anything else is refused and the refusal is written to the audit log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .audit import AuditLog

TIER_READ_ONLY = 0
TIER_LOW_RISK = 1
TIER_HIGH_RISK = 2

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    tier: int
    handler: Callable[..., Awaitable[Any]]
    with_session: bool = False  # the handler is also given the session that called it ("user:session")
    private: tuple[str, ...] = ()  # arguments that hold text: audited as length and fingerprint, never in clear

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _for_audit(arguments: dict[str, Any], private: tuple[str, ...]) -> dict[str, Any]:
    """What the audit log keeps of a call's arguments."""
    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return value.encode("utf-8", "replace").decode("utf-8")  # a stray half character must not fail the turn
        if isinstance(value, dict):
            return {clean(k): clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    kept = {}
    for key, value in arguments.items():
        if key in private:
            text = value if isinstance(value, str) else json.dumps(clean(value), ensure_ascii=True, default=str)
            kept[clean(key)] = {"chars": len(text), "digest": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]}
        else:
            kept[clean(key)] = clean(value)
    return kept


class ToolRegistry:
    def __init__(
        self,
        audit: AuditLog,
        max_tier: int = TIER_READ_ONLY,
        redact: Callable[[str], str] | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._audit = audit
        self._max_tier = max_tier
        self._redact = redact

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def remove(self, prefix: str) -> None:
        """Takes away every tool whose name starts so (the tools of one system, when its credentials go)."""
        for name in [name for name in self._tools if name.startswith(prefix)]:
            del self._tools[name]

    def names(self) -> list[str]:
        return sorted(t.name for t in self._tools.values() if t.tier <= self._max_tier)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values() if t.tier <= self._max_tier]

    async def dispatch(self, session: str, name: str, arguments: Any, audit: bool = True) -> dict[str, Any]:
        """Run one tool call. Always returns a dict the model can read.

        audit=False is for a caller that writes its own record of what it did (Jarvis's own checks);
        refusals are always audited.
        """
        if not isinstance(arguments, dict):
            arguments = {}
        tool = self._tools.get(name)
        if tool is None:
            self._audit.append("tool_denied", {"session": session, "tool": name, "reason": "unknown tool"})
            return {"error": f"unknown tool: {name}"}
        if tool.tier > self._max_tier:
            self._audit.append(
                "tool_denied",
                {"session": session, "tool": name, "tier": tool.tier, "reason": "tier not enabled"},
            )
            return {"error": f"{name} needs approval and the approval gate is not built yet"}
        started = time.monotonic()
        try:
            result = await (tool.handler(arguments, session) if tool.with_session else tool.handler(arguments))
            ok = True
        except Exception as exc:  # a failing tool must not take the turn down
            result = {"error": f"{type(exc).__name__}: {exc}"}
            ok = False
        if audit:
            self._audit.append(
                "tool_call",
                {
                    "session": session,
                    "tool": name,
                    "tier": tool.tier,
                    "arguments": _for_audit(arguments, tool.private),
                    "ok": ok,
                    "ms": round((time.monotonic() - started) * 1000),
                },
            )
        if not isinstance(result, dict):
            result = {"result": result}
        if self._redact:
            # Nothing a tool returns may carry a secret on to the model: every text in it, keys included, is
            # masked as it is (not as JSON would spell it, where a quote or a line end in a value looks different).
            result = _masked(result, self._redact)
        return result


def _masked(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {(redact(k) if isinstance(k, str) else k): _masked(v, redact) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_masked(v, redact) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


DRI = Path("/dev/dri")  # where a GPU's render device shows up; a container has one only when it was given one


async def _gpu_status() -> dict[str, Any]:
    """What this machine can say about its GPU. Memory and temperature come from nvidia-smi where that exists;
    for any other GPU only its presence is known here, and Ollama reports how much of a model sits on it."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        present = DRI.is_dir() and any(DRI.glob("renderD*"))
        return {"available": present, "details": "not reported on this machine"} if present else {"available": False}
    proc = await asyncio.create_subprocess_exec(
        exe,
        "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        return {"available": False, "error": "nvidia-smi timed out"}
    if proc.returncode != 0:
        return {"available": False}
    try:
        parts = [p.strip() for p in out.decode().strip().splitlines()[0].split(",")]
        return {
            "available": True,
            "name": parts[0],
            "memory_used_mib": int(parts[1]),
            "memory_total_mib": int(parts[2]),
            "utilization_percent": int(parts[3]),
            "temperature_c": int(parts[4]),
        }
    except (IndexError, ValueError):  # a card that reports "[N/A]" for one of them
        return {"available": True, "details": "not reported on this machine"}


def make_local_status(ollama_url: str, client: httpx.AsyncClient | None = None) -> Tool:
    """Read-only status of the machine Jarvis runs on."""

    async def handler(_: dict[str, Any]) -> dict[str, Any]:
        own = client is None
        http = client or httpx.AsyncClient(timeout=5)
        try:
            try:
                resp = await http.get(f"{ollama_url}/api/ps")
                resp.raise_for_status()
                models = [
                    {
                        "name": m.get("name"),
                        "size_gb": round(m.get("size", 0) / 1e9, 1),
                        "on_gpu_percent": round(100 * m.get("size_vram", 0) / m["size"]) if m.get("size") else None,
                    }
                    for m in resp.json().get("models", [])
                ]
                ollama: dict[str, Any] = {"up": True, "loaded_models": models}
            except (httpx.HTTPError, ValueError) as exc:
                ollama = {"up": False, "error": type(exc).__name__}
        finally:
            if own:
                await http.aclose()
        load1, load5, load15 = os.getloadavg()
        with open("/proc/uptime", encoding="ascii") as fh:
            uptime_s = int(float(fh.read().split()[0]))
        return {
            "host": os.uname().nodename,
            "uptime_hours": round(uptime_s / 3600, 1),
            "load_1m": round(load1, 2),
            "cpu_count": os.cpu_count(),
            "gpu": await _gpu_status(),
            "ollama": ollama,
        }

    return Tool(
        name="local_status",
        description=(
            "Read-only status of the computer Jarvis runs on: uptime, load, whether it has a GPU (with its memory "
            "and temperature where the machine reports them), and which local models are loaded and how much of "
            "each sits on the GPU. Takes no arguments."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        tier=TIER_READ_ONLY,
        handler=handler,
    )


def to_tool_message(name: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_name": name, "content": json.dumps(result, ensure_ascii=False)}
