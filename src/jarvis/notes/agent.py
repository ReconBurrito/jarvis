"""An ssh-agent of Jarvis's own, holding the notes repository's deploy key in memory only.

The key comes from the vault. It is never written to a file: it is handed to a private ssh-agent on its standard
input, and git reaches GitHub through that agent. (A key in memory cannot be given to ssh by a /dev/fd path, since
ssh closes every inherited file descriptor when it starts.) The agent's socket lives in a folder only this process
can open, and the agent is stopped when the key is no longer needed or changes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

PATH = "/usr/bin:/bin"


class AgentError(RuntimeError):
    pass


class KeyAgent:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._folder: Path | None = None
        self.socket: Path | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, key: bytes) -> Path:
        """Starts the agent with this one key in it. Returns the socket."""
        if b"PRIVATE KEY-----" not in key:
            raise AgentError("the deploy key is not a private key")
        await self.stop()
        folder = Path(tempfile.mkdtemp(prefix="jarvis-agent-"))
        os.chmod(folder, 0o700)
        socket = folder / "socket"
        self._folder, self.socket = folder, socket
        try:
            process = await asyncio.create_subprocess_exec(
                "ssh-agent", "-D", "-a", str(socket), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env={"PATH": PATH})
        except OSError as exc:
            await self.stop()
            raise AgentError(f"ssh-agent could not be started ({type(exc).__name__})") from None
        self._process = process
        for _ in range(100):
            if socket.exists() or process.returncode is not None:
                break
            await asyncio.sleep(0.02)
        if not socket.exists() or self._process is not process:   # stopped meanwhile, or never came up
            await self.stop()
            raise AgentError("ssh-agent did not start")
        add = await asyncio.create_subprocess_exec(
            "ssh-add", "-q", "-", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env={"PATH": PATH, "SSH_AUTH_SOCK": str(socket)})
        await add.communicate(key if key.endswith(b"\n") else key + b"\n")
        if add.returncode != 0 or self._process is not process:
            await self.stop()
            raise AgentError("the deploy key was not accepted by ssh-add (a key with a passphrase, or not an SSH key)")
        return socket

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)
        self._folder = self.socket = None
