"""Jarvis's secrets: read from the folder chosen at install, held in this process only.

Two ways to keep them (JARVIS_SECRETS_MODE):

  sops    Files named *.enc.env, encrypted with sops for an age identity of this brain. The identity rests on
          the disk only sealed with the owner's passphrase; after every restart the owner unlocks it
          (jarvis-unlock, as root), which puts it in /run, a memory-backed folder a restart empties. Until then
          Jarvis is locked: it runs, says so, and has no lab tools.
  plain   Files named *.env that only root and the jarvis user can read. For a lab without sops.

Values are never written anywhere by Jarvis, never shown to the model, and masked wherever they would leave in a
tool's answer or a log. Nothing sops says is passed on either: what it prints about a file it cannot open may
quote the file, so only fixed sentences chosen by its exit code are.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

NONE = "none"          # no secrets file at all
LOCKED = "locked"      # sops files, but the identity is not unlocked
UNLOCKED = "unlocked"  # every file read
ERROR = "error"        # a file that could not be read

MIN_MASKED = 8                     # values shorter than this are not masked (they would mask ordinary words)
# Values that only say where a system is, which tools report in their answers anyway; never masked.
PLAIN = ("_HOST", "_HOSTS", "_SERVERS", "_LAB_NAME")
RETRY_AFTER = 30                   # seconds before a file that could not be read is tried again unchanged
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_IDENTITY = re.compile(r"^AGE-SECRET-KEY-1[0-9A-Z]{58}$")
_RECIPIENT = re.compile(r"^sops_age__list_\d+__map_recipient=(age1[0-9a-z]{58})$", re.MULTILINE)
# What sops's exit codes mean (sops' cmd/sops/codes), said in Jarvis's words.
_SOPS_SAYS = {
    128: "the key to open it could not be got with this brain's identity",
    25: "a value in it cannot be decrypted (a line added by hand?); open and save it with sops edit",
    24: "its checksum cannot be decrypted",
    51: "its checksum does not match: it was changed outside sops",
    52: "it has no checksum: it is not a whole sops file",
    2: "sops cannot read it",
    1: "sops could not open it",
}


def parse_dotenv(text: str) -> dict[str, str]:
    """KEY=value lines, as sops writes them and as a person writes a plain file. sops' own lines are left out."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if not _NAME.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value.replace("\\n", "\n")
    return values


