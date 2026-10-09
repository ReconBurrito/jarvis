import asyncio
import ipaddress
import struct

import pytest
from fakes import run

from jarvis.adapters.dns import ask, build_query, make_dns_tool, parse_response, parse_servers

RECORDS = {
    "example.com": ["93.184.216.34"],
    "jarvis.lab.example": ["192.0.2.70"],
    "doubleclick.net": ["0.0.0.0"],
}


def question_name(data):
    labels, pos = [], 12
    while data[pos]:
        labels.append(data[pos + 1 : pos + 1 + data[pos]].decode())
        pos += 1 + data[pos]
    return ".".join(labels), pos + 5


class FakeResolver(asyncio.DatagramProtocol):
    """A tiny real DNS server on a UDP socket."""

    def __init__(self, records, silent=False, nxdomain=()):
        self.records, self.silent, self.nxdomain = records, silent, set(nxdomain)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if self.silent:
            return
        name, end = question_name(data)
        answers = [] if name in self.nxdomain else self.records.get(name, [])
        rcode = 3 if name in self.nxdomain else 0
        header = data[:2] + struct.pack("!HHHHH", 0x8180 | rcode, 1, len(answers), 0, 0)
        body = data[12:end]
        for address in answers:
            # 0xC00C points back at the question name: real servers compress like this.
            body += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + ipaddress.IPv4Address(address).packed
        self.transport.sendto(header + body, addr)


async def start(records=RECORDS, **kwargs):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: FakeResolver(records, **kwargs), local_addr=("127.0.0.1", 0))
    return transport, transport.get_extra_info("sockname")[1]


def test_query_and_response_round_trip():
    packet = build_query("example.com", 0x1234)
    assert packet[:2] == b"\x12\x34" and packet[12:] == b"\x07example\x03com\x00\x00\x01\x00\x01"
    reply = packet[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0) + packet[12:] + b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([1, 2, 3, 4])
    assert parse_response(reply, 0x1234) == (0, ["1.2.3.4"])
    with pytest.raises(ValueError):
        parse_response(reply, 0x9999)
    with pytest.raises(ValueError):
        build_query("bad..name", 1)


def test_healthy_resolver_over_real_udp():
    async def go():
        transport, port = await start()
        try:
            tool = make_dns_tool([("127.0.0.1", "dns1")], "jarvis.lab.example", port=port)
            return await tool.handler({})
        finally:
            transport.close()

    result = run(go())
    row = result["resolvers"][0]
    assert row["resolver"] == "dns1" and row["answering"] is True and row["healthy"] is True
    assert row["internet_names_resolve"] and row["lab_names_resolve"] and row["blocking_ads"]
    assert 0 <= row["resolver_response_ms"] < 500 and 0 <= row["internet_lookup_ms"] < 500
    assert "latency_ms" not in row and result["all_healthy"] is True


def test_resolver_that_stopped_blocking_or_lost_lab_names():
    records = {"example.com": ["93.184.216.34"], "doubleclick.net": ["142.250.1.1"]}

    async def go():
        transport, port = await start(records)
        try:
            return await make_dns_tool([("127.0.0.1", "dns2")], "jarvis.lab.example", port=port).handler({})
        finally:
            transport.close()

    result = run(go())
    row = result["resolvers"][0]
    assert row["answering"] and row["internet_names_resolve"]
    assert row["blocking_ads"] is False and row["lab_names_resolve"] is False
    assert row["healthy"] is False and result["all_healthy"] is False


def test_nxdomain_counts_as_blocked():
    async def go():
        transport, port = await start(nxdomain={"doubleclick.net"})
        try:
            return await make_dns_tool([("127.0.0.1", "dns1")], "jarvis.lab.example", port=port).handler({})
        finally:
            transport.close()

    assert run(go())["resolvers"][0]["blocking_ads"] is True


def test_silent_resolver_is_reported_down_quickly():
    async def go():
        up, up_port = await start()
        down, down_port = await start(silent=True)
        try:
            dead = await ask("127.0.0.1", down_port, "example.com", timeout=0.2)
            tool = make_dns_tool([("127.0.0.1", "dns1")], "jarvis.lab.example", port=down_port, timeout=0.2)
            return dead, await tool.handler({})
        finally:
            up.close()
            down.close()

    dead, result = run(go())
    assert dead == {"answered": False, "error": "TimeoutError"}
    assert result["resolvers"][0] == {"resolver": "dns1", "address": "127.0.0.1", "answering": False}
    assert result["all_healthy"] is False


def test_server_list_parsing():
    assert parse_servers("192.0.2.53=dns1, 192.0.2.54") == [("192.0.2.53", "dns1"), ("192.0.2.54", "192.0.2.54")]
    with pytest.raises(ValueError):
        parse_servers("dns1.example=dns1")
