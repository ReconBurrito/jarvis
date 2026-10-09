import asyncio

import asyncssh
import pytest
from fakes import run

from jarvis.adapters.switch import (
    SwitchSession, make_switch_tool, parse_poe, parse_ports, parse_system, parse_vlans,
)

# Output in the shape TP-Link's command line prints it. Every value is invented; no real device was copied.
SYSTEM = """ System Description   - Example 12-Port Gigabit Switch
 System Name          - Lab Switch
 System Location      - Rack 1
 Contact Information  - www.example.com
 Hardware Version     - EX-12 1.0
 Software Version     - 1.0.0 Build 20000101 Rel.00001
 Bootloader Version   - BOOTUTIL(v1.0.0)
 Mac Address          - (not shown)
 Serial Number        - TESTSERIAL01
 System Time          - 2000-01-01 00:00:00
 Running Time         - 1 day - 2 hour - 3 min - 4 sec
"""
PORTS = """ Port      Status      Speed     Duplex    FlowCtrl    Active-Medium   LAG   Linkdown-Status Description
 ----      ------      -----     ------    --------    -------------   ---   ------------    -----------
 Gi1/0/1    LinkUp      1000M     Full      Disable     Copper          N/A   N/A
 Gi1/0/2    LinkUp      1000M     Full      Disable     Copper          N/A   N/A
 Gi1/0/3    LinkDown    N/A       N/A       N/A         Copper          N/A   N/A
 Gi1/0/4    LinkUp      1000M     Full      Disable     Copper          N/A   N/A
 Gi1/0/5    LinkUp      1000M     Full      Disable     Copper          N/A   N/A
 Gi1/0/6    LinkUp      1000M     Full      Disable     Copper          N/A   N/A             Printer
 Gi1/0/7    LinkUp      1000M     Full      Disable     Copper          N/A   N/A
 Gi1/0/8    LinkUp      1000M     Full      Disable     Copper          N/A   N/A             Camera
 Gi1/0/9    LinkUp      1000M     Full      Disable     Copper          N/A   N/A
 Gi1/0/10   LinkUp      1000M     Full      Disable     Copper          N/A   N/A
 Gi1/0/11   LinkDown    N/A       N/A       N/A         Copper          N/A   N/A
 Gi1/0/12   LinkDown    N/A       N/A       N/A         Copper          N/A   N/A
"""
VLANS = """UT: Untagged;     TG: Tagged
VLAN  Name                 Status    Ports
----- -------------------- --------- ----------------------------------------
1     System-VLAN          active    UT: Gi1/0/12
11    OFFICE               active    UT: Gi1/0/1, Gi1/0/2, Gi1/0/3, Gi1/0/4,
                                         Gi1/0/5
24    VLAN0024             active    UT: Gi1/0/7, Gi1/0/8
                                     TG: Gi1/0/9
45    PHONES               active    TG: Gi1/0/9, Gi1/0/10
52    GUESTS               active    UT: Gi1/0/11
                                     TG: Gi1/0/9, Gi1/0/10
77    LAB                  active    TG: Gi1/0/9
93    PRINTERS             active    UT: Gi1/0/6
"""
POE = """
 System Power Limit: 370.0w
 System Power Consumption: 42.5w
 System Power Remain: 327.5w
"""
OUTPUTS = {"show system-info": SYSTEM, "show interface status": PORTS, "show vlan brief": VLANS, "show power inline": POE}
PASSWORD = "view-only-test-password"


def test_parse_system():
    assert parse_system(SYSTEM) == {
        "name": "Lab Switch", "model": "EX-12 1.0",
        "description": "Example 12-Port Gigabit Switch",
        "firmware": "1.0.0 Build 20000101 Rel.00001",
        "uptime": "1 day - 2 hour - 3 min - 4 sec", "time": "2000-01-01 00:00:00",
    }


def test_parse_ports():
    result = parse_ports(PORTS)
    assert result["ports_up"] == 9 and result["ports_down"] == 3 and len(result["ports"]) == 12
    assert result["ports"][5] == {"port": "Gi1/0/6", "link": "up", "speed": "1000M", "duplex": "Full", "description": "Printer"}
    assert result["ports"][0] == {"port": "Gi1/0/1", "link": "up", "speed": "1000M", "duplex": "Full"}
    assert result["ports"][11] == {"port": "Gi1/0/12", "link": "down"}


def test_parse_vlans_with_wrapped_lines():
    vlans = {v["id"]: v for v in parse_vlans(VLANS)}
    assert sorted(vlans) == [1, 11, 24, 45, 52, 77, 93]
    assert vlans[11] == {"id": 11, "name": "OFFICE", "untagged": ["Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/4", "Gi1/0/5"], "tagged": []}
    assert vlans[52] == {"id": 52, "name": "GUESTS", "untagged": ["Gi1/0/11"], "tagged": ["Gi1/0/9", "Gi1/0/10"]}
    assert vlans[45]["untagged"] == [] and vlans[45]["tagged"] == ["Gi1/0/9", "Gi1/0/10"]
    assert vlans[24]["untagged"] == ["Gi1/0/7", "Gi1/0/8"]


def test_parse_poe():
    assert parse_poe(POE) == {"limit_w": 370.0, "used_w": 42.5, "remaining_w": 327.5}


BEHAVIOUR = {"quiet_until_enter": False, "mute": False}


class FakeSwitchServer(asyncssh.SSHServer):
    seen_passwords: list = []

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        FakeSwitchServer.seen_passwords.append((username, password))
        return username == "jarvis" and password == PASSWORD


