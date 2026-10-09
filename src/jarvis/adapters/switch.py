"""Read-only adapter for a TP-Link (Omada) smart switch.

The switch has no API, so Jarvis logs in over SSH as a view-only user and runs
a fixed set of `show` commands. The commands are constants in this file: the
model can ask for the switch status, but it cannot supply command text. The
switch's host key is pinned (/etc/jarvis/trust/switch_known_hosts on the brain),
and the password is only ever sent after that key has been verified.
"""

from __future__ import annotations

import asyncio
import logging
import re
import warnings
from pathlib import Path
from typing import Any, Callable

import asyncssh
from cryptography.utils import CryptographyDeprecationWarning

from ..tools import TIER_READ_ONLY, Tool
from .common import AdapterError

log = logging.getLogger("jarvis.switch")
# Some switches offer only finite-field Diffie-Hellman, which cryptography warns about on every login. The
# warning says nothing the owner can act on, so it is not printed; the host key check is unaffected.
warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning, module=r"asyncssh\.")

# Each fixed command and a word that only its real output contains. A reply is
# complete when that word has arrived and the text ends at a prompt, so a stray
# extra prompt can never be mistaken for the answer.
COMMANDS = (
    ("show system-info", "System Description"),
    ("show interface status", "Status"),
    ("show vlan brief", "VLAN"),
    ("show power inline", "System Power"),
)
# TP-Link's command line only accepts carriage return plus line feed as Enter.
ENTER = "\r\n"
PROMPT = re.compile(r"(?:^|\n)[^\n]{1,64}[>#] ?$")
PAGER = re.compile(r"Press any key to continue[^\n]*$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def parse_system(text: str) -> dict[str, Any]:
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(" - ")
        if sep:
            fields[key.strip()] = value.strip()
    return {
        "name": fields.get("System Name"),
        "model": fields.get("Hardware Version"),
        "description": fields.get("System Description"),
        "firmware": fields.get("Software Version"),
        "uptime": fields.get("Running Time"),
        "time": fields.get("System Time"),
    }


def parse_ports(text: str) -> dict[str, Any]:
    ports = []
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 2 or not re.match(r"^[A-Za-z]{2}\d+/\d+/\d+$", cols[0]):
            continue
        up = cols[1].lower() == "linkup"
        port = {"port": cols[0], "link": "up" if up else "down"}
        if up and len(cols) >= 4:
            port["speed"] = cols[2]
            port["duplex"] = cols[3]
        # Columns: Port Status Speed Duplex FlowCtrl Active-Medium LAG Linkdown-Status [Description...]
        if len(cols) > 8:
            port["description"] = " ".join(cols[8:])
        ports.append(port)
    return {
        "ports_up": sum(1 for p in ports if p["link"] == "up"),
        "ports_down": sum(1 for p in ports if p["link"] == "down"),
        "ports": ports,
    }


def parse_vlans(text: str) -> list[dict[str, Any]]:
    vlans: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    kind = None
    for line in text.splitlines():
        head = re.match(r"^(\d+)\s+(\S.*?)\s+(active|suspend\w*)\s*(.*)$", line.strip())
        if head:
            current = {"id": int(head.group(1)), "name": head.group(2).strip(), "untagged": [], "tagged": []}
            vlans.append(current)
            rest, kind = head.group(4), None
        elif current is not None and line.strip() and not line.startswith(("UT:", "VLAN", "---")):
            rest = line.strip()
        else:
            continue
        marker = re.match(r"^(UT|TG):\s*(.*)$", rest)
        if marker:
            kind = "untagged" if marker.group(1) == "UT" else "tagged"
            rest = marker.group(2)
        if kind:
            current[kind].extend(p.strip() for p in rest.split(",") if p.strip())
    return vlans


def parse_poe(text: str) -> dict[str, Any]:
    def watts(label: str) -> float | None:
        found = re.search(rf"{label}:\s*([0-9.]+)\s*w", text, re.IGNORECASE)
        return float(found.group(1)) if found else None

    return {
        "limit_w": watts("System Power Limit"),
        "used_w": watts("System Power Consumption"),
        "remaining_w": watts("System Power Remain"),
    }


class SwitchSession:
    """One SSH login: verify the host key, log in, raise to the show level, run the fixed commands."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        known_hosts: Path,
        port: int = 22,
        timeout: float = 10,
    ):
        self.host, self.port = host, port
        self._username, self._password = username, password
        self._known_hosts = str(known_hosts)
        self._timeout = timeout

    async def _read_until(self, process, stage: str, done: Callable[[str], bool], nudge: bool = False) -> str:
        """Read until the text ends at a prompt and `done` accepts it. Timeouts name the stage."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        nudge_at = loop.time() + 3 if nudge else None
        buffer = ""
        while True:
            now = loop.time()
            if now >= deadline:
                log.warning("switch timeout %s; last text %r", stage, buffer[-160:])
                raise AdapterError(f"the switch stopped answering ({stage})")
            wait = deadline - now
            if nudge_at is not None:
                wait = min(wait, max(nudge_at - now, 0.05))
            try:
                chunk = await asyncio.wait_for(process.stdout.read(4096), wait)
            except asyncio.TimeoutError:
                if nudge_at is not None and loop.time() >= nudge_at:
                    # A command line may show no prompt until Enter is pressed once.
                    process.stdin.write(ENTER)
                    nudge_at = None
                continue
            if not chunk:
                log.warning("switch closed the session %s; last text %r", stage, buffer[-160:])
                raise AdapterError(f"the switch closed the session ({stage})")
            buffer += CONTROL.sub("", ANSI.sub("", chunk).replace("\r", ""))
            if PAGER.search(buffer):
                buffer = PAGER.sub("", buffer)
                process.stdin.write(" ")
                continue
            if PROMPT.search(buffer) and done(buffer):
                return buffer

    async def run(self) -> dict[str, str]:
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    self.host,
                    port=self.port,
                    username=self._username,
                    password=self._password,
                    known_hosts=self._known_hosts,
                    client_keys=None,
                    agent_path=None,
                    preferred_auth="password",
                    config=None,
                ),
                self._timeout,
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            raise AdapterError("the switch presented a host key that is not the pinned one") from exc
        except asyncssh.PermissionDenied as exc:
            raise AdapterError("the switch refused the view-only login") from exc
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            raise AdapterError(f"switch unreachable: {type(exc).__name__}") from exc

        try:
            async with conn:
                process = await asyncio.wait_for(
                    conn.create_process(term_type="vt100", term_size=(200, 1000), errors="replace"),
                    self._timeout,
                )
                banner = await self._read_until(process, "waiting for the first prompt", lambda _: True, nudge=True)
                if not banner.rstrip().endswith("#"):
                    process.stdin.write("enable" + ENTER)
                    await self._read_until(process, "after enable", lambda text: text.rstrip().endswith("#"))
                outputs = {}
                for command, marker in COMMANDS:
                    process.stdin.write(command + ENTER)
                    text = await self._read_until(process, f"after {command}", lambda text, m=marker: m in text)
                    lines = text.split("\n")
                    # Drop echoed commands, stray prompts and the trailing prompt line.
                    body = [
                        ln for ln in lines[:-1]
                        if not ln.strip().endswith(command) and not PROMPT.search(ln)
                    ]
                    outputs[command] = "\n".join(body)
                process.stdin.write("exit" + ENTER)
                return outputs
        except asyncio.TimeoutError as exc:
            raise AdapterError("the switch stopped answering (opening the session)") from exc
        except (OSError, asyncssh.Error) as exc:
            raise AdapterError(f"switch session failed: {type(exc).__name__}") from exc


def make_switch_tool(session: SwitchSession) -> Tool:
    async def handler(_: dict[str, Any]) -> dict[str, Any]:
        try:
            out = await session.run()
        except AdapterError as exc:
            return {"error": str(exc)}
        return {
            "system": parse_system(out["show system-info"]),
            **parse_ports(out["show interface status"]),
            "vlans": parse_vlans(out["show vlan brief"]),
            "poe": parse_poe(out["show power inline"]),
        }

    return Tool(
        name="switch_status",
        description=(
            "Read-only. Status of the network switch: model, firmware and uptime, each port's link state, speed and "
            "description, which ports are in which VLAN (untagged and tagged), and PoE power use. Takes no arguments."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        tier=TIER_READ_ONLY,
        handler=handler,
    )
