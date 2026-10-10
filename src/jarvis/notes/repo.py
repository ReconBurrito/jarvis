"""The notes repository as a git working tree.

Jarvis keeps a clone in its state folder and talks to the remote with a deploy key that can reach this one
repository only. The key comes from the vault and is held by an ssh-agent of Jarvis's own, in memory only, and the
remote's host keys are pinned. Three rules are enforced here, in code, whatever the model asks for: paths cannot
leave the repository, the standing files change only through proposal branches, and nothing that looks like a
secret is ever committed.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
import tempfile
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from .agent import AgentError, KeyAgent

# These shape how Jarvis behaves in every conversation, so they change only when the owner merges a proposal.
PROTECTED_FILES = frozenset({"SOUL.md", "MEMORY.md", "USER.md", "HEARTBEAT.md", "SCHEMA.md"})
PROTECTED_DIRS = ("skills/",)
PROPOSAL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SECRET_PATTERNS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AGE-SECRET-KEY-1[A-Z0-9]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bsk-ant-[A-Za-z0-9_-]{20,}|\bPVEAPIToken=|\bPBSAPIToken="
    r"|(?i:\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9+/_\-]{16,})"
)
OFF, READY, FAILED = "off", "ready", "failed"
AUTHOR = ("-c", "user.name=Jarvis", "-c", "user.email=jarvis@localhost")
# A remote is a repository over SSH (user@host:path, or ssh://), or a folder on this machine named by its full path.
SSH_REMOTE = re.compile(r"^(?:[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+|ssh://[A-Za-z0-9._@:/~-]+)$")


FIRST_NOTE = """# Jarvis's notes

