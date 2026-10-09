"""Read-only Proxmox adapter.

The client can only send GET requests; there is no method that writes. Each tool calls one fixed API path, so the
model cannot reach anything else on the cluster. The token (an API token with the PVEAuditor role) is attached
here, after the model has chosen the tool; the model never sees it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from ..tools import TIER_READ_ONLY, Tool
from .common import AdapterError, ReadOnlyApiClient
from .common import gib as _gib
from .common import hours as _hours
from .common import percent as _percent

NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")

ProxmoxError = AdapterError


class ProxmoxClient(ReadOnlyApiClient):
    system = "Proxmox node"

    def __init__(
        self,
        hosts: list[str],
        token_id: str,
        token_secret: str,
        ca_file: Path | None = None,
        http: httpx.AsyncClient | None = None,
        port: int = 8006,
    ):
        super().__init__(hosts, f"PVEAPIToken={token_id}={token_secret}", port, trust_file=ca_file, http=http)

    async def node_names(self) -> list[str]:
        return sorted(r["node"] for r in await self.get("/cluster/resources", {"type": "node"}))


def make_proxmox_tools(client: ProxmoxClient) -> list[Tool]:
    async def cluster_status(_: dict[str, Any]) -> dict[str, Any]:
        status = await client.get("/cluster/status")
        resources = {r["node"]: r for r in await client.get("/cluster/resources", {"type": "node"})}
        cluster = next((s for s in status if s.get("type") == "cluster"), None)
        nodes = []
        for entry in status:
            if entry.get("type") != "node":
                continue
            res = resources.get(entry["name"], {})
            nodes.append(
                {
                    "node": entry["name"],
                    "online": bool(entry.get("online")),
                    "cpu_percent": round(100 * float(res.get("cpu", 0)), 1) if "cpu" in res else None,
                    "cpu_count": res.get("maxcpu"),
                    "memory_percent": _percent(res.get("mem"), res.get("maxmem")),
                    "memory_total_gib": _gib(res.get("maxmem")),
                    "uptime_hours": _hours(res.get("uptime")),
                }
            )
        return {
            "cluster": cluster.get("name") if cluster else None,
            "quorate": bool(cluster.get("quorate")) if cluster else None,
            "nodes": sorted(nodes, key=lambda n: n["node"]),
        }

    async def guests(args: dict[str, Any]) -> dict[str, Any]:
        want_status = str(args.get("status") or "").lower()
        want_node = str(args.get("node") or "")
        if want_status and want_status not in ("running", "stopped"):
            return {"error": "status must be running or stopped"}
        rows = []
        for r in await client.get("/cluster/resources", {"type": "vm"}):
            if r.get("template"):
                continue
            if want_status and r.get("status") != want_status:
                continue
            if want_node and r.get("node") != want_node:
                continue
            rows.append(
                {
                    "id": r.get("vmid"),
                    "name": r.get("name"),
                    "kind": "container" if r.get("type") == "lxc" else "vm",
                    "node": r.get("node"),
                    "status": r.get("status"),
                    "memory_percent": _percent(r.get("mem"), r.get("maxmem")) if r.get("status") == "running" else None,
                }
            )
        rows.sort(key=lambda g: g["id"] or 0)
        running = sum(1 for g in rows if g["status"] == "running")
        return {"count": len(rows), "running": running, "stopped": len(rows) - running, "guests": rows}

    async def node_status(args: dict[str, Any]) -> dict[str, Any]:
        node = str(args.get("node") or "")
        if not NODE_NAME.match(node):
            return {"error": "node must be a plain node name"}
        known = await client.node_names()
        if node not in known:
            return {"error": f"unknown node; the cluster has: {', '.join(known)}"}
        data = await client.get(f"/nodes/{node}/status")
        memory, rootfs = data.get("memory", {}), data.get("rootfs", {})
        return {
            "node": node,
            "cpu_percent": round(100 * float(data.get("cpu", 0)), 1),
            "load_average": data.get("loadavg"),
            "memory_percent": _percent(memory.get("used"), memory.get("total")),
            "memory_total_gib": _gib(memory.get("total")),
            "root_disk_percent": _percent(rootfs.get("used"), rootfs.get("total")),
            "uptime_hours": _hours(data.get("uptime")),
            "pve_version": data.get("pveversion"),
            "kernel": (data.get("current-kernel") or {}).get("release") or data.get("kversion"),
            "cpu_model": (data.get("cpuinfo") or {}).get("model"),
        }

    async def storage(_: dict[str, Any]) -> dict[str, Any]:
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for r in await client.get("/cluster/resources", {"type": "storage"}):
            # Shared storage is reported once per node; list it once.
            key = (r.get("storage"), "shared" if r.get("shared") else r.get("node"))
            seen.setdefault(
                key,
                {
                    "storage": r.get("storage"),
                    "node": "all nodes" if r.get("shared") else r.get("node"),
                    "type": r.get("plugintype"),
                    "status": r.get("status"),
                    "used_percent": _percent(r.get("disk"), r.get("maxdisk")),
                    "total_gib": _gib(r.get("maxdisk")),
                },
            )
        return {"storages": sorted(seen.values(), key=lambda s: (s["storage"] or "", s["node"] or ""))}

    def tool(name: str, description: str, properties: dict[str, Any], required: list[str], handler) -> Tool:
        async def guarded(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await handler(args)
            except ProxmoxError as exc:
                return {"error": str(exc)}

        return Tool(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties, "required": required},
            tier=TIER_READ_ONLY,
            handler=guarded,
        )

    return [
        tool(
            "proxmox_cluster_status",
            "Read-only. Health of the Proxmox cluster: quorum, and for each node whether it is online, CPU and memory use, uptime.",
            {},
            [],
            cluster_status,
        ),
        tool(
            "proxmox_guests",
            "Read-only. List the virtual machines and containers on the Proxmox cluster with id, name, node and status. "
            "Optional filters: status (running or stopped) and node.",
            {
                "status": {"type": "string", "enum": ["running", "stopped"], "description": "Only guests in this state"},
                "node": {"type": "string", "description": "Only guests on this node"},
            },
            [],
            guests,
        ),
        tool(
            "proxmox_node_status",
            "Read-only. Detail for one Proxmox node: CPU, load, memory, root disk, uptime, Proxmox and kernel versions.",
            {"node": {"type": "string", "description": "Node name, as proxmox_cluster_status lists it"}},
            ["node"],
            node_status,
        ),
        tool(
            "proxmox_storage",
            "Read-only. Proxmox storage pools with how full each one is.",
            {},
            [],
            storage,
        ),
    ]
