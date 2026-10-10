"""Jarvis's tools for its own machine's files, through jarvis-fsd (daemon.py), which runs as root.

The owner gave Jarvis read and write access to the whole of its own machine. jarvis-fsd keeps what must stay
out of reach (keys, the vault, password files, /proc, /sys, /dev) and a copy of what was there before every
change, so each change can be undone (fs_undo). Text that is a value of the vault is masked before the model
sees it, like every tool result.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..tools import TIER_READ_ONLY, Tool

TIMEOUT = 60
LIMIT = 4 * 1024 * 1024


class SelfFiles:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    async def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.socket_path), limit=LIMIT), 5)
        except (OSError, asyncio.TimeoutError) as exc:
            return {"error": f"jarvis-fsd does not answer ({type(exc).__name__}); see: systemctl status jarvis-fs"}
        try:
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readuntil(b"\n"), TIMEOUT)
            answer = json.loads(line)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as exc:
            return {"error": f"jarvis-fsd's answer could not be read ({type(exc).__name__})"}
        finally:
            writer.close()
        return answer if isinstance(answer, dict) else {"error": "jarvis-fsd answered with something that is not an object"}


def make_selffs_tools(files: SelfFiles) -> list[Tool]:
    def path_only(description: str) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string", "description": description}}, "required": ["path"]}

    async def fs_list(a: dict[str, Any]) -> dict[str, Any]:
        return await files.ask({"op": "list", "path": str(a.get("path") or "/")})

    async def fs_read(a: dict[str, Any]) -> dict[str, Any]:
        return await files.ask({"op": "read", "path": a.get("path"), "offset": a.get("offset", 0), "length": a.get("length", 65536)})

    async def fs_find(a: dict[str, Any]) -> dict[str, Any]:
        return await files.ask({"op": "find", "path": a.get("path") or "/", "name": a.get("name") or "*", "contains": a.get("contains") or ""})

    async def fs_write(a: dict[str, Any]) -> dict[str, Any]:
        request = {"op": "write", "path": a.get("path"), "content": a.get("content")}
        if a.get("mode"):
            request["mode"] = str(a["mode"])
        if a.get("expect_sha256"):
            request["expect"] = str(a["expect_sha256"])
        return await files.ask(request)

    async def fs_mkdir(a: dict[str, Any]) -> dict[str, Any]:
        return await files.ask({"op": "mkdir", "path": a.get("path")})

    async def fs_delete(a: dict[str, Any]) -> dict[str, Any]:
        return await files.ask({"op": "delete", "path": a.get("path")})

    async def fs_changes(a: dict[str, Any]) -> dict[str, Any]:
        return await files.ask({"op": "changes", "limit": a.get("limit", 20)})

    async def fs_undo(a: dict[str, Any]) -> dict[str, Any]:
        return await files.ask({"op": "undo", "change": str(a.get("change") or "")})

    # Tier 0: the owner chose to let Jarvis change its own machine's files without asking each time. Every change
    # is in the audit log (content by length and fingerprint only) and can be undone with fs_undo.
    return [
        Tool(name="fs_list", description="List a folder on your own machine (the brain): names, types, sizes, owners.",
             parameters=path_only("Full path of the folder, such as /etc/jarvis"), tier=TIER_READ_ONLY, handler=fs_list),
        Tool(name="fs_read", description="Read a text file on your own machine. Large files come in parts: give offset "
                                         "to read on. Keys and the vault are kept from you.",
             parameters={"type": "object", "properties": {
                 "path": {"type": "string", "description": "Full path of the file"},
                 "offset": {"type": "integer", "description": "Byte to start at (default 0)"},
                 "length": {"type": "integer", "description": "Bytes to read (default 65536, at most 262144)"}},
                 "required": ["path"]}, tier=TIER_READ_ONLY, handler=fs_read),
        Tool(name="fs_find", description="Find files on your own machine under a folder, by name pattern (such as *.conf) "
                                         "and, if given, text they contain.",
             parameters={"type": "object", "properties": {
                 "path": {"type": "string", "description": "Folder to search under"},
                 "name": {"type": "string", "description": "File name pattern, such as *.service"},
                 "contains": {"type": "string", "description": "Text the file must contain"}},
                 "required": ["path"]}, tier=TIER_READ_ONLY, handler=fs_find),
        Tool(name="fs_write", description="Write a whole text file on your own machine: a new file, or one that replaces what "
                                          "is there. To replace a file, read it first and pass the sha256 fs_read gave as "
                                          "expect_sha256, so a change made meanwhile is not lost; for a new file leave it out. "
                                          "The file keeps its owner and mode unless mode is given. Every write can be undone "
                                          "with fs_undo. Only do this when the owner asked for the change.",
             parameters={"type": "object", "properties": {
                 "path": {"type": "string", "description": "Full path of the file"},
                 "content": {"type": "string", "description": "The file's whole new text"},
                 "mode": {"type": "string", "description": "Octal mode for the file, such as 644 (optional)"},
                 "expect_sha256": {"type": "string", "description": "Only when replacing a file: the sha256 fs_read gave for it"}},
                 "required": ["path", "content"]}, tier=TIER_READ_ONLY, handler=fs_write, private=("content",)),
        Tool(name="fs_mkdir", description="Make a folder on your own machine.", parameters=path_only("Full path of the new folder"),
             tier=TIER_READ_ONLY, handler=fs_mkdir),
        Tool(name="fs_delete", description="Delete a file, a link or an empty folder on your own machine. It can be undone "
                                           "with fs_undo. Only do this when the owner asked for it.",
             parameters=path_only("Full path of what to delete"), tier=TIER_READ_ONLY, handler=fs_delete),
        Tool(name="fs_changes", description="The latest changes you made to your own machine's files, newest first, with "
                                            "the numbers fs_undo takes.",
             parameters={"type": "object", "properties": {"limit": {"type": "integer", "description": "How many (default 20)"}},
                         "required": []}, tier=TIER_READ_ONLY, handler=fs_changes),
        Tool(name="fs_undo", description="Undo one change you made to your own machine's files, by its number from "
                                         "fs_changes or from the answer to the change.",
             parameters={"type": "object", "properties": {"change": {"type": "string", "description": "The change's number"}},
                         "required": ["change"]}, tier=TIER_READ_ONLY, handler=fs_undo),
    ]