What Jarvis knows and keeps. Jarvis searches and reads these notes, and adds to them when you ask it to. The
standing files (SOUL.md, MEMORY.md, USER.md, HEARTBEAT.md, SCHEMA.md) and everything under skills/ shape how it
behaves, so Jarvis changes them only through a proposal branch that you merge. You can edit any note in the Notes
window of Jarvis's panel.
"""


class BrainError(RuntimeError):
    pass


def is_ssh_remote(remote: str) -> bool:
    return bool(SSH_REMOTE.match(remote))


def is_protected(rel: str) -> bool:
    return rel in PROTECTED_FILES or rel.startswith(PROTECTED_DIRS)


class BrainRepo:
    def __init__(
        self,
        path: Path,
        remote: str = "",
        deploy_key: bytes = b"",
        known_hosts: Path | None = None,
        has_secret: Callable[[str], bool] | None = None,
        local: bool = False,
    ):
        self.path = path
        self.remote = remote
        self._key = deploy_key
        self._known_hosts = known_hosts
        self._has_secret = has_secret or (lambda text: False)
        self.local = local   # with no remote: notes kept on this machine only, made when there are none yet
        self._lock = asyncio.Lock()
        self._agent = KeyAgent()
        self._env: dict[str, str] | None = None
        self._closed = False
        self.status, self.detail = OFF, "not started"

    @property
    def ready(self) -> bool:
        return self.status == READY

    @property
    def can_push(self) -> bool:
        return bool(self.remote) and self._env is not None

    # ---- git plumbing --------------------------------------------------------------------------

    async def _prepare_env(self) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        if self._closed:
            raise BrainError("the notes were closed (the vault changed or locked)")
        if not self.remote:
            return env
        if not SSH_REMOTE.match(self.remote):
            if not self.remote.startswith("/"):
                raise BrainError("JARVIS_NOTES_REPO must be a repository over SSH (such as git@github.com:you/notes.git) "
                                 "or a folder on this machine named by its full path")
            env["GIT_ALLOW_PROTOCOL"] = "file"
            return env
        env["GIT_ALLOW_PROTOCOL"] = "ssh"
        if not (self._key and self._known_hosts and self._known_hosts.is_file()):
            raise BrainError("the deploy key or the pinned host keys are missing")
        if not self._agent.running:
            try:
                await self._agent.start(self._key)
            except AgentError as exc:
                raise BrainError(str(exc)) from None
        env["GIT_SSH_COMMAND"] = (
            f"ssh -F /dev/null -o IdentityAgent={self._agent.socket} -o BatchMode=yes -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={self._known_hosts} -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=15"
        )
        return env

    async def close(self) -> None:
        """Lets the key go for good: the agent stops, and this object never starts it again."""
        self._closed = True
        self._key = b""
        await self._agent.stop()
        async with self._locked():
            await self._agent.stop()   # in case a start that was under way started it again
        self.status, self.detail = OFF, "closed"

    @contextlib.asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        """One change at a time: within this process, and between the service and a jarvis command run beside it."""
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path.parent / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)   # closing it lets the lock go

    async def _git(self, *args: str, cwd: Path | None = None, check: bool = True, strip: bool = True,
                   extra: dict[str, str] | None = None, given: bytes | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", *AUTHOR, *args, cwd=str(cwd or self.path), env={**(self._env or {}), **(extra or {})},
            stdin=asyncio.subprocess.PIPE if given is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(given), 120)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            process.kill()   # git does not go on by itself when the work it was doing is called off
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BrainError(f"git {args[0]} timed out") from None
        if check and process.returncode != 0:
            raise BrainError(f"git {args[0]} failed: {err.decode('utf-8', 'replace').strip()[:300]}")
        text = out.decode("utf-8", "replace")
        return text.strip() if strip else text

    async def _git_ok(self, *args: str) -> bool:
        """Did this git command succeed? For questions git answers with its exit code."""
        process = await asyncio.create_subprocess_exec(
            "git", *AUTHOR, *args, cwd=str(self.path), env=self._env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return await asyncio.wait_for(process.wait(), 120) == 0
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return False

    async def start(self) -> None:
        """Clone the notes if they are not here yet, otherwise bring them up to date. Never raises.

        An empty repository on the remote is given its first note and used. Notes kept on this machine only (no
        remote) are made when there are none yet, and move to a remote named later when that remote is still empty."""
        if self._closed:
            return
        async with self._locked():
            try:
                self._env = await self._prepare_env()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                for left in (*self.path.parent.glob(".begin-*"), *self.path.parent.glob(".clone-*")):
                    await asyncio.to_thread(_remove, left)   # what a start cut short left beside the notes
                if (self.path / ".git").is_dir():
                    if not self.remote and self.local and await self._git("config", "--get", "remote.origin.url", check=False):
                        self.status, self.detail = FAILED, (
                            f"{self.path} is a clone of a notes repository the vault no longer names, so it is not used as "
                            f"notes kept on this machine; name it in the vault again, or move it away")
                        return
                    if self.remote:
                        url = await self._git("config", "--get", "remote.origin.url", check=False)
                        if not url:
                            if not await self._remote_empty():
                                self.status, self.detail = FAILED, (
                                    f"{self.path} holds notes kept on this machine, and JARVIS_NOTES_REPO already has "
                                    f"notes of its own; move {self.path} away to use those, or empty that repository")
                                return
                            # Sent by address first: until it is there, no origin is set, so a push that fails leaves
                            # the notes as they were and the next start looks at the remote afresh.
                            await self._git("push", "--", self.remote, "main:main")
                            await self._git("remote", "add", "origin", self.remote)
                            await self._git("fetch", "-q", "origin", "main")
                        elif url != self.remote:
                            self.status, self.detail = FAILED, (
                                f"{self.path} holds a clone of another repository than JARVIS_NOTES_REPO names, so it is "
                                f"neither read nor written; move it away and Jarvis clones the one named")
                            return
                        else:
                            await self._pull(rebase=False)
                elif self.remote:
                    if await self._remote_empty():
                        await self._begin(push=True)
                    else:
                        # Cloned beside its place and moved in when whole, so a clone cut short is never taken for one.
                        scratch = Path(tempfile.mkdtemp(prefix=".clone-", dir=self.path.parent))
                        try:
                            await self._git("clone", "--branch", "main", "--", self.remote, str(scratch / "repo"), cwd=self.path.parent)
                            os.rename(scratch / "repo", self.path)
                        finally:
                            await asyncio.to_thread(_remove, scratch)
                elif self.local:
                    await self._begin(push=False)
                else:
                    self.status, self.detail = OFF, "no notes repository is set up"
                    return
                head = await self._git("rev-parse", "--short", "HEAD")
                self.status, self.detail = READY, f"main at {head}" + ("" if self.remote else " (kept on this machine only)")
            except (BrainError, OSError) as exc:
                if (self.path / ".git").is_dir() and not self._closed:
                    # The local copy is still good for reading; say why it could not be refreshed.
                    self._env = self._env or {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_TERMINAL_PROMPT": "0",
                                              "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
                    self.status, self.detail = READY, f"local copy only: {exc}"[:200]
                else:
                    self.status, self.detail = FAILED, str(exc)[:200]

    async def _pull(self, rebase: bool) -> None:
        """Bring main up to date with the remote. Two histories with nothing in common are never mixed."""
        await self._git("fetch", "-q", "origin", "main")
        if not await self._git_ok("merge-base", "HEAD", "FETCH_HEAD"):
            raise BrainError("the notes here and the notes on the remote have no history in common, so they are not "
                             "mixed; move one of them away")
        if rebase:
            await self._git("rebase", "-q", "FETCH_HEAD")
        else:
            await self._git("merge", "-q", "--ff-only", "FETCH_HEAD")

    async def _remote_empty(self) -> bool:
        """Whether the remote has no branches at all (a repository just made). One without main is an error."""
        heads = await self._git("ls-remote", "--heads", "--", self.remote, cwd=self.path.parent)
        if not heads:
            return True
        if not re.search(r"\trefs/heads/main$", heads, re.M):
            raise BrainError("JARVIS_NOTES_REPO has no branch main; Jarvis keeps its notes on main")
        return False

    async def _begin(self, push: bool) -> None:
        """The notes' first commit, made beside their place and moved in when whole; sent to the remote when there
        is one (which is then empty)."""
        scratch = Path(tempfile.mkdtemp(prefix=".begin-", dir=self.path.parent))
        try:
            repo = scratch / "repo"
            repo.mkdir()
            await self._git("init", "-q", "-b", "main", cwd=repo)
            (repo / "README.md").write_text(FIRST_NOTE, encoding="utf-8")
            await self._git("add", "README.md", cwd=repo)
            await self._git("commit", "-q", "-m", "Jarvis's notes begin", cwd=repo)
            if push:
                await self._git("remote", "add", "origin", self.remote, cwd=repo)
                await self._git("push", "-u", "origin", "main", cwd=repo)
            os.rename(repo, self.path)
        finally:
            await asyncio.to_thread(_remove, scratch)

    # ---- reading -------------------------------------------------------------------------------

    def resolve(self, rel: str) -> Path:
        """A path inside the repository, or an error. Nothing may point outside it or into .git."""
        if not rel or rel.startswith(("/", "~")) or "\\" in rel or "\x00" in rel:
            raise BrainError(f"not a note path: {rel!r}")
        target = (self.path / rel).resolve()
        root = self.path.resolve()
        if root not in target.parents or ".git" in target.relative_to(root).parts:
            raise BrainError(f"not a note path: {rel!r}")
        return target

    def read(self, rel: str) -> str:
        target = self.resolve(rel)
        if not target.is_file():
            raise BrainError(f"no such note: {rel}")
        return target.read_text(encoding="utf-8", errors="replace")

    def notes(self) -> list[str]:
        """Every Markdown file in the repository, as paths inside it."""
        root = self.path.resolve()
        found = []
        for file in sorted(root.rglob("*.md")):
            parts = file.relative_to(root).parts
            if ".git" not in parts:
                found.append("/".join(parts))
        return found

    # ---- writing -------------------------------------------------------------------------------

    def looks_secret(self, text: str) -> bool:
        return bool(SECRET_PATTERNS.search(text)) or self._has_secret(text)

    def _check(self, files: dict[str, str], main: bool) -> None:
        if not files:
            raise BrainError("nothing to write")
        for rel, text in files.items():
            target = self.resolve(rel)
            if target.relative_to(self.path.resolve()).as_posix() != rel:
                raise BrainError(f"not a note path: {rel!r}")   # wiki/../SOUL.md is SOUL.md, and is judged as that
            self._no_links(rel)
            if main and is_protected(rel):
                raise BrainError(f"{rel} changes only through a proposal branch")
            if rel.startswith("raw/") and target.exists():
                raise BrainError(f"{rel} is a raw source and is never edited")
            if self.looks_secret(text):
                raise BrainError(f"{rel} looks as if it holds a secret; nothing was written")

    def _no_links(self, rel: str) -> None:
        """A note is its own file. A link could point a harmless name at a standing file or a raw source."""
        here = self.path
        for part in rel.split("/"):
            here = here / part
            if here.is_symlink():
                raise BrainError(f"{rel} is a link, and nothing is written through a link")

    def read_exact(self, rel: str) -> str:
        """The note exactly as it is stored, for an edit that must not alter anything else in it."""
        target = self.resolve(rel)
        self._no_links(rel)
        if not target.is_file():
            raise BrainError(f"no such note: {rel}")
        raw = target.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BrainError(f"{rel} is not plain UTF-8 text; edit it by hand") from None
        if "\r" in text or "\x00" in text:
            raise BrainError(f"{rel} has Windows line endings or control characters; edit it by hand")
        return text

    @staticmethod
    def _write(root: Path, files: dict[str, str]) -> None:
        for rel, text in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")

    async def save(self, files: dict[str, str], message: str) -> str:
        """Write notes on main, commit, and push when a remote is set. Returns the commit."""
        async with self._locked():
            if not self.ready:
                raise BrainError(f"the notes are not available: {self.detail}")
            self._check(files, main=True)
            self._write(self.path, files)
            await self._git("add", "--", *files)
            if not await self._git("status", "--porcelain", "--", *files):
                return await self._git("rev-parse", "--short", "HEAD")
            await self._git("commit", "-m", message, "--", *files)
            if self.can_push:
                try:
                    await self._pull(rebase=True)
                    await self._git("push", "origin", "main")
                except BrainError:
                    await self._git("rebase", "--abort", check=False)  # the commit stays here; never a half-done rebase
                    raise
            return await self._git("rev-parse", "--short", "HEAD")

    async def _sync(self) -> None:
        """Bring main up to date with GitHub before an edit. Called with the lock held.

        A commit made here earlier that could not be sent (the journal, while GitHub was out of reach) is put on top
        of what is there now. If that cannot be done cleanly, the rebase is undone and the edit is refused, so the
        clone is never left half way.
        """
        if not self.can_push:
            return
        try:
            await self._pull(rebase=True)
        except BrainError as exc:
            await self._git("rebase", "--abort", check=False)
            raise BrainError(f"the notes could not be brought up to date with GitHub, so nothing was changed: {exc}") from None

    async def _clean(self, rel: str) -> None:
        if await self._git("status", "--porcelain", "--", rel):
            raise BrainError(f"{rel} has changes here that are not committed; nothing was changed")

    async def edit(self, rel: str, change: Callable[[str], str], message: str) -> str:
        """Change one note on main: read, change and commit in one step, so that nothing written in between is lost.

        Either the change is on main (and on GitHub, when there is a remote) or nothing has changed at all. Only
        this note is touched, also when something goes wrong. Returns the commit.
        """
        async with self._locked():
            if not self.ready:
                raise BrainError(f"the notes are not available: {self.detail}")
            await self._sync()
            text = self.read_exact(rel)
            await self._clean(rel)
            before = await self._git("rev-parse", "HEAD")
            new = change(text)
            if new == text:
                raise BrainError("that would leave the note as it is; nothing was changed")
            self._check({rel: new}, main=True)
            target = self.resolve(rel)
            try:
                self._write(self.path, {rel: new})
                await self._git("add", "--", rel)
                await self._git("commit", "-m", message, "--", rel)
                if self.can_push:
                    await self._git("push", "origin", "main")
            except (BrainError, OSError) as exc:
                # Undo this one change and nothing else: other notes and other commits stay as they were.
                if await self._git("rev-parse", "HEAD", check=False) != before:
                    await self._git("reset", "--soft", before, check=False)
                await self._git("reset", "-q", "--", rel, check=False)
                target.write_bytes(text.encode("utf-8"))
                raise BrainError(f"the change could not be stored, so nothing was changed: {exc}") from None
            commit = await self._git("rev-parse", "--short", "HEAD")
            self.detail = f"main at {commit}"
            return commit

    async def edit_proposal(self, name: str, rel: str, change: Callable[[str], str], message: str) -> str:
        """Put a change to one file on its proposal branch, on top of whatever is already waiting there.

        Read, change and propose happen in one step. Either the branch holds the change or nothing has changed.
        What the owner put on the branch is kept, and a push from them that lands at the same moment is never
        overwritten. Returns the branch.
        """
        if not PROPOSAL_NAME.match(name):
            raise BrainError(f"not a proposal name: {name!r}")
        branch = f"proposal/{name}"
        async with self._locked():
            if not self.ready:
                raise BrainError(f"the notes are not available: {self.detail}")
            await self._sync()
            on_main = self.read_exact(rel)
            waiting, start, tip = await self._pending(name, rel, on_main)
            base = on_main if waiting is None else waiting
            new = change(base)
            if new == base:
                raise BrainError("that would leave the file as it is; nothing was proposed")
            if new == on_main:
                raise BrainError("that would leave the file as it is on main, so there is nothing to propose; "
                                 "the owner can drop the waiting proposal by deleting its branch")
            before = await self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
            try:
                return await self._propose({rel: new}, name, message, start=start, lease=tip)
            except (BrainError, OSError) as exc:
                if before:
                    await self._git("branch", "-f", branch, before, check=False)
                else:
                    await self._git("branch", "-D", branch, check=False)
                raise BrainError(f"the proposal could not be stored, so nothing was changed: {exc}") from None

    async def _pending(self, name: str, rel: str, on_main: str) -> tuple[str | None, str, str]:
        """(text waiting on the proposal branch or None, where a new proposal starts, the branch's tip on GitHub).

        Called with the lock held. GitHub is asked, so a proposal the owner deleted is gone and one they amended is
        built on as amended. A proposal that waits but was made against an older version of the file is not
        dropped: the edit is refused until the owner merges or deletes it.
        """
        branch = f"proposal/{name}"
        tip = ""
        if self.can_push:
            listed = await self._git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
            if not listed:
                return None, "main", ""  # never pushed, or deleted (merged or rejected) by the owner
            tip = listed.split()[0]
            await self._git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            ref = f"refs/remotes/origin/{branch}"
        else:
            ref = f"refs/heads/{branch}"
            if not await self._git_ok("rev-parse", "--verify", "--quiet", ref):
                return None, "main", ""
        if await self._git_ok("merge-base", "--is-ancestor", ref, "main"):
            return None, "main", tip  # merged already: start again from main
        base = await self._git("merge-base", "main", ref)
        try:
            before = await self._git("show", f"{base}:{rel}", strip=False)
        except BrainError:
            before = None
        try:
            waiting = await self._git("show", f"{ref}:{rel}", strip=False)
        except BrainError:
            waiting = None
        if waiting is None or before == waiting:
            return None, ref, tip  # the branch waits with other changes; this file's change goes on top of it
        if before != on_main:
            raise BrainError(f"{rel} has changed on main since the proposal on {branch} was made, so it was not "
                             f"changed again; the owner should merge or delete {branch} first")
        return waiting, ref, tip

    async def save_as_owner(
        self, files: dict[str, str], message: str, name: str, unchanged: Callable[[], Awaitable[None] | None] | None = None
    ) -> tuple[str, dict[str, str]]:
        """Write what the owner typed in the notes window, on main, in the owner's name.

        Returns the commit and each file's text as it stands after the save (read under the same lock, so a change
        merged in from the remote is part of it). Either the save is on the remote too, or nothing has changed here:
        a save that cannot be sent is undone, so the owner can simply save again.

        The proposal rule guards against Jarvis's own writes, so it does not apply here: the owner may change the
        standing files directly. Sources stay immutable and nothing that looks like a secret is committed.
        """
        author = re.sub(r"[<>\r\n]", "", name).strip() or "Owner"
        async with self._locked():
            if not self.ready:
                raise BrainError(f"the notes are not available: {self.detail}")
            if unchanged is not None:
                unchanged()  # raises when the note is no longer what the window started from; checked under the lock
            self._check(files, main=False)
            before = await self._git("rev-parse", "HEAD")
            kept = {rel: (self.resolve(rel).read_bytes() if self.resolve(rel).is_file() else None) for rel in files}
            stage = "written"
            try:
                self._write(self.path, files)
                await self._git("add", "--", *files)
                if await self._git("status", "--porcelain", "--", *files):
                    await self._git("commit", f"--author={author} <owner@jarvis.invalid>", "-m", message, "--", *files)
                    if self.can_push:
                        stage = "sent"
                        await self._pull(rebase=True)
                        await self._git("push", "origin", "main")
            except (BrainError, OSError) as exc:
                await self._git("rebase", "--abort", check=False)
                if await self._git("rev-parse", "HEAD", check=False) != before:
                    # Back to a clean tree without the owner's commit. What the remote added meanwhile stays: it is on
                    # the remote anyway, and dropping it would leave it half in the index.
                    upstream = "refs/remotes/origin/main"
                    back = upstream if (self.can_push and await self._git_ok("merge-base", "--is-ancestor", before, upstream)) else before
                    await self._git("reset", "-q", "--keep", back, check=False)
                else:
                    await self._git("reset", "-q", "--", *files, check=False)
                    for rel, data in kept.items():
                        target = self.resolve(rel)
                        if data is None:
                            target.unlink(missing_ok=True)
                        else:
                            target.write_bytes(data)
                if stage == "sent":
                    reason = "the notes could not be brought up to date with the remote or sent to it"   # git's words name the remote
                elif isinstance(exc, OSError):
                    reason = exc.strerror or "the file could not be written"
                else:
                    reason = str(exc)
                raise BrainError(f"nothing was saved: {reason}") from None
            commit = await self._git("rev-parse", "--short", "HEAD")
            self.detail = f"main at {commit}"
            return commit, {rel: self.read(rel) for rel in files}

    async def propose(self, name: str, files: dict[str, str], message: str) -> str:
        """Put a change on a proposal branch for the owner to review. The working tree stays on main."""
        if not PROPOSAL_NAME.match(name):
            raise BrainError(f"not a proposal name: {name!r}")
        async with self._locked():
            if not self.ready:
                raise BrainError(f"the notes are not available: {self.detail}")
            return await self._propose(files, name, message)

    async def _propose(self, files: dict[str, str], name: str, message: str, start: str = "main", lease: str | None = None) -> str:
        """Called with the lock held. With `lease` (the branch's tip on the remote, or "" when it has none) the push
        fails rather than overwrite anything pushed there since; without it the branch is replaced.

        The commit is built from git's objects alone, never by checking the branch out: a branch may hold a link
        where a note should be, and a file written through it could land anywhere."""
        branch = f"proposal/{name}"
        self._check(files, main=False)
        base = await self._git("rev-parse", "--verify", f"{start}^{{commit}}")
        index = Path(tempfile.mkdtemp(prefix=".proposal-", dir=self.path.parent)) / "index"
        extra = {"GIT_INDEX_FILE": str(index)}
        try:
            await self._git("read-tree", base, extra=extra)
            for rel, text in files.items():
                parts = rel.split("/")
                for depth in range(1, len(parts) + 1):
                    listed = await self._git("ls-tree", base, "--", "/".join(parts[:depth]))
                    if listed.startswith("120000 "):
                        raise BrainError(f"{rel} is a link on {branch}, and nothing is written through a link")
                body = text if text.endswith("\n") else text + "\n"
                blob = await self._git("hash-object", "-w", "--stdin", given=body.encode("utf-8"))
                await self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", extra=extra)
            tree = await self._git("write-tree", extra=extra)
            commit = await self._git("commit-tree", tree, "-p", base, "-m", message)
        finally:
            await asyncio.to_thread(_remove, index.parent)
        await self._git("update-ref", f"refs/heads/{branch}", commit)
        if self.can_push:
            force = "--force" if lease is None else f"--force-with-lease=refs/heads/{branch}:{lease}"
            await self._git("push", force, "origin", f"{branch}:refs/heads/{branch}")
        return branch


def _remove(folder: Path) -> None:
    import shutil
    shutil.rmtree(folder, ignore_errors=True)
