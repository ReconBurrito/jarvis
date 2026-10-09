"""The service: Jarvis as the panel on the desktop reaches it.

    jarvis serve        started by systemd (jarvis.service), as the user jarvis

One HTTPS port. It serves the panel itself (the pages in src/jarvis/web) and what the panel asks for: the
conversation, as a stream of events, and the state of things for its readings. There is one conversation,
the owner's, and one answer at a time.

Who gets in, in this order:
  1. Only the addresses named at install (JARVIS_PANEL_ALLOW; the desktop container). The installer's
     firewall lets nobody else reach the port, and the same list is applied here again.
  2. Only under this machine's own address as its name (the Host header), so a web page cannot reach the
     service through a name of its own that it points here.
  3. Only the panel's own pages. The desktop's browser also shows pages from the internet, and such a page
     can make that browser send a request here. Every browser says truthfully which page a request comes
     from (Sec-Fetch-Site, Origin); anything not from the panel's own pages is refused. The panel cannot be
     shown inside another page.
This machine itself may ask whether the service is up (/api/health) and nothing else.

What this does not stop: a program running as the desktop's user outside the browser can say what it likes
in those headers. The desktop's browser keeps pages in its sandbox so that there is no such program.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from .audit import digest
from .config import Settings
from .core import Jarvis
from .notes.editor import EditConflict, open_note, save_note, tree
from .notes.repo import BrainError

SURFACE = ("The owner types to you in your panel, which is docked beside a web browser on their desktop, and "
           "reads your answers there.")
SESSION = "panel"
MAX_QUESTION = 4000        # characters in one message from the panel
KEPT = 200                 # entries of the conversation kept for showing again after the panel was reloaded
PULSE_SECONDS = 15         # how long one reading of this machine is good for
VAULT_EVERY = 3            # seconds between two looks at whether the vault was unlocked or changed
DENIALS_EVERY = 10         # seconds between two audit records about refused requests

WEB = Path(__file__).with_name("web")
SHARED = Path(__file__).resolve().parents[2] / "desktop"   # the font and the icon the desktop's own pages use too
FILES = {
    "index.html": (WEB, "text/html; charset=utf-8"),
    "panel.css": (WEB, "text/css; charset=utf-8"),
    "panel.js": (WEB, "text/javascript; charset=utf-8"),
    "notes.html": (WEB, "text/html; charset=utf-8"),
    "notes.css": (WEB, "text/css; charset=utf-8"),
    "notes.js": (WEB, "text/javascript; charset=utf-8"),
    "markdown.js": (WEB, "text/javascript; charset=utf-8"),
    "Oxanium.ttf": (SHARED, "font/ttf"),
    "icon.png": (SHARED, "image/png"),
}
HEADERS = {
    # Everything the panel loads comes from this service; nothing may be put inside another page.
    "content-security-policy": ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
                                "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                "form-action 'none'"),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "no-store",
}


def sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _loopback(peer: str) -> bool:
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


class Guard:
    """Decides about every request before anything else sees it, and puts the same headers on every answer."""

    def __init__(self, app: Callable[..., Awaitable[None]], settings: Settings, audit: Any):
        self.app = app
        self.audit = audit
        self.own = settings.address
        self.host = f"{settings.address}:{settings.port}" if settings.address else ""
        self.origin = f"https://{self.host}" if self.host else ""
        self.allowed = [ipaddress.ip_network(item) for item in settings.panel_allow]
        self._last_note = 0.0
        self._unnoted = 0

    def judge(self, peer: str, method: str, path: str, headers: dict[str, str]) -> str | None:
        """Why this request is refused, or None when it may pass."""
        if _loopback(peer) or (self.own and peer == self.own):
            return None if method in ("GET", "HEAD") and path == "/api/health" else "from this machine only /api/health is answered"
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return "no address"
        if not any(address in network for network in self.allowed):
            return "this address may not talk to Jarvis"
        if not self.host:
            return "this service was not told its own address"
        if headers.get("host", "") != self.host:
            return "asked for under another name than this machine's address"
        site = headers.get("sec-fetch-site", "")
        if path == "/panel/ping":
            return None if method == "GET" else "not a way to ask for that"
        if path in ("/", "/panel") or path.startswith("/panel/"):
            if method not in ("GET", "HEAD"):
                return "not a way to ask for that"
            # The panel's own pieces, the address typed or opened by the desktop, or a page going to the panel
            # as a whole page. Never a piece of the panel fetched by, or shown inside, a page from elsewhere.
            whole_page = headers.get("sec-fetch-mode") == "navigate" and headers.get("sec-fetch-dest") == "document"
            return None if site in ("same-origin", "none") or whole_page else "not asked for by the panel"
        if site != "same-origin":
            return "not asked for by the panel"
        if method not in ("GET", "HEAD"):
            if headers.get("origin", "") != self.origin:
                return "not asked for by the panel"
            if headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return "not in the form the panel uses"
        return None

    def _note(self, peer: str, path: str, reason: str) -> None:
        """Refusals are audited, but a stream of them does not fill the log: one record every few seconds."""
        now = time.monotonic()
        if now - self._last_note < DENIALS_EVERY:
            self._unnoted += 1
            return
        data = {"client": peer, "path": path[:200], "reason": reason}
        if self._unnoted:
            data["more_since_last"] = self._unnoted
        self._last_note, self._unnoted = now, 0
        with contextlib.suppress(Exception):  # a log that cannot be written must not turn a refusal into an answer
            self.audit.append("request_denied", data)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return  # nothing else is spoken here
        peer = (scope.get("client") or ("", 0))[0]
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        reason = self.judge(peer, scope["method"], scope["path"], headers)

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                named = {key.lower() for key, _ in message.get("headers", [])}
                message = dict(message, headers=list(message.get("headers", [])) + [
                    (key.encode(), value.encode()) for key, value in HEADERS.items() if key.encode() not in named])
            await send(message)

        if reason is not None:
            self._note(peer, scope["path"], reason)
            await JSONResponse({"error": reason}, status_code=403)(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)


class Panel:
    """The one conversation, and what the panel shows around it."""

    def __init__(self, jarvis: Jarvis, settings: Settings):
        self.jarvis = jarvis
        self.settings = settings
        self.busy = False
        self.conversation: list[dict[str, Any]] = []
        self._pulse: tuple[float, dict[str, Any]] = (0.0, {})
        self._reading = asyncio.Lock()

    async def pulse(self) -> dict[str, Any]:
        """A few readings of this machine, from the same tool the model uses, taken at most every few seconds.
        Several windows asking at once share one reading."""
        async with self._reading:
            return await self._take_pulse()

    async def _take_pulse(self) -> dict[str, Any]:
        taken, reading = self._pulse
        if reading and time.monotonic() - taken < PULSE_SECONDS:
            return reading
        status = await self.jarvis.tools.dispatch(SESSION, "local_status", {}, audit=False)
        if "error" in status:
            reading = {"error": status["error"]}
        else:
            ollama = status.get("ollama") or {}
            want = self.settings.model if ":" in self.settings.model else self.settings.model + ":latest"
            loaded = next((m for m in ollama.get("loaded_models") or [] if m.get("name") == want), None)
            reading = {
                "host": status.get("host"),
                "uptime_hours": status.get("uptime_hours"),
                "load_1m": status.get("load_1m"),
                "cpu_count": status.get("cpu_count"),
                "gpu": bool((status.get("gpu") or {}).get("available")),
                "model_server_up": bool(ollama.get("up")),
                "model_loaded": loaded is not None,
                "model_on_gpu_percent": loaded.get("on_gpu_percent") if loaded else None,
            }
        # The cluster, from the same read-only tool the model uses, when Jarvis has it.
        if "proxmox_cluster_status" in self.jarvis.tools.names():
            cluster = await self.jarvis.tools.dispatch(SESSION, "proxmox_cluster_status", {}, audit=False)
            if "error" in cluster:
                reading["cluster"] = {"error": cluster["error"]}
            else:
                nodes = cluster.get("nodes") or []
                reading["cluster"] = {"nodes": len(nodes), "online": sum(1 for node in nodes if node.get("online")),
                                      "quorate": cluster.get("quorate")}
        self._pulse = (time.monotonic(), reading)
        return reading

    def state(self, pulse: dict[str, Any]) -> dict[str, Any]:
        return {
            "release": self.settings.release_now(),
            "model": self.settings.model if self.settings.has_model else None,
            "busy": self.busy,
            "conversation": self.conversation,
            "waiting": [],   # changes that wait for the owner's yes or no; nothing can ask for one yet
            "pulse": pulse,
            "vault": {"status": self.jarvis.vault.status, "detail": self.jarvis.vault.detail},
            "lab": {system: why or "ready" for system, why in self.jarvis.lab.items()},
        }

    async def turn(self, text: str) -> AsyncIterator[str]:
        """One turn as the panel receives it. Whatever ends it, the conversation shown and Jarvis's own memory
        of it are left in step: an answer that was stopped is marked so, and the router forgets that turn."""
        if self.busy:  # two questions sent at the same moment: the second is told, not queued
            yield sse({"type": "error", "message": "Jarvis is still answering."})
            return
        self.busy = True
        reply: dict[str, Any] = {"role": "jarvis", "text": "", "state": "answering"}
        self.conversation += [{"role": "you", "text": text}, reply]
        try:
            async with contextlib.aclosing(self.jarvis.router.turn(SESSION, text, user="owner")) as events:
                async for event in events:
                    if event["type"] == "token":
                        reply["text"] += event["text"]
                    elif event["type"] == "error":
                        reply["state"], reply["note"] = "failed", event["message"]
                    elif event["type"] == "done":
                        reply["state"] = "done"
                    yield sse(event)
        except (asyncio.CancelledError, GeneratorExit):
            raise  # the panel went away or pressed stop; marked below
        except Exception as exc:  # the audit log cannot be written, say: told to the panel, not left as silence
            reply["state"], reply["note"] = "failed", f"{type(exc).__name__}: {exc}"
            yield sse({"type": "error", "message": reply["note"]})
        finally:
            if reply["state"] == "answering":
                reply["state"] = "stopped"
            del self.conversation[:-KEPT]
            self.busy = False

    def new(self) -> None:
        self.jarvis.router.forget(SESSION)
        self.conversation.clear()


def create_app(jarvis: Jarvis, settings: Settings) -> Guard:
    panel = Panel(jarvis, settings)

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "release": settings.release_now()})

    async def ping(request: Request) -> Response:
        # The desktop's own start page asks this to learn that the brain is there, and may read nothing but that.
        return Response(status_code=204, headers={"cross-origin-resource-policy": "cross-origin"})

    async def home(request: Request) -> Response:
        return RedirectResponse("/panel/", status_code=308)

    async def page(request: Request) -> Response:
        name = request.path_params.get("name") or "index.html"
        if name not in FILES:
            return JSONResponse({"error": "there is no such page"}, status_code=404)
        folder, kind = FILES[name]
        try:
            body = (folder / name).read_bytes()
        except OSError:
            return JSONResponse({"error": f"{name} is missing from this installation"}, status_code=500)
        return Response(body, media_type=kind)

    async def state(request: Request) -> Response:
        return JSONResponse(panel.state(await panel.pulse()))

    async def chat(request: Request) -> Response:
        try:
            body = await request.json()
            text = body["text"].strip() if isinstance(body.get("text"), str) else ""
        except (ValueError, AttributeError, KeyError, UnicodeDecodeError):
            return JSONResponse({"error": "that is not a message the panel sends"}, status_code=400)
        if not text or len(text) > MAX_QUESTION:
            return JSONResponse({"error": f"a message is between 1 and {MAX_QUESTION} characters long"}, status_code=400)
        if jarvis.backend is None:
            return JSONResponse({"error": "no local model is set on this machine, so there is nothing to answer with"}, status_code=503)
        if panel.busy:
            return JSONResponse({"error": "Jarvis is still answering."}, status_code=409)
        return StreamingResponse(panel.turn(text), media_type="text/event-stream")

    async def new(request: Request) -> Response:
        if panel.busy:
            return JSONResponse({"error": "Jarvis is still answering."}, status_code=409)
        panel.new()
        return JSONResponse({"ok": True})

    def notes_or_none() -> Any:
        repo = jarvis.notes
        return repo if repo is not None and jarvis.vault.status == "unlocked" else None

    async def notes_tree(request: Request) -> Response:
        repo = notes_or_none()
        if repo is None:
            return JSONResponse({"error": "Jarvis has no notes yet: " + (jarvis.lab.get("notes") or "they are not in the vault")}, status_code=503)
        if not repo.ready:
            await repo.start()
        if not repo.ready:
            return JSONResponse({"error": f"the notes are not available: {repo.detail}"}, status_code=503)
        return JSONResponse(tree(repo))

    async def notes_open(request: Request) -> Response:
        repo = notes_or_none()
        if repo is None or not repo.ready:
            return JSONResponse({"error": "the notes are not available"}, status_code=503)
        try:
            return JSONResponse(open_note(repo, request.query_params.get("path", "")))
        except (BrainError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def notes_save(request: Request) -> Response:
        repo = notes_or_none()
        if repo is None:
            return JSONResponse({"error": "the notes are not available"}, status_code=503)
        try:
            body = await request.json()
            path, text, base = body["path"], body["text"], body.get("base", "")
            if not all(isinstance(value, str) for value in (path, text, base)):
                raise TypeError
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
            return JSONResponse({"error": "that is not a note the window sends"}, status_code=400)
        record = {"path": path[:200], "by": "owner", "chars": len(text), "digest": digest(text)}
        try:
            saved = await save_note(repo, path, text, base)
        except EditConflict as exc:
            jarvis.audit.append("note_save_failed", {**record, "reason": "changed meanwhile"})
            return JSONResponse({"error": str(exc), "conflict": True}, status_code=409)
        except BrainError as exc:
            with contextlib.suppress(Exception):
                jarvis.audit.append("note_save_failed", {**record, "reason": str(exc)[:200]})
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:   # said without the paths of this machine
            with contextlib.suppress(Exception):
                jarvis.audit.append("note_save_failed", {**record, "reason": exc.strerror or type(exc).__name__})
            return JSONResponse({"error": "nothing was saved: " + (exc.strerror or "the file could not be written")}, status_code=400)
        try:
            jarvis.audit.append("note_saved", {**record, "commit": saved["commit"], "protected": saved["protected"]})
        except Exception:
            # The note is saved and sent; saying otherwise would make the owner save it again.
            saved["warning"] = "The audit log could not be written; see jarvis doctor."
        return JSONResponse(saved)

    async def watch_vault() -> None:
        """An unlock (or a new vault file) is noticed within seconds; the conversation is not lost to a restart."""
        while True:
            await asyncio.sleep(VAULT_EVERY)
            with contextlib.suppress(Exception):
                # Reading the files (sops may take a moment) happens beside the service, not in its way.
                if await asyncio.to_thread(jarvis.vault.read):
                    while panel.busy:  # nothing changes under an answer in progress, not even what is masked
                        await asyncio.sleep(0.5)
                    jarvis.vault.commit()
                    jarvis.apply_vault()
                    panel._pulse = (0.0, {})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        jarvis.audit.append("startup", {"release": settings.release_now(), "listen": f"{settings.listen}:{settings.port}",
                                        "panel_allow": list(settings.panel_allow), "model": settings.model})
        # The model is loaded now, so the first question does not wait for it.
        warm = asyncio.create_task(jarvis.backend.warm()) if jarvis.backend is not None else None
        watch = asyncio.create_task(watch_vault())
        try:
            yield
        finally:
            watch.cancel()
            if warm is not None:
                warm.cancel()
            with contextlib.suppress(Exception):
                jarvis.audit.append("shutdown", {})
            await jarvis.close()

    app = Starlette(lifespan=lifespan, routes=[
        Route("/api/health", health, methods=["GET"]),
        Route("/api/state", state, methods=["GET"]),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/new", new, methods=["POST"]),
        Route("/api/notes", notes_tree, methods=["GET"]),
        Route("/api/notes/note", notes_open, methods=["GET"]),
        Route("/api/notes/note", notes_save, methods=["PUT"]),
        Route("/panel/ping", ping, methods=["GET"]),
        Route("/panel/", page, methods=["GET"]),
        Route("/panel/{name}", page, methods=["GET"]),
        Route("/panel", home, methods=["GET"]),
        Route("/", home, methods=["GET"]),
    ])
    guarded = Guard(app, settings, jarvis.audit)
    guarded.panel = panel  # for whoever holds the app and wants to look (the tests)
    return guarded


def serve(settings: Settings) -> int:
    import uvicorn

    cert, key = settings.tls_dir / "server.pem", settings.tls_dir / "server.key"
    for needed in (cert, key):
        if not needed.is_file():
            print(f"jarvis: {needed} is not there. The installer makes it; as root, run: update --repair")
            return 1
    if not settings.address:
        print(f"jarvis: this machine's own address is not in {settings.site} (JARVIS_ADDRESS). The installer writes it; "
              "as root, run: update --repair")
        return 1
    if not settings.panel_allow:
        print("jarvis: nobody is named who may reach the panel (JARVIS_PANEL_ALLOW is empty); only this machine's "
              "own check will be answered.")
    app = create_app(Jarvis(settings, surface=SURFACE, record_vault=True), settings)
    config = uvicorn.Config(app, host=settings.listen, port=settings.port, ssl_certfile=str(cert), ssl_keyfile=str(key),
                            log_level="warning", access_log=False, server_header=False, timeout_graceful_shutdown=3,
                            # Who is asking is the address the connection really comes from. Nothing stands in
                            # front of this service, so a header that names another address is never believed.
                            proxy_headers=False)
    uvicorn.Server(config).run()
    return 0
