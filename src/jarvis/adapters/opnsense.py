"""Read-only OPNsense adapter.

OPNsense privileges are granted per page, and most status pages also allow an
action. The API user therefore holds only "Lobby: Dashboard", whose endpoints
report on the firewall without changing it. This adapter calls a fixed set of
those endpoints with GET. The web certificate is self-signed, so it is pinned
(/etc/jarvis/trust/opnsense.pem on the brain) and must be pinned again when OPNsense renews it.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from ..tools import TIER_READ_ONLY, Tool
from .common import AdapterError, ReadOnlyApiClient


class OpnsenseClient(ReadOnlyApiClient):
    system = "firewall"

    def __init__(
        self,
        hosts: list[str],
        api_key: str,
        api_secret: str,
        cert_file: Path | None = None,
        http: httpx.AsyncClient | None = None,
        port: int = 443,
    ):
        basic = base64.b64encode(f"{api_key}:{api_secret}".encode("utf-8")).decode("ascii")
        super().__init__(
            hosts, f"Basic {basic}", port, trust_file=cert_file, pinned=True, http=http, base_path="/api", unwrap=None
        )


def _number(value: Any) -> float | None:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def make_opnsense_tools(client: OpnsenseClient) -> list[Tool]:
    async def section(path: str) -> tuple[Any, str | None]:
        try:
            return await client.get(path), None
        except AdapterError as exc:
            return None, str(exc)

    async def status(_: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        problems: list[str] = []

        info, err = await section("/diagnostics/system/system_information")
        if isinstance(info, dict):
            result["name"] = info.get("name")
            result["versions"] = info.get("versions")
        elif err:
            # If the first call cannot get through, none will: stop here with the reason.
            return {"error": err}

        clock, err = await section("/diagnostics/system/system_time")
        if isinstance(clock, dict):
            result["uptime"] = clock.get("uptime")
            result["load_average"] = clock.get("loadavg")
            result["last_config_change"] = clock.get("config")
        elif err:
            problems.append(f"time: {err}")

        res, err = await section("/diagnostics/system/system_resources")
        if isinstance(res, dict) and isinstance(res.get("memory"), dict):
            memory = res["memory"]
            used, total = _number(memory.get("used")), _number(memory.get("total"))
            if used is not None and total:
                result["memory_used_percent"] = round(100 * used / total, 1)
        elif err:
            problems.append(f"memory: {err}")

        disk, err = await section("/diagnostics/system/system_disk")
        if isinstance(disk, dict) and isinstance(disk.get("devices"), list):
            result["disks"] = [
                {"mountpoint": d.get("mountpoint"), "used_percent": _number(d.get("used_pct"))}
                for d in disk["devices"][:8]
                if isinstance(d, dict)
            ]
        elif err:
            problems.append(f"disk: {err}")

        temps, err = await section("/diagnostics/system/system_temperature")
        if isinstance(temps, list):
            readings = [t for t in (_number(x.get("temperature")) for x in temps if isinstance(x, dict)) if t is not None]
            if readings:
                result["hottest_sensor_c"] = max(readings)
        elif err:
            problems.append(f"temperature: {err}")

        states, err = await section("/diagnostics/firewall/pf_states")
        if isinstance(states, dict):
            result["firewall_states"] = {"current": _number(states.get("current")), "limit": _number(states.get("limit"))}
        elif err:
            problems.append(f"states: {err}")

        if problems:
            result["unavailable"] = problems
        return result

    return [
        Tool(
            name="opnsense_status",
            description=(
                "Read-only. Health of the OPNsense firewall: name, versions, uptime, load, memory and disk use, "
                "hottest temperature sensor, number of firewall states, and when the configuration last changed. "
                "Takes no arguments."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            tier=TIER_READ_ONLY,
            handler=status,
        )
    ]