def recipients(path: Path) -> list[str]:
    """The age public keys a sops file is encrypted for. They are written in the file in clear."""
    try:
        return _RECIPIENT.findall(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def identity_ok(text: str) -> bool:
    """One age identity and nothing else. (A public key where the private one belongs was seen once.)"""
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return len(lines) == 1 and _IDENTITY.match(lines[0]) is not None


class VaultError(Exception):
    """Why a file could not be read. Never holds a value."""


class Vault:
    def __init__(self, env_dir: Path, mode: str = "sops", identity: Path = Path("/run/jarvis/age.key"),
                 sops: str = "/usr/local/bin/sops", run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 watch: tuple[Path, ...] = ()):
        self.env_dir = Path(env_dir)
        self.mode = mode
        self.identity = Path(identity)
        self.sops = sops
        self.watch = tuple(watch)   # other files whose change should be noticed (a certificate the tools use)
        self._run = run
        self.status = NONE
        self.detail = "not read yet"
        self.files: list[str] = []
        self._values: dict[str, str] = {}
        self._seen: tuple | None = None
        self._retry_at = 0.0
        self._pending: tuple | None = None

    # ------------------------------------------------------------------ reading

    def _files(self) -> list[Path]:
        pattern = "*.enc.env" if self.mode == "sops" else "*.env"
        try:
            found = sorted(self.env_dir.glob(pattern))
        except OSError:
            return []
        return [path for path in found if path.is_file() and (self.mode == "sops" or not path.name.endswith(".enc.env"))]

    def _state(self, files: list[Path]) -> tuple:
        """What the outcome depends on. While it stays the same, nothing is read again."""
        def stamp(path: Path) -> tuple:
            try:
                info = path.stat()
                return (str(path), info.st_mtime_ns, info.st_size, info.st_ino)
            except OSError:
                return (str(path), None, None, None)
        identity = stamp(self.identity) if self.mode == "sops" else None
        return (self.mode, tuple(stamp(path) for path in files), identity, tuple(stamp(path) for path in self.watch))

    def read(self) -> bool:
        """Reads the files again when they, the identity or a watched file changed (or a failed read is due again),
        and keeps the outcome aside until commit(). True when there is something new to commit. Safe to run
        beside the service: it changes nothing the service uses."""
        files = self._files()
        state = self._state(files)
        watched_changed = self._seen is not None and state[3] != self._seen[3]
        if state == self._seen and not (self.status == ERROR and time.monotonic() >= self._retry_at):
            return False
        self._seen = state
        names = [path.name for path in files]
        values: dict[str, str] = {}
        if not files:
            kind = "*.enc.env" if self.mode == "sops" else "*.env"
            status, detail = NONE, f"no {kind} file in {self.env_dir}"
        elif self.mode == "sops" and not self.identity.exists():
            status, detail = LOCKED, "locked since the last restart: as root on the brain, run jarvis-unlock"
        else:
            status, detail = UNLOCKED, ""
            try:
                self._check_identity()
                for path in files:
                    try:
                        values.update(self._read(path))
                    except VaultError as exc:
                        raise VaultError(f"{path.name}: {exc}") from None
            except VaultError as exc:
                status, detail, values = ERROR, str(exc), {}
                self._retry_at = time.monotonic() + RETRY_AFTER
            else:
                detail = f"{len(values)} values from {len(files)} file{'s' if len(files) != 1 else ''}"
        new = (status, detail, names, values)
        if new == (self.status, self.detail, self.files, self._values) and not watched_changed:
            return False
        self._pending = new
        return True

    def commit(self) -> None:
        """Puts what read() found in force, all at once."""
        if self._pending is not None:
            self.status, self.detail, self.files, self._values = self._pending
            self._pending = None

    def refresh(self) -> bool:
        """read() and commit() together, for whoever is not running beside answers in progress."""
        changed = self.read()
        self.commit()
        return changed

    def _check_identity(self) -> None:
        if self.mode != "sops":
            return
        try:
            text = self.identity.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            raise VaultError(f"{self.identity} cannot be read") from None
        if not identity_ok(text):
            raise VaultError(f"{self.identity} does not hold an age identity (a public key in its place?); "
                             "as root on the brain, run jarvis-unlock again")

    def _read(self, path: Path) -> dict[str, str]:
        if self.mode != "sops":
            try:
                return parse_dotenv(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                raise VaultError(f"cannot be read ({type(exc).__name__})") from None
        env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "SOPS_AGE_KEY_FILE": str(self.identity)}
        try:
            done = self._run([self.sops, "decrypt", "--input-type", "dotenv", "--output-type", "dotenv", str(path)],
                             env=env, capture_output=True, timeout=30)
        except FileNotFoundError:
            raise VaultError(f"{self.sops} is not installed") from None
        except subprocess.TimeoutExpired:
            raise VaultError("sops took too long") from None
        except OSError as exc:
            raise VaultError(f"{self.sops} cannot be run ({type(exc).__name__})") from None
        if done.returncode != 0:
            mine, listed = self.recipient(), recipients(path)
            if mine and listed and mine not in listed:
                raise VaultError("is not encrypted for this brain's identity; add its public key to .sops.yaml and "
                                 "run sops updatekeys on the file")
            said = _SOPS_SAYS.get(done.returncode, "sops could not open it")
            raise VaultError(f"{said} (sops code {done.returncode})")
        try:
            return parse_dotenv(done.stdout.decode("utf-8"))
        except UnicodeDecodeError:
            raise VaultError("holds a value that is not UTF-8 text") from None

    def recipient(self) -> str:
        """This brain's public key, as jarvis-unlock recorded it beside the unlocked identity."""
        try:
            text = (self.identity.parent / "recipient").read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return ""
        return text if re.match(r"^age1[0-9a-z]{58}$", text) else ""

    # ------------------------------------------------------------------ using

    def get(self, name: str) -> str | None:
        return self._values.get(name) or None

    def names(self) -> list[str]:
        return sorted(name for name, value in self._values.items() if value)

    def fingerprint(self, *names: str) -> str:
        """Tells whether the named values changed, without holding them."""
        joined = "\0".join(f"{name}={self._values.get(name, '')}" for name in names)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def mask(self, text: str) -> str:
        """Every value of the vault replaced by its name, longest first, wherever it appears."""
        for name, value in sorted(self._values.items(), key=lambda item: -len(item[1])):
            if len(value) >= MIN_MASKED and not name.endswith(PLAIN):
                text = text.replace(value, f"[{name}]")
        return text

    def about(self) -> dict[str, object]:
        """What may be shown about the vault: its state, never a value."""
        return {"status": self.status, "detail": self.detail, "files": self.files, "names": len(self.names())}

    def __repr__(self) -> str:  # never shows a value
        return f"Vault(status={self.status!r}, names={self.names()!r})"
