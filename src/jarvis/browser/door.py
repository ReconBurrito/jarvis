"""The door to the browser on the desktop.

The desktop's Chromium has its DevTools port on its own loopback only. A small TLS door there (desktop container,
port 9223) opens it to the network for one client: one holding a certificate signed by the brain's own authority
for client use, which only this brain has. The desktop's firewall also lets only the brain's address reach the door.
DevTools has no login of its own and gives whoever reaches it the whole browser, so all of that matters.

This side pins the door's own certificate, which the installer on the Proxmox node hands over from the desktop, and
presents the brain's client certificate. Nothing here is secret beyond the client key, which the brain's installer
made and which never leaves the brain (like the key of the brain's own HTTPS service).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
from pathlib import Path
from typing import Any

import websockets

DOOR_NAME = "jarvis-door.invalid"   # the one name in the door's certificate; no machine can have it
TIMEOUT = 10
MAX_ANSWER = 4 * 1024 * 1024        # a /json answer is small; a page's text is read over the websocket
MAX_MESSAGE = 64 * 1024 * 1024      # one DevTools message (a screenshot can be large)


class DoorError(Exception):
    """Why the door could not be used, in words for the owner. Never holds a key."""


def read_address(path: Path) -> tuple[str, int]:
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise DoorError(f"{path} cannot be read") from None
    host, _, port = text.rpartition(":")
    try:
        ipaddress.IPv4Address(host)
        number = int(port)
    except ValueError:
        raise DoorError(f"{path} must hold the desktop's door as address:port, such as 192.0.2.21:9223") from None
    if not 0 < number < 65536:
        raise DoorError(f"{path} names no port")
    return host, number


class Door:
    def __init__(self, address: Path, pinned: Path, certificate: Path, key: Path):
        self.address_file, self.pinned, self.certificate, self.key = address, pinned, certificate, key
        self._context: ssl.SSLContext | None = None

    def ready(self) -> str:
        """"" when the door can be tried; otherwise what is missing, in words."""
        for path, what in ((self.address_file, "the desktop's door address (the installer on the node puts it there)"),
                           (self.pinned, "the desktop door's certificate (the installer on the node puts it there)"),
                           (self.certificate, "the brain's door certificate (the brain's installer makes it)"),
                           (self.key, "the brain's door key (the brain's installer makes it)")):
            if not path.is_file():
                return f"{path} is missing: {what}"
        try:
            read_address(self.address_file)
        except DoorError as exc:
            return str(exc)
        return ""

    def set_up(self) -> bool:
        """Whether a desktop has handed its door over at all (its address or its certificate is here)."""
        return self.address_file.is_file() or self.pinned.is_file()

    @property
    def where(self) -> tuple[str, int]:
        return read_address(self.address_file)

    def context(self) -> ssl.SSLContext:
        if self._context is None:
            try:
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cadata=self.pinned.read_text(encoding="ascii"))
                context.minimum_version = ssl.TLSVersion.TLSv1_3
                context.load_cert_chain(self.certificate, self.key)
            except (OSError, ValueError, ssl.SSLError) as exc:
                raise DoorError(f"the door's certificates did not load ({type(exc).__name__})") from None
            self._context = context
        return self._context

    async def get(self, path: str) -> Any:
        """One DevTools HTTP answer (/json/version, /json/list), as JSON."""
        host, port = self.where
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=self.context(), server_hostname=DOOR_NAME), TIMEOUT)
        except ssl.SSLCertVerificationError:
            raise DoorError("the desktop's door showed a certificate other than the one handed over; run the desktop's "
                            "installer on the node again") from None
        except ssl.SSLError as exc:
            raise DoorError(f"the desktop's door refused the connection ({exc.reason or type(exc).__name__})") from None
        except (OSError, asyncio.TimeoutError) as exc:
            raise DoorError(f"the desktop's door at {host}:{port} does not answer ({type(exc).__name__})") from None
        try:
            writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip() or b"0")
            if length > MAX_ANSWER:
                raise DoorError("the desktop's browser answered with more than can be read")
            body = await asyncio.wait_for(reader.readexactly(length), TIMEOUT) if length else b""
            data = head + body
        except ssl.SSLError as exc:
            raise DoorError(f"the desktop's door refused this brain's certificate ({exc.reason or type(exc).__name__}); "
                            "run the desktop's installer on the node again") from None
        except (ConnectionResetError, asyncio.IncompleteReadError):
            # Under TLS 1.3 the door judges this brain's certificate after the handshake: a refusal shows up here.
            raise DoorError("the desktop's door closed the connection: it refused this brain's certificate (run the "
                            "desktop's installer on the node again), or nothing listens behind it") from None
        except (OSError, asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError) as exc:
            raise DoorError(f"the desktop's door stopped answering ({type(exc).__name__})") from None
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, ssl.SSLError, asyncio.TimeoutError):
                pass
        head, _, body = data.partition(b"\r\n\r\n")
        if not head.startswith(b"HTTP/1.1 200"):
            first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")[:60]
            raise DoorError(f"the desktop's browser did not answer ({first or 'the door closed the connection'}); "
                            "is Jarvis's browser window open on the desktop?")
        if len(body) > MAX_ANSWER:
            raise DoorError("the desktop's browser answered with more than can be read")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DoorError("the desktop's browser answered with something that is not JSON") from None

    async def websocket(self, debugger_url: str) -> Any:
        """A DevTools websocket, through the door. The browser names the address on its own loopback; the path
        is what counts."""
        path = "/" + debugger_url.split("://", 1)[-1].split("/", 1)[-1]
        if not path.startswith("/devtools/"):
            raise DoorError("the desktop's browser named no DevTools address")
        host, port = self.where
        try:
            return await asyncio.wait_for(websockets.connect(
                f"wss://{host}:{port}{path}", ssl=self.context(), server_hostname=DOOR_NAME, max_size=MAX_MESSAGE,
                open_timeout=TIMEOUT, ping_interval=None, proxy=None), TIMEOUT + 2)
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException, ssl.SSLError) as exc:
            raise DoorError(f"the desktop's browser could not be reached for control ({type(exc).__name__})") from None
