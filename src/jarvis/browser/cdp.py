"""A DevTools connection to the desktop's browser, and the guard that rides along while Jarvis has hold of a tab.

Jarvis takes hold of a tab only for the moment it works in it (to open a page, or to read one), and lets go after.
While it holds a tab, every request that tab makes is shown to Jarvis first (DevTools' Fetch domain) and goes on
only when it is for a public address on the internet. The brain itself, the rest of the lab and the desktop's own
ports are never reached by a page Jarvis drives: a page could otherwise be made to speak to them with the
desktop's address, which the brain trusts. Requests that do not use the network (data:, blob:, about:) pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .door import Door, DoorError

CALL_TIMEOUT = 20


class CdpError(Exception):
    """A DevTools call that failed, in words."""


def public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


class Resolver:
    """Whether a host is on the internet: every address its name stands for must be public. Asked once per host
    for as long as Jarvis holds a tab. A page that asks for very many names gets no more lookups: the rest are
    refused, so a page cannot keep the brain busy looking names up for it."""

    MAX_HOSTS = 64
    AT_ONCE = 4

    def __init__(self, lookup: Callable[[str], Awaitable[list[str]]] | None = None, max_hosts: int = MAX_HOSTS):
        self._lookup = lookup or self._dns
        self._known: dict[str, bool] = {}
        self._gate = asyncio.Semaphore(self.AT_ONCE)
        self.max_hosts = max_hosts
        self.capped = False   # a host was refused only because the page asked for too many

    @staticmethod
    async def _dns(host: str) -> list[str]:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), 5)
        return [info[4][0] for info in infos]

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme in ("data", "blob", "about"):
            return True
        if parts.scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
            return False
        host = parts.hostname.strip("[]").lower()
        if host not in self._known:
            if len(self._known) >= self.max_hosts:
                self.capped = True
                return False
            try:
                ipaddress.ip_address(host)
                addresses = [host]
            except ValueError:
                async with self._gate:
                    try:
                        addresses = await self._lookup(host)
                    except (OSError, asyncio.TimeoutError, UnicodeError):
                        addresses = []
            # Jarvis's browser reaches the internet over IPv4 only (the desktop's firewall refuses it IPv6, where a
            # lab's own machines have global addresses too): a name must have a public IPv4 address, and no
            # address of it may be private.
            addresses = [a.split("%")[0] for a in addresses]
            four = [a for a in addresses if ":" not in a]
            self._known[host] = bool(four) and all(public(a) for a in addresses)
        return self._known[host]


class Cdp:
    """One browser-wide DevTools connection: calls by number, events to whoever waits for them."""

    def __init__(self, socket_: Any):
        self._ws = socket_
        self._next = 0
        self._waiting: dict[int, asyncio.Future] = {}
        self._handlers: list[Callable[[dict[str, Any]], Awaitable[None] | None]] = []
        self._reader = asyncio.get_running_loop().create_task(self._read())
        self.closed = False

    @classmethod
    async def open(cls, door: Door) -> "Cdp":
        version = await door.get("/json/version")
        url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        if not isinstance(url, str):
            raise DoorError("the desktop's browser named no DevTools address")
        return cls(await door.websocket(url))

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if "id" in message:
                    waiter = self._waiting.pop(message["id"], None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(message)
                    continue
                for handler in list(self._handlers):
                    with contextlib.suppress(Exception):
                        outcome = handler(message)
                        if asyncio.iscoroutine(outcome):
                            asyncio.get_running_loop().create_task(outcome)
        except Exception:
            pass
        finally:
            self.closed = True
            for waiter in self._waiting.values():
                if not waiter.done():
                    waiter.set_exception(CdpError("the connection to the desktop's browser was lost"))
            self._waiting.clear()

    def on(self, handler: Callable[[dict[str, Any]], Awaitable[None] | None]) -> Callable[[], None]:
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler) if handler in self._handlers else None

    async def call(self, method: str, params: dict[str, Any] | None = None, session: str | None = None,
                   timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        if self.closed:
            raise CdpError("the connection to the desktop's browser was lost")
        self._next += 1
        number = self._next
        message: dict[str, Any] = {"id": number, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        waiter = asyncio.get_running_loop().create_future()
        self._waiting[number] = waiter
        try:
            await self._ws.send(json.dumps(message))
            answer = await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            raise CdpError(f"the desktop's browser did not answer {method} in time") from None
        except Exception as exc:
            if isinstance(exc, CdpError):
                raise
            raise CdpError(f"the desktop's browser could not be asked ({type(exc).__name__})") from None
        finally:
            self._waiting.pop(number, None)
        if "error" in answer:
            raise CdpError(f"{method}: {str(answer['error'].get('message', 'refused'))[:200]}")
        return answer.get("result", {})

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._ws.close(), 2)
        self.closed = True


class Hold:
    """Jarvis's hold on one tab: attached, every request guarded, let go of on leaving the block.

    Frames from other sites and workers run in targets of their own; each is attached as it starts (held until
    then) and guarded the same way. On leaving, every request still waiting for an answer is refused before the
    guard is lifted, since lifting it lets whatever still waits through. (The desktop's firewall keeps Jarvis's
    browser off private addresses in any case; this guard is the second line, and it names what it stopped.)"""

    MAX_REFUSED = 50

    def __init__(self, cdp: Cdp, target: str, resolver: Resolver):
        self.cdp, self.target, self.resolver = cdp, target, resolver
        self.session = ""
        self.sessions: set[str] = set()
        self.refused: list[str] = []   # hosts a request went to and was stopped
        self._off: Callable[[], None] | None = None
        self._pending: dict[tuple[str, str], asyncio.Task] = {}   # (session, request): the task deciding it
        self._closing = False
        self._late: list[asyncio.Task] = []   # refusals sent while letting go

    async def __aenter__(self) -> "Hold":
        attached = await self.cdp.call("Target.attachToTarget", {"targetId": self.target, "flatten": True})
        self.session = attached["sessionId"]
        self.sessions.add(self.session)
        self._off = self.cdp.on(self._event)
        try:
            await self._guard(self.session)
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def _guard(self, session: str) -> None:
        await self.cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session)
        await self.cdp.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, session)

    def _event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "Fetch.requestPaused" and message.get("sessionId") in self.sessions:
            key = (message["sessionId"], str(message.get("params", {}).get("requestId")))
            if self._closing:   # letting go: refused at once, not looked up
                self._late.append(asyncio.get_running_loop().create_task(self._fail(key)))
                return
            task = asyncio.get_running_loop().create_task(self._paused(key, message.get("params", {})))
            self._pending[key] = task
            task.add_done_callback(lambda _, key=key: self._pending.pop(key, None))
        elif method == "Target.attachedToTarget" and message.get("sessionId") in self.sessions:
            child = message.get("params", {}).get("sessionId")
            if child:
                self.sessions.add(child)
                task = asyncio.get_running_loop().create_task(self._child(child))
                self._pending[(child, "")] = task
                task.add_done_callback(lambda _, child=child: self._pending.pop((child, ""), None))

    async def _child(self, session: str) -> None:
        """A frame or worker of the tab, held at its start: guarded where it has requests of its own (a
        dedicated worker's go through its parent), then let run."""
        if self._closing:
            return
        with contextlib.suppress(CdpError):
            await self.cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session, timeout=5)
        with contextlib.suppress(CdpError):
            await self.cdp.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                                session, timeout=5)
        with contextlib.suppress(CdpError):
            await self.cdp.call("Runtime.runIfWaitingForDebugger", {}, session, timeout=5)

    async def _fail(self, key: tuple[str, str]) -> None:
        with contextlib.suppress(CdpError):
            await self.cdp.call("Fetch.failRequest", {"requestId": key[1], "errorReason": "BlockedByClient"}, key[0], timeout=5)

    def _refuse(self, url: str) -> None:
        host = urlsplit(url).hostname or url[:40]
        if host not in self.refused and len(self.refused) < self.MAX_REFUSED:
            self.refused.append(host)

    async def _paused(self, key: tuple[str, str], params: dict[str, Any]) -> None:
        session, request = key
        url = str(params.get("request", {}).get("url", ""))
        with contextlib.suppress(CdpError):
            if not self._closing and await self.resolver.allowed(url):
                await self.cdp.call("Fetch.continueRequest", {"requestId": request}, session)
            else:
                self._refuse(url)
                await self.cdp.call("Fetch.failRequest", {"requestId": request, "errorReason": "BlockedByClient"}, session)

    async def __aexit__(self, *exc: Any) -> None:
        self._closing = True
        # What still waits is refused, not let through by the guard being lifted; what arrives while letting go
        # is refused at once (see _event), until the tab is let go of.
        waiting = list(self._pending.items())
        for _, task in waiting:
            task.cancel()
        await asyncio.gather(*(task for _, task in waiting), return_exceptions=True)
        if not self.cdp.closed:
            for key, _ in waiting:
                if key[1]:
                    await self._fail(key)
        try:
            if self.session and not self.cdp.closed:
                with contextlib.suppress(CdpError):
                    await self.cdp.call("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False},
                                        self.session, timeout=5)
                await asyncio.gather(*self._late, return_exceptions=True)
                for session in sorted(self.sessions - {self.session}):
                    with contextlib.suppress(CdpError):
                        await self.cdp.call("Fetch.disable", {}, session, timeout=5)
                with contextlib.suppress(CdpError):
                    await self.cdp.call("Fetch.disable", {}, self.session, timeout=5)
                with contextlib.suppress(CdpError):
                    await self.cdp.call("Target.detachFromTarget", {"sessionId": self.session}, timeout=5)
        finally:
            if self._off is not None:
                self._off()

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        return await self.cdp.call(method, params, self.session, timeout)
