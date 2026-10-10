"""jarvis-fsd: Jarvis's hands on its own machine's files.

Runs as root (jarvis-fs.service) and answers one user only, the one Jarvis's service runs as, on a Unix socket.
Jarvis can list, read, find, write, make folders, delete, and undo what it changed. Every change keeps a copy of
what was there before, so each one can be undone (jarvis-fsd keeps the last thousand).

Never read or changed: private keys and the vault (the brain's TLS keys, the vault identity sealed and unlocked,
the .env files), the system's password files, SSH, GnuPG and other key stores, jarvis-fsd's own history, and
/proc, /sys and /dev. A file that holds a private key is not read wherever it is. A protected file is known by
its place and by its inode, so a link, a hard link or a bind mount does not lead around it.

Read, never changed: what keeps those guards in place, so that no write can switch them off at the next restart
or update: Jarvis's code and its Python environment, jarvis-fsd and the units that start it and Jarvis, Jarvis's
commands, sops, the site settings, the release keys and pinned certificates, and the audit log.

This file uses Python's own modules only: the installer copies it to /usr/local/sbin/jarvis-fsd, and root runs
it with -I, away from Jarvis's environment.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fnmatch
import glob
import grp
import hashlib
import json
import os
import pwd
import socket
import stat
import struct
import sys
import tempfile
import time

MAX_REQUEST = 2 * 1024 * 1024
MAX_WRITE = 1024 * 1024          # what one write may hold
MAX_READ = 256 * 1024            # what one read returns at most
MAX_LINE = 8192                  # a read is widened to whole lines by at most this much on each side
KEEP_LIMIT = 8 * 1024 * 1024     # a file larger than this is not changed: no copy of it could be kept for undo
SCAN_LIMIT = 64 * 1024 * 1024    # a file larger than this is not read: it could not be checked for keys
KEEP_ENTRIES = 1000
KEEP_BYTES = 512 * 1024 * 1024
MAX_LIST = 500
MAX_FOUND = 200
MAX_WALKED = 20000
FIND_SECONDS = 20
FIND_BYTES = 256 * 1024 * 1024
INODES_FOR = 10                  # seconds the inodes of what is protected are kept before they are looked up again
# Marks of key material: PEM and OpenSSH private keys, PGP secret keys, age, GnuPG's own store, PuTTY, a PEM block
# in base64 (as in kubeconfig files), WireGuard and NetBird keys.
KEY_MARKS = (b"PRIVATE KEY", b"AGE-SECRET-KEY-", b"(private-key", b"(protected-private-key", b"PuTTY-User-Key-File",
             b"LS0tLS1CRUdJTi", b"PrivateKey")
# Paths as seen on the machine. A pattern ending in /** covers the folder and everything in it.
PROTECTED = (
    "/proc/**", "/sys/**", "/dev/**",
    "/etc/jarvis/tls/*.key", "/etc/jarvis/tls/*.key.new",
    "/etc/jarvis/secrets/**", "/var/lib/jarvis-key/**", "/run/jarvis/**",
    "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-", "/etc/ssh/ssh_host_*_key",
    "/root/.ssh/**", "/home/*/.ssh/**", "/root/.gnupg/**", "/home/*/.gnupg/**", "/etc/ssl/private/**",
    "/etc/wireguard/**", "/etc/netbird/**", "/root/.config/sops/**", "/etc/credstore/**", "/etc/credstore.encrypted/**",
    "/run/credentials/**", "/var/lib/systemd/credential.secret",
    "/var/lib/jarvis-fs/**", "/run/jarvis-fs/**",
)
READ_ONLY = (
    "/opt/jarvis/**", "/opt/jarvis-venv/**",
    "/usr/local/sbin/jarvis-fsd", "/usr/sbin/jarvis-unlock", "/usr/bin/jarvis", "/usr/bin/update", "/usr/local/bin/sops",
    "/etc/systemd/system/jarvis.service", "/etc/systemd/system/jarvis.service.d/**",
    "/etc/systemd/system/jarvis-fs.service", "/etc/systemd/system/jarvis-fs.service.d/**",
    "/etc/systemd/system/jarvis-firewall.service", "/etc/systemd/system/jarvis-firewall.service.d/**",
    "/etc/jarvis/site.env", "/etc/jarvis/allowed_signers", "/etc/jarvis/trust/**", "/etc/jarvis/release",
    "/etc/jarvis/release.pending", "/var/lib/jarvis/audit/**",
)


class Refused(Exception):
    """Why a request was not done, in words for Jarvis."""


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    parts = name.split("/")
    ancestors = ["/".join(parts[:i]) or "/" for i in range(2, len(parts) + 1)]   # /etc, /etc/jarvis, ..., name
    for pattern in patterns:
        if pattern.endswith("/**"):
            if any(fnmatch.fnmatchcase(place, pattern[:-3]) for place in ancestors):
                return True
        elif fnmatch.fnmatchcase(name, pattern):
            return True
    return False


class Files:
    def __init__(self, root: str = "/", history: str = "/var/lib/jarvis-fs/history", protect: tuple[str, ...] = ()):
        self.root = os.path.realpath(root)
        self.history = history
        self.protected = PROTECTED + tuple(p.rstrip("/") + "/**" for p in protect if p)
        self.read_only = READ_ONLY
        self._inodes: tuple = (float("-inf"), set(), set(), set(), set())
        os.makedirs(self.history, mode=0o700, exist_ok=True)
        os.chmod(self.history, 0o700)

    # ------------------------------------------------------------------ paths

    def shown(self, real: str) -> str:
        """The machine's own name for a path under the root."""
        rel = os.path.relpath(real, self.root)
        return "/" if rel == "." else "/" + rel

    def locate(self, path: object, must_exist: bool = True) -> str:
        """The real path a request names, links followed, never outside the root."""
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096:
            raise Refused("give a full path, starting with /")
        real = os.path.realpath(os.path.join(self.root, path.lstrip("/")))
        if real != self.root and not real.startswith(self.root.rstrip(os.sep) + os.sep):   # the root "/" included
            raise Refused(f"{path} leads outside this machine's files")
        if must_exist and not os.path.lexists(real):
            raise Refused(f"{path} does not exist")
        return real

    def _places(self, patterns: tuple[str, ...], inside: bool) -> tuple[set, set]:
        """The inodes of what the patterns name: folders (anything under them counts) and files; with inside, also
        every file in those folders (another name for one of them, a hard link, is then known too)."""
        folders, files = set(), set()
        for pattern in patterns:
            if pattern.startswith(("/proc", "/sys", "/dev")):
                continue
            base = pattern[:-3] if pattern.endswith("/**") else pattern
            for found in glob.glob(os.path.join(self.root, base.lstrip("/"))):
                try:
                    info = os.stat(found)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    folders.add((info.st_dev, info.st_ino))
                    if not pattern.endswith("/**") or not inside:
                        continue
                    for top, _, names in os.walk(found):
                        for name in names:
                            try:
                                inner = os.stat(os.path.join(top, name))
                            except OSError:
                                continue
                            files.add((inner.st_dev, inner.st_ino))
                else:
                    files.add((info.st_dev, info.st_ino))
        return folders, files

    def _known(self) -> tuple[set, set, set, set]:
        when, *places = self._inodes
        if time.monotonic() - when > INODES_FOR:
            places = [*self._places(self.protected, True), *self._places(self.read_only, False)]
            self._inodes = (time.monotonic(), *places)
        return tuple(places)  # type: ignore[return-value]

    def _by_inode(self, real: str, folders: set, files: set) -> bool:
        """Whether the path is one of those files, or lies in one of those folders, under any name."""
        try:
            info = os.stat(real)
            if (info.st_dev, info.st_ino) in files or (info.st_dev, info.st_ino) in folders:
                return True
        except OSError:
            pass
        place = os.path.dirname(real)
        while True:
            try:
                info = os.stat(place)
                if (info.st_dev, info.st_ino) in folders:
                    return True
            except OSError:
                pass
            if place == self.root or place == os.path.dirname(place):
                return False
            place = os.path.dirname(place)

    def is_protected(self, real: str) -> bool:
        if _matches(self.shown(real), self.protected):
            return True
        folders, files, _, _ = self._known()
        return self._by_inode(real, folders, files)

    def is_read_only(self, real: str) -> bool:
        if _matches(self.shown(real), self.read_only):
            return True
        _, _, folders, files = self._known()
        return self._by_inode(real, folders, files)

    def guard(self, real: str, what: str) -> None:
        if self.is_protected(real):
            raise Refused(f"{self.shown(real)} is kept from Jarvis (keys, the vault, password files, /proc, /sys, "
                          f"/dev); it cannot be {what}")

    def guard_change(self, real: str, what: str) -> None:
        self.guard(real, what)
        if self.is_read_only(real):
            raise Refused(f"{self.shown(real)} keeps Jarvis's guards in place (its code, jarvis-fsd, their units, the "
                          f"settings, the release keys, the audit log); it can be read but not {what}")

    # ------------------------------------------------------------------ reading

    def list(self, path: object) -> dict:
        real = self.locate(path)
        if not os.path.isdir(real):
            raise Refused(f"{path} is not a folder")
        self.guard(real, "listed")
        entries = []
        names = sorted(os.listdir(real))
        for name in names[:MAX_LIST]:
            full = os.path.join(real, name)
            try:
                info = os.lstat(full)
            except OSError:
                continue
            kind = ("link" if stat.S_ISLNK(info.st_mode) else "folder" if stat.S_ISDIR(info.st_mode)
                    else "file" if stat.S_ISREG(info.st_mode) else "other")
            entry = {"name": name, "type": kind, "size": info.st_size, "mode": oct(stat.S_IMODE(info.st_mode)),
                     "owner": _user(info.st_uid) + ":" + _group(info.st_gid), "modified": int(info.st_mtime)}
            if kind == "link":
                entry["to"] = os.readlink(full)
            if self.is_protected(full):
                entry["protected"] = True
            elif self.is_read_only(full):
                entry["read_only"] = True
            entries.append(entry)
        return {"path": self.shown(real), "entries": entries, "more": max(0, len(names) - MAX_LIST)}

    def read(self, path: object, offset: object = 0, length: object = 65536) -> dict:
        real = self.locate(path)
        self.guard(real, "read")
        try:
            start = max(0, int(offset))
            size = max(1, min(MAX_READ, int(length)))
        except (TypeError, ValueError, OverflowError):
            raise Refused("offset and length are numbers") from None
        with _open_regular(real) as handle:
            whole = os.fstat(handle.fileno()).st_size
            if whole > SCAN_LIMIT:
                raise Refused(f"{path} is larger than {SCAN_LIMIT} bytes; it cannot be checked for keys, so it is not read")
            data = handle.read()
        if _holds_key(data):
            raise Refused(f"{path} holds a private key; it is not read")
        if b"\x00" in data[:8192]:
            return {"path": self.shown(real), "size": whole, "binary": True, "note": "a binary file: its bytes are not shown"}
        # Whole lines only: a value of the vault on a line is then never cut in two, and so is always masked.
        begin = min(start, whole)
        back = data.rfind(b"\n", max(0, begin - MAX_LINE), begin)
        begin = 0 if begin == 0 else (back + 1 if back >= 0 else max(0, begin - MAX_LINE))
        end = min(whole, start + size)
        ahead = data.find(b"\n", end, end + MAX_LINE) if end < whole else -1
        end = whole if end >= whole else (ahead + 1 if ahead >= 0 else min(whole, end + MAX_LINE))
        part = data[begin:end]
        try:
            text = part.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = part.decode("utf-8", "replace")
            encoding = "not utf-8 (shown with replacement characters; it cannot be written back)"
        return {"path": self.shown(real), "size": whole, "offset": begin, "next_offset": end if end < whole else None,
                "text": text, "encoding": encoding, "sha256": hashlib.sha256(data).hexdigest()}

    def find(self, path: object, name: object = "*", contains: object = "") -> dict:
        real = self.locate(path)
        self.guard(real, "searched")
        pattern = str(name or "*")
        needle = str(contains or "").encode("utf-8", "replace")
        found: list[dict] = []
        walked, spent, began = 0, 0, time.monotonic()
        for top, folders, files in os.walk(real):   # os.walk does not follow links to folders
            folders[:] = sorted(f for f in folders if not self.is_protected(os.path.join(top, f)))
            for file in sorted(files):
                walked += 1
                if walked > MAX_WALKED or len(found) >= MAX_FOUND or spent > FIND_BYTES or time.monotonic() - began > FIND_SECONDS:
                    return {"path": self.shown(real), "found": found, "stopped": "the search was cut short; search a smaller folder"}
                full = os.path.join(top, file)
                if not fnmatch.fnmatch(file, pattern):
                    continue
                try:
                    info = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or self.is_protected(full):
                    continue   # links, pipes and devices are not looked into
                entry = {"path": self.shown(full)}
                if needle:
                    spent += info.st_size
                    line = _line_with(full, needle)
                    if line is None:
                        continue
                    entry.update(line)
                found.append(entry)
        return {"path": self.shown(real), "found": found}

    # ------------------------------------------------------------------ changing

    def write(self, path: object, content: object, mode: object = None, expect: object = None) -> dict:
        if not isinstance(content, str):
            raise Refused("content is text")
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            raise Refused("that text has characters that are not Unicode text") from None
        if len(data) > MAX_WRITE:
            raise Refused(f"one write holds at most {MAX_WRITE} bytes")
        if _holds_key(data):
            raise Refused("that text holds a private key; Jarvis does not write keys")
        real = self.locate(path, must_exist=False)
        parent = os.path.dirname(real)
        if not os.path.isdir(parent):
            raise Refused(f"the folder {self.shown(parent)} does not exist; make it first (fs_mkdir)")
        self.guard_change(parent, "changed")
        self.guard_change(real, "changed")
        existed = os.path.lexists(real)
        if existed and not os.path.isfile(real):
            raise Refused(f"{path} is not a file")
        before = self._before(real) if existed else None
        if before is not None and not before["utf8"]:
            raise Refused(f"{path} is not UTF-8 text; it is not written over")
        # A version to expect guards what is there; for a new file there is nothing to lose, so it does not count.
        if expect and before is not None and before["sha256"] != expect:
            raise Refused(f"{path} changed since it was read; read it again first")
        if before is not None:
            info = os.stat(real)
            uid, gid, perm = info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)
        else:
            owner = os.stat(parent)   # a new file belongs to whoever owns its folder
            uid, gid, perm = owner.st_uid, owner.st_gid, 0o644
        if mode is not None:
            try:
                perm = int(str(mode), 8)
            except ValueError:
                raise Refused("mode is an octal number such as 644") from None
            if perm & ~0o7777:
                raise Refused("mode is an octal number such as 644")
        perm &= ~0o6000   # set-user and set-group modes are never given out
        entry = self._keep("write", real, before)
        try:
            _replace(real, data, uid, gid, perm)
        except BaseException:
            self._forget(entry)
            raise
        entry["after"] = hashlib.sha256(data).hexdigest()
        self._save(entry)
        return {"done": True, "path": self.shown(real), "bytes": len(data), "created": not existed,
                "change": entry["id"], "sha256": entry["after"]}

    def mkdir(self, path: object) -> dict:
        real = self.locate(path, must_exist=False)
        if os.path.lexists(real):
            raise Refused(f"{path} already exists")
        parent = os.path.dirname(real)
        if not os.path.isdir(parent):
            raise Refused(f"the folder {self.shown(parent)} does not exist; make it first")
        self.guard_change(parent, "changed")
        self.guard_change(real, "made")
        owner = os.stat(parent)
        entry = self._keep("mkdir", real, None)
        try:
            os.mkdir(real, 0o755)
            os.chown(real, owner.st_uid, owner.st_gid)
            os.chmod(real, 0o755)
        except BaseException:
            self._forget(entry)
            raise
        entry["after"] = "folder"
        self._save(entry)
        return {"done": True, "path": self.shown(real), "change": entry["id"]}

    def delete(self, path: object) -> dict:
        if not isinstance(path, str):
            raise Refused("give a full path, starting with /")
        # A link is deleted itself, not what it points to.
        parent = self.locate(os.path.dirname(path.rstrip("/")) or "/")
        real = os.path.join(parent, os.path.basename(path.rstrip("/")))
        if not os.path.lexists(real):
            raise Refused(f"{path} does not exist")
        self.guard_change(parent, "changed")
        self.guard_change(real, "deleted")
        self.guard_change(self.locate(path), "deleted")
        before = self._state_of(real)
        if os.path.isdir(real) and not os.path.islink(real) and os.listdir(real):
            raise Refused(f"{path} is a folder with things in it; delete them first")
        if not (os.path.islink(real) or os.path.isdir(real) or os.path.isfile(real)):
            raise Refused(f"{path} is not a file, link or folder")
        entry = self._keep("delete", real, before)
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                os.rmdir(real)
            else:
                os.unlink(real)
        except BaseException:
            self._forget(entry)
            raise
        entry["after"] = None
        self._save(entry)
        return {"done": True, "path": self.shown(real), "change": entry["id"]}

    def undo(self, change: object) -> dict:
        entry = self._entry(change)
        if entry.get("undone"):
            raise Refused(f"change {entry['id']} was already undone")
        if entry.get("after") == "pending":
            raise Refused(f"change {entry['id']} was never finished; there is nothing to undo")
        real = self.locate(entry["path"], must_exist=False)
        if real != os.path.join(self.root, entry["path"].lstrip("/")).rstrip("/"):
            raise Refused(f"{entry['path']} now leads somewhere else; it is not touched")
        self.guard_change(os.path.dirname(real), "changed")
        self.guard_change(real, "changed")
        now = _state(real)
        if now != entry["after"]:
            raise Refused(f"{entry['path']} changed again after change {entry['id']}; undo the later changes first")
        prior = entry.get("before")
        if prior is None and os.path.isdir(real) and not os.path.islink(real) and os.listdir(real):
            raise Refused(f"{entry['path']} has things in it now; delete them first")
        undo = self._keep("undo", real, self._state_of(real) if os.path.lexists(real) else None)
        undo["undoes"] = entry["id"]
        try:
            if os.path.lexists(real):
                if os.path.isdir(real) and not os.path.islink(real):
                    os.rmdir(real)
                elif prior is None or "link" in prior or prior.get("folder"):
                    os.unlink(real)
            if prior is None:
                pass
            elif "link" in prior:
                os.symlink(prior["link"], real)
            elif prior.get("folder"):
                os.mkdir(real, 0o755)
                os.chown(real, prior.get("uid", 0), prior.get("gid", 0))
                os.chmod(real, int(prior.get("mode", "0o755"), 8))
            else:
                with open(os.path.join(self.history, prior["data"]), "rb") as kept:
                    data = kept.read()
                _replace(real, data, prior["uid"], prior["gid"], int(prior["mode"], 8) & ~0o6000)
        except BaseException:
            self._forget(undo)
            raise
        undo["after"] = _state(real)
        self._save(undo)
        entry["undone"] = undo["id"]
        self._save(entry)
        return {"done": True, "path": entry["path"], "undid": entry["id"], "change": undo["id"]}

    def changes(self, limit: object = 20) -> dict:
        try:
            count = max(1, min(100, int(limit)))
        except (TypeError, ValueError, OverflowError):
            count = 20
        names = sorted((n for n in os.listdir(self.history) if n.endswith(".json")), reverse=True)[:count]
        out = []
        for name in names:
            entry = self._entry(name[:-5])
            out.append({k: entry[k] for k in ("id", "what", "path", "time") if k in entry}
                       | ({"undone": entry["undone"]} if entry.get("undone") else {})
                       | ({"undoes": entry["undoes"]} if entry.get("undoes") else {})
                       | ({"unfinished": True} if entry.get("after") == "pending" else {}))
        return {"changes": out}

    # ------------------------------------------------------------------ history

    def _state_of(self, real: str) -> dict:
        """What is at a path, as a history entry keeps it to put it back."""
        if os.path.islink(real):
            return {"link": os.readlink(real)}
        info = os.stat(real)
        if os.path.isdir(real):
            return {"folder": True, "uid": info.st_uid, "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode))}
        return self._before(real)

    def _before(self, real: str) -> dict:
        with _open_regular(real) as handle:
            info = os.fstat(handle.fileno())
            if info.st_size > KEEP_LIMIT:
                raise Refused(f"{self.shown(real)} is larger than {KEEP_LIMIT} bytes; no copy of it could be kept to undo a change")
            data = handle.read()
        if _holds_key(data):
            raise Refused(f"{self.shown(real)} holds a private key; it is not changed")
        try:
            data.decode("utf-8")
            utf8 = True
        except UnicodeDecodeError:
            utf8 = False
        return {"data": data, "sha256": hashlib.sha256(data).hexdigest(), "uid": info.st_uid, "gid": info.st_gid,
                "mode": oct(stat.S_IMODE(info.st_mode)), "utf8": utf8}

    def _keep(self, what: str, real: str, before: dict | None) -> dict:
        """Starts a history entry, saved before the change is made: a change is never made without its record."""
        number = self._next()
        entry = {"id": number, "what": what, "path": self.shown(real), "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "after": "pending"}
        if before is not None and "data" in before:
            name = f"{number}.data"
            fd = os.open(os.path.join(self.history, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(before["data"])
            entry["before"] = {k: v for k, v in before.items() if k not in ("data", "utf8")} | {"data": name}
        else:
            entry["before"] = before
        self._save(entry)
        self._trim()
        return entry

    def _forget(self, entry: dict) -> None:
        for suffix in (".json", ".data"):
            try:
                os.unlink(os.path.join(self.history, f"{entry['id']}{suffix}"))
            except FileNotFoundError:
                pass

    def _next(self) -> str:
        numbers = [int(n.split(".")[0]) for n in os.listdir(self.history) if n.split(".")[0].isdigit()]
        return f"{(max(numbers) + 1) if numbers else 1:08d}"

    def _trim(self) -> None:
        entries = sorted(n[:-5] for n in os.listdir(self.history) if n.endswith(".json"))
        sizes = {}
        for number in entries:
            try:
                sizes[number] = os.path.getsize(os.path.join(self.history, f"{number}.data"))
            except OSError:
                sizes[number] = 0
        total = sum(sizes.values())
        while entries and (len(entries) > KEEP_ENTRIES or total > KEEP_BYTES):
            old = entries.pop(0)
            total -= sizes[old]
            self._forget({"id": old})

    def _save(self, entry: dict) -> None:
        path = os.path.join(self.history, f"{entry['id']}.json")
        fd, tmp = tempfile.mkstemp(dir=self.history)
        with os.fdopen(fd, "w") as handle:
            json.dump(entry, handle)
        os.replace(tmp, path)

    def _entry(self, change: object) -> dict:
        name = str(change or "").strip()
        if not name.isdigit() or len(name) > 12:
            raise Refused("name a change by its number (fs_changes lists them)")
        try:
            with open(os.path.join(self.history, f"{int(name):08d}.json")) as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise Refused(f"there is no change {name} (only the latest are kept)") from None

    # ------------------------------------------------------------------ requests

    CHANGES = ("write", "mkdir", "delete", "undo", "changes")

    def handle(self, request: dict) -> dict:
        op = request.get("op")
        try:
            if op == "list":
                return self.list(request.get("path"))
            if op == "read":
                return self.read(request.get("path"), request.get("offset", 0), request.get("length", 65536))
            if op == "find":
                return self.find(request.get("path"), request.get("name", "*"), request.get("contains", ""))
            if op == "write":
                return self.write(request.get("path"), request.get("content"), request.get("mode"), request.get("expect"))
            if op == "mkdir":
                return self.mkdir(request.get("path"))
            if op == "delete":
                return self.delete(request.get("path"))
            if op == "undo":
                return self.undo(request.get("change"))
            if op == "changes":
                return self.changes(request.get("limit", 20))
            return {"error": f"unknown request '{op}'"}
        except Refused as exc:
            return {"error": str(exc)}
        except PermissionError as exc:
            return {"error": f"not allowed by the system: {exc.strerror}"}
        except OSError as exc:
            return {"error": f"{exc.strerror or type(exc).__name__}"}
        except Exception as exc:   # whatever a request holds, it gets an answer
            return {"error": f"the request could not be done ({type(exc).__name__})"}


class _open_regular:
    """Opens a regular file to read without following a final link or waiting on a pipe or a device."""

    def __init__(self, real: str):
        self.real = real

    def __enter__(self):
        try:
            fd = os.open(self.real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENXIO):   # a link after all; a socket
                raise Refused(f"{self.real} is not a file") from None
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise Refused("that is not a file")
        self.handle = os.fdopen(fd, "rb")
        return self.handle

    def __exit__(self, *exc):
        self.handle.close()


def _holds_key(data: bytes) -> bool:
    return any(mark in data for mark in KEY_MARKS)


def _digest(real: str) -> str:
    with _open_regular(real) as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _state(real: str) -> object:
    """What is at a path now, as a history entry says it: a file's digest, "folder", a link, or None."""
    if os.path.islink(real):
        return {"link": os.readlink(real)}
    if os.path.isdir(real):
        return "folder"
    if os.path.isfile(real):
        return _digest(real)
    return None


def _replace(real: str, data: bytes, uid: int, gid: int, perm: int) -> None:
    """Writes the file whole or not at all: a new file beside it, then put in its place."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".jarvis-fsd-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(tmp, uid, gid)
        os.chmod(tmp, perm)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _line_with(real: str, needle: bytes) -> dict | None:
    try:
        with _open_regular(real) as handle:
            if os.fstat(handle.fileno()).st_size > KEEP_LIMIT:
                return None
            data = handle.read()
    except (OSError, Refused):
        return None
    if b"\x00" in data[:8192] or _holds_key(data):
        return None
    at = data.find(needle)
    if at < 0:
        return None
    number = data.count(b"\n", 0, at) + 1
    start = data.rfind(b"\n", 0, at) + 1
    end = data.find(b"\n", at)
    return {"line": number, "text": data[start:end if end >= 0 else len(data)][:300].decode("utf-8", "replace")}


def _user(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


async def serve(files: Files, path: str, allowed: set[int], group: int | None) -> None:
    lock = asyncio.Lock()

    async def one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            if peer_uid(sock) not in allowed:
                writer.write(json.dumps({"error": "not for this user"}).encode() + b"\n")
                return
            line = await asyncio.wait_for(reader.readuntil(b"\n"), 30)
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                request = None
            if not isinstance(request, dict):
                answer = {"error": "a request is one JSON object on one line"}
            elif request.get("op") in Files.CHANGES:
                async with lock:   # one change at a time, so the history stays in order
                    answer = await asyncio.to_thread(files.handle, request)
            else:
                answer = await asyncio.to_thread(files.handle, request)
            writer.write(json.dumps(answer).encode() + b"\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o750, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(one, path, limit=MAX_REQUEST)
    if group is not None:
        os.chown(folder, 0, group)
        os.chown(path, 0, group)
    os.chmod(folder, 0o750)
    os.chmod(path, 0o660)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-fsd", description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default="/run/jarvis-fs/socket")
    parser.add_argument("--user", default="jarvis", help="the one user answered")
    parser.add_argument("--root", default="/")
    parser.add_argument("--history", default="/var/lib/jarvis-fs/history")
    parser.add_argument("--protect", action="append", default=[], help="another folder kept from Jarvis")
    args = parser.parse_args(argv)
    try:
        user = pwd.getpwnam(args.user)
    except KeyError:
        print(f"jarvis-fsd: there is no user {args.user}", file=sys.stderr)
        return 1
    files = Files(args.root, args.history, tuple(args.protect))
    asyncio.run(serve(files, args.socket, {user.pw_uid}, user.pw_gid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
