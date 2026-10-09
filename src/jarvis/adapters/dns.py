"""DNS health check for the PiHoles, with no credential at all.

Pi-hole has no read-only API role, so instead of holding a full-access
password Jarvis asks each resolver three ordinary DNS questions: an internet
name, a lab name (one the lab's own DNS answers, from the vault), and a name
that the block lists should refuse.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets as pysecrets
import struct
import time
from typing import Any

from ..tools import TIER_READ_ONLY, Tool

TYPE_A = 1
BLOCKED_ANSWERS = {"0.0.0.0"}


def build_query(name: str, query_id: int) -> bytes:
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    question = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        if not 0 < len(raw) < 64:
            raise ValueError("bad DNS label")
        question += bytes([len(raw)]) + raw
    return header + question + b"\x00" + struct.pack("!HH", TYPE_A, 1)


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        length = data[pos]
        if length & 0xC0 == 0xC0:
            return pos + 2
        if length == 0:
            return pos + 1
        pos += 1 + length


def parse_response(data: bytes, query_id: int) -> tuple[int, list[str]]:
    """Return (response code, list of A record addresses)."""
    rid, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", data[:12])
    if rid != query_id or not flags & 0x8000:
        raise ValueError("not the answer to this question")
    pos = 12
    for _ in range(qdcount):
        pos = _skip_name(data, pos) + 4
    addresses = []
    for _ in range(ancount):
        pos = _skip_name(data, pos)
        rtype, _, _, rdlength = struct.unpack("!HHIH", data[pos : pos + 10])
        pos += 10
        if rtype == TYPE_A and rdlength == 4:
            addresses.append(str(ipaddress.IPv4Address(data[pos : pos + 4])))
        pos += rdlength
    return flags & 0x000F, addresses


class _Query(asyncio.DatagramProtocol):
    def __init__(self, payload: bytes):
        self.payload = payload
        self.reply: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    def connection_made(self, transport):
        transport.sendto(self.payload)

    def datagram_received(self, data, addr):
        if not self.reply.done():
            self.reply.set_result(data)

    def error_received(self, exc):
        if not self.reply.done():
            self.reply.set_exception(exc)


async def ask(server: str, port: int, name: str, timeout: float = 2.0) -> dict[str, Any]:
    """One A-record question to one resolver. Never raises."""
    query_id = pysecrets.randbelow(65536)
    started = time.monotonic()
    transport = None
    try:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _Query(build_query(name, query_id)), remote_addr=(server, port)
        )
        data = await asyncio.wait_for(protocol.reply, timeout)
        rcode, addresses = parse_response(data, query_id)
        return {"answered": True, "rcode": rcode, "addresses": addresses, "ms": round((time.monotonic() - started) * 1000, 1)}
    except (asyncio.TimeoutError, OSError, ValueError, struct.error, IndexError) as exc:
        return {"answered": False, "error": type(exc).__name__}
    finally:
        if transport is not None:
            transport.close()


def parse_servers(spec: str) -> list[tuple[str, str]]:
    """'192.0.2.53=dns1,192.0.2.54=dns2' -> [(address, name), ...]"""
    servers = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        address, _, name = entry.partition("=")
        ipaddress.ip_address(address.strip())
        servers.append((address.strip(), name.strip() or address.strip()))
    return servers


def make_dns_tool(
    servers: list[tuple[str, str]],
    local_name: str,
    internet_name: str = "example.com",
    blocked_name: str = "doubleclick.net",
    port: int = 53,
    timeout: float = 2.0,
) -> Tool:
    async def check(address: str, name: str) -> dict[str, Any]:
        internet, local, blocked = await asyncio.gather(
            ask(address, port, internet_name, timeout),
            ask(address, port, local_name, timeout),
            ask(address, port, blocked_name, timeout),
        )
        answering = internet["answered"] or local["answered"] or blocked["answered"]
        row: dict[str, Any] = {"resolver": name, "address": address, "answering": answering}
        if not answering:
            return row
        # The blocked name is answered by the PiHole itself, so it measures the resolver, not the internet.
        row["resolver_response_ms"] = blocked.get("ms") or local.get("ms") or internet.get("ms")
        row["internet_lookup_ms"] = internet.get("ms")
        row["internet_names_resolve"] = bool(internet["answered"] and internet["rcode"] == 0 and internet["addresses"])
        row["lab_names_resolve"] = bool(local["answered"] and local["rcode"] == 0 and local["addresses"])
        row["blocking_ads"] = bool(
            blocked["answered"] and (blocked["rcode"] == 3 or set(blocked["addresses"]) <= BLOCKED_ANSWERS)
        )
        row["healthy"] = row["internet_names_resolve"] and row["lab_names_resolve"] and row["blocking_ads"]
        return row

    async def handler(_: dict[str, Any]) -> dict[str, Any]:
        rows = await asyncio.gather(*(check(address, name) for address, name in servers))
        return {
            "resolvers": list(rows),
            "all_healthy": all(r.get("healthy") for r in rows),
            "checked": {"internet_name": internet_name, "lab_name": local_name, "ad_name": blocked_name},
        }

    return Tool(
        name="dns_health",
        description=(
            "Read-only. Checks each PiHole DNS resolver by asking it three questions: does it answer, how fast it "
            "responds by itself (resolver_response_ms) and how long an internet lookup took (internet_lookup_ms), "
            "does it resolve internet names and lab names, and does it still block a known ad domain. Takes no arguments."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        tier=TIER_READ_ONLY,
        handler=handler,
    )