async def fake_cli(process):
    """Behaves like the switch: basic mode first, `enable` for the show level, echo, and one paged listing."""
    name, mode = "Lab Switch", ">"
    if BEHAVIOUR["mute"]:
        await process.stdin.read(4096)
        await asyncio.sleep(30)
        return
    if not BEHAVIOUR["quiet_until_enter"]:
        process.stdout.write(f"\r\n{name}{mode}")
    line, previous = "", ""
    while True:
        char = await process.stdin.read(1)
        if not char:
            return
        # As on TP-Link's command line, Enter is carriage return then line feed; a bare line feed does nothing.
        if char == "\r":
            previous = char
            continue
        if char == "\n":
            if previous != "\r":
                continue
            previous = ""
        else:
            previous = ""
            line += char
            process.stdout.write(char)
            continue
        command, line = line.strip(), ""
        process.stdout.write("\r\n")
        if command == "exit":
            process.exit(0)
            return
        if command == "enable":
            mode = "#"
        elif mode == "#" and command in OUTPUTS:
            text = OUTPUTS[command].replace("\n", "\r\n")
            if command == "show interface status":
                half = text.index(" Gi1/0/6")
                process.stdout.write(text[:half] + "Press any key to continue (Q to quit)")
                await process.stdin.read(1)
                process.stdout.write("\r" + " " * 40 + "\r" + text[half:])
            else:
                process.stdout.write(text)
        elif command:
            process.stdout.write("-" * len(name + mode) + "^\r\nError: Bad command\r\n")
        process.stdout.write(f"\r\n{name}{mode}")


async def start_switch(tmp_path):
    key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    FakeSwitchServer.seen_passwords = []
    server = await asyncssh.create_server(
        FakeSwitchServer, "127.0.0.1", 0, server_host_keys=[key], process_factory=fake_cli, line_editor=False,
    )
    port = server.sockets[0].getsockname()[1]
    known = tmp_path / "known_hosts"
    known.write_text(f"[127.0.0.1]:{port} {key.export_public_key().decode().strip()}\n")
    return server, port, known


def test_full_session_against_a_switch_like_ssh_server(tmp_path):
    async def go():
        server, port, known = await start_switch(tmp_path)
        try:
            session = SwitchSession("127.0.0.1", "jarvis", PASSWORD, known, port=port, timeout=5)
            return await make_switch_tool(session).handler({})
        finally:
            server.close()

    result = run(go())
    assert result["system"]["model"] == "EX-12 1.0" and result["system"]["firmware"].startswith("1.0.0")
    assert result["ports_up"] == 9 and result["ports_down"] == 3
    assert [p["port"] for p in result["ports"]] == [f"Gi1/0/{n}" for n in range(1, 13)], "paged listing must be whole"
    assert result["ports"][5]["description"] == "Printer"
    assert len(result["vlans"]) == 7 and result["poe"]["limit_w"] == 370.0


def test_switch_that_shows_no_prompt_until_enter_still_works(tmp_path, monkeypatch):
    monkeypatch.setitem(BEHAVIOUR, "quiet_until_enter", True)

    async def go():
        server, port, known = await start_switch(tmp_path)
        try:
            session = SwitchSession("127.0.0.1", "jarvis", PASSWORD, known, port=port, timeout=8)
            return await make_switch_tool(session).handler({})
        finally:
            server.close()

    result = run(go())
    assert result["ports_up"] == 9 and len(result["vlans"]) == 7 and result["system"]["name"] == "Lab Switch"


def test_silent_switch_error_names_the_stage(tmp_path, monkeypatch):
    monkeypatch.setitem(BEHAVIOUR, "mute", True)

    async def go():
        server, port, known = await start_switch(tmp_path)
        try:
            session = SwitchSession("127.0.0.1", "jarvis", PASSWORD, known, port=port, timeout=4)
            return await make_switch_tool(session).handler({})
        finally:
            server.close()

    assert run(go()) == {"error": "the switch stopped answering (waiting for the first prompt)"}


def test_wrong_host_key_is_refused_before_any_password_is_sent(tmp_path):
    async def go():
        server, port, known = await start_switch(tmp_path)
        other = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
        known.write_text(f"[127.0.0.1]:{port} {other.export_public_key().decode().strip()}\n")
        try:
            session = SwitchSession("127.0.0.1", "jarvis", PASSWORD, known, port=port, timeout=5)
            return await make_switch_tool(session).handler({})
        finally:
            server.close()

    result = run(go())
    assert result == {"error": "the switch presented a host key that is not the pinned one"}
    assert FakeSwitchServer.seen_passwords == []


def test_wrong_password_and_unreachable_switch_are_plain_errors(tmp_path):
    async def go():
        server, port, known = await start_switch(tmp_path)
        try:
            bad = SwitchSession("127.0.0.1", "jarvis", "wrong", known, port=port, timeout=5)
            refused = await make_switch_tool(bad).handler({})
        finally:
            server.close()
            await server.wait_closed()
        gone = SwitchSession("127.0.0.1", "jarvis", PASSWORD, known, port=port, timeout=2)
        return refused, await make_switch_tool(gone).handler({})

    refused, unreachable = run(go())
    assert refused == {"error": "the switch refused the view-only login"}
    assert unreachable["error"].startswith("switch unreachable:")
    assert PASSWORD not in str(refused) + str(unreachable)
