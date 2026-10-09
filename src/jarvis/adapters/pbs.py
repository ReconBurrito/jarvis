"""Read-only Proxmox Backup Server adapter.

Same rules as the Proxmox adapter: GET only, one fixed path per tool, the Audit token attached after the model has
chosen the tool. PBS uses a self-signed certificate, so the server is verified against its own certificate, pinned
(the owner puts it on the brain as /etc/jarvis/trust/pbs.pem, checked against the fingerprint read on the PBS
console), and against the name in that certificate.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..tools import TIER_READ_ONLY, Tool
from .common import AdapterError, ReadOnlyApiClient, gib, percent

STORE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
UPID = re.compile(r"^UPID:[A-Za-z0-9._:@!/+=\\-]{10,300}$")  # worker ids are escaped, as in backups\\x3act-214
LOGS_READ = 3  # the newest failed tasks whose logs are fetched
LOG_LINES = 12  # the end of a log is where the reason is


class PbsClient(ReadOnlyApiClient):
    system = "backup server"

    def __init__(
        self,
        hosts: list[str],
        token_id: str,
        token_secret: str,
        cert_file: Path | None = None,
        http: httpx.AsyncClient | None = None,
        port: int = 8007,
    ):
        super().__init__(
            hosts, f"PBSAPIToken={token_id}:{token_secret}", port, trust_file=cert_file, pinned=True, http=http
        )

    async def store_names(self) -> list[str]:
        return sorted(s["store"] for s in await self.get("/status/datastore-usage"))


def _hours_ago(epoch: Any, now: float) -> float | None:
    try:
        return round((now - float(epoch)) / 3600, 1)
    except (TypeError, ValueError):
        return None


def make_pbs_tools(client: PbsClient, clock=time.time) -> list[Tool]:
    async def datastores(_: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for s in await client.get("/status/datastore-usage"):
            row = {
                "datastore": s.get("store"),
                "used_percent": percent(s.get("used"), s.get("total")),
                "total_gib": gib(s.get("total")),
                "free_gib": gib(s.get("avail")),
            }
            if s.get("error"):
                row["error"] = str(s["error"])[:160]
            rows.append(row)
        return {"datastores": sorted(rows, key=lambda r: r["datastore"] or "")}

    async def backups(args: dict[str, Any]) -> dict[str, Any]:
        try:
            max_age = float(args.get("max_age_hours") or 36)
        except (TypeError, ValueError):
            return {"error": "max_age_hours must be a number"}
        wanted = str(args.get("datastore") or "")
        if wanted and not STORE_NAME.match(wanted):
            return {"error": "datastore must be a plain datastore name"}
        stores = await client.store_names()
        if wanted and wanted not in stores:
            return {"error": f"unknown datastore; the server has: {', '.join(stores)}"}
        now = clock()
        result = []
        for store in [wanted] if wanted else stores:
            groups = []
            for g in await client.get(f"/admin/datastore/{store}/groups"):
                groups.append(
                    {
                        "guest": f"{g.get('backup-type')}/{g.get('backup-id')}",
                        "last_backup_hours_ago": _hours_ago(g.get("last-backup"), now),
                        "snapshots": g.get("backup-count"),
                    }
                )
            groups.sort(key=lambda g: (g["last_backup_hours_ago"] is None, g["last_backup_hours_ago"]))
            stale = [g for g in groups if g["last_backup_hours_ago"] is None or g["last_backup_hours_ago"] > max_age]
            result.append(
                {
                    "datastore": store,
                    "guests_with_backups": len(groups),
                    "fresh": len(groups) - len(stale),
                    "older_than_limit": [g["guest"] for g in stale],
                    "limit_hours": max_age,
                    "newest_hours_ago": groups[0]["last_backup_hours_ago"] if groups else None,
                    "groups": groups,
                }
            )
        return {"datastores": result}

    async def failed_tasks(args: dict[str, Any]) -> dict[str, Any]:
        try:
            days = min(max(float(args.get("days") or 7), 1), 30)
        except (TypeError, ValueError):
            return {"error": "days must be a number"}
        now = clock()
        tasks = await client.get(
            "/nodes/localhost/tasks", {"errors": "true", "limit": "20", "since": str(int(now - days * 86400))}
        )
        rows = [
            {
                "task": t.get("worker_type"),
                "target": t.get("worker_id"),
                "started_hours_ago": _hours_ago(t.get("starttime"), now),
                "result": str(t.get("status") or "")[:160],
            }
            for t in tasks
        ]
        # The status line seldom says why. The end of the task's own log does, so it comes along for the newest few.
        newest = sorted(range(len(tasks)), key=lambda i: -float(tasks[i].get("starttime") or 0))[:LOGS_READ]
        for index in newest:
            rows[index]["log_end"] = await log_end(str(tasks[index].get("upid") or ""))
        return {"days": days, "failed_count": len(rows), "failed": rows}

    async def log_end(upid: str) -> list[str] | str:
        if not UPID.match(upid):
            return "the log could not be read: the task has no usable id"
        try:
            lines = await client.get(f"/nodes/localhost/tasks/{quote(upid, safe='')}/log", {"start": "0", "limit": "500"})
        except AdapterError as exc:
            return f"the log could not be read: {exc}"
        text = [str(line.get("t") or "")[:200] for line in lines if isinstance(line, dict)]
        return [line for line in text if line.strip()][-LOG_LINES:] or "the log is empty"

    def tool(name: str, description: str, properties: dict[str, Any], handler) -> Tool:
        async def guarded(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await handler(args)
            except AdapterError as exc:
                return {"error": str(exc)}

        return Tool(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties, "required": []},
            tier=TIER_READ_ONLY,
            handler=guarded,
        )

    return [
        tool(
            "pbs_datastores",
            "Read-only. Backup server datastores with how full each one is.",
            {},
            datastores,
        ),
        tool(
            "pbs_backups",
            "Read-only. For each guest on the backup server: how many hours ago its last backup finished and how many "
            "snapshots exist. Lists guests whose last backup is older than max_age_hours (default 36).",
            {
                "datastore": {"type": "string", "description": "Only this datastore"},
                "max_age_hours": {"type": "number", "description": "Age limit for a backup to count as fresh"},
            },
            backups,
        ),
        tool(
            "pbs_failed_tasks",
            "Read-only. Backup server tasks that ended in an error or with warnings in the last days (default 7): "
            "backups, verifies, prunes, garbage collection, sync, package updates. For the newest ones, log_end holds "
            "the last lines of the task's own log, which is where the reason is: quote from it when asked what went wrong.",
            {"days": {"type": "number", "description": "How many days back to look, 1 to 30"}},
            failed_tasks,
        ),
    ]
