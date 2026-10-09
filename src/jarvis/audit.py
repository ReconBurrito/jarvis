"""Hash-chained audit log.

Each record carries the hash of the record before it, so removing or editing any line breaks every hash
after it. Records are appended to a JSON-lines file and, when a syslog target is configured, copied there
as RFC 5424 syslog over UDP.

When Jarvis holds a signing key, each record also carries an Ed25519 signature over its hash. The chain
shows that nothing was edited or removed; the signature shows that Jarvis wrote it. The public key is not
a secret, so the log can be checked anywhere.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

GENESIS = "0" * 64

SYSLOG_FACILITY_LOCAL0 = 16
SEVERITY_WARNING = 4
SEVERITY_INFO = 6
WARNING_KINDS = {"request_denied", "tool_denied", "login_failed", "login_denied", "turn_failed"}
MAX_SYSLOG_BYTES = 8000


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(prev: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev + _canonical(body)).encode("utf-8")).hexdigest()


class Signer:
    """Ed25519 signer built from a 32-byte seed held in the vault."""

    def __init__(self, seed_b64: str):
        try:
            seed = base64.b64decode(seed_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("signing key is not valid base64") from exc
        if len(seed) != 32:
            raise ValueError("signing key must be 32 bytes")
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        raw = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key = base64.b64encode(raw).decode("ascii")

    def sign(self, record_hash_hex: str) -> str:
        return base64.b64encode(self._key.sign(record_hash_hex.encode("ascii"))).decode("ascii")


def signature_ok(public_key_b64: str, record_hash_hex: str, sig_b64: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(sig_b64, validate=True), record_hash_hex.encode("ascii"))
        return True
    except (InvalidSignature, binascii.Error, ValueError):
        return False


def digest(text: str) -> str:
    """Short fingerprint used in place of message text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AuditDamaged(Exception):
    """The log cannot be added to as it is. Nothing is written until someone has looked at it."""


class AuditLog:
    def __init__(
        self,
        path: Path,
        syslog: tuple[str, int] | None = None,
        host: str = "jarvis",
        signer: Signer | None = None,
    ):
        self.signer = signer
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._syslog = syslog
        self._host = host
        self._sock: socket.socket | None = None
        self.seq, self.last_hash = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        with self.path.open("rb") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)  # never in the middle of another program's write
            return self._tail_of(fh)[:2]

    def _tail_of(self, fh: Any) -> tuple[int, str, bool]:
        """The number and hash of the last record in an open log, read from its end, and whether the log ends
        with a line end (a log mended by hand may not)."""
        size = fh.seek(0, os.SEEK_END)
        reach = 4096
        while True:
            start = max(0, size - reach)
            fh.seek(start)
            lines = [line for line in fh.read(size - start).split(b"\n") if line.strip()]
            # The first piece may be the cut-off end of a longer record, unless the reading began at the start.
            if len(lines) >= 2 or start == 0:
                break
            reach *= 4
        fh.seek(max(0, size - 1))
        whole = size == 0 or fh.read(1) == b"\n"
        if not lines:
            return 0, GENESIS, whole
        try:
            rec = json.loads(lines[-1].decode("utf-8", "replace"))
            return int(rec["seq"]), str(rec["hash"]), whole
        except (ValueError, KeyError, TypeError) as exc:
            # A chain cannot be continued from a record that cannot be read, and is not started over in silence.
            raise AuditDamaged(f"the last record of {self.path} cannot be read ({type(exc).__name__})") from exc

    def append(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        """Adds one record. More than one program may write the same log (the service and the jarvis command):
        the file is locked for the moment of writing and the chain is continued from what is really its end."""
        with self._lock:
            with self.path.open("a+b") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                seq, last_hash, whole = self._tail_of(fh)
                body = {"seq": seq + 1, "ts": round(time.time(), 3), "kind": kind, "data": data}
                rec = dict(body, prev=last_hash, hash=record_hash(last_hash, body))
                if self.signer:
                    rec["sig"] = self.signer.sign(rec["hash"])
                fh.seek(0, os.SEEK_END)
                # A last record that is whole but lacks its line end gets it, so that this one starts a line.
                fh.write((("" if whole else "\n") + _canonical(rec) + "\n").encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            self.seq, self.last_hash = rec["seq"], rec["hash"]
        self._send_syslog(rec)
        return rec

    def syslog_line(self, rec: dict[str, Any]) -> bytes:
        severity = SEVERITY_WARNING if rec["kind"] in WARNING_KINDS else SEVERITY_INFO
        pri = SYSLOG_FACILITY_LOCAL0 * 8 + severity
        stamp = datetime.fromtimestamp(rec["ts"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        body = {"audit_seq": rec["seq"], "audit_kind": rec["kind"], "audit_hash": rec["hash"], **rec["data"]}
        if rec.get("sig"):
            body["audit_sig"] = rec["sig"]
        msg = _canonical(body)
        if len(msg.encode("utf-8")) > MAX_SYSLOG_BYTES:
            msg = _canonical({"audit_seq": rec["seq"], "audit_kind": rec["kind"], "audit_hash": rec["hash"], "truncated": True})
        return f"<{pri}>1 {stamp} {self._host} jarvis - {rec['kind']} - {msg}".encode("utf-8")

    def _send_syslog(self, rec: dict[str, Any]) -> None:
        if not self._syslog:
            return
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.sendto(self.syslog_line(rec), self._syslog)
        except OSError:
            # The syslog server being away must never stop Jarvis. The file is the record.
            pass


def verify(path: Path, public_key: str | None = None, strict: bool = False) -> tuple[bool, int, str]:
    """Walk the chain, and the signatures when a public key is given.

    Returns (ok, records checked, message). A record is unsigned when Jarvis
    wrote it without the key: before signing was switched on, or while the
    vault was still locked after a boot. Those are counted and reported. With
    strict, any unsigned record after the first signed one is a failure.
    """
    prev = GENESIS
    count = 0
    signed = 0
    late_unsigned: list[int] = []
    with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)  # a record another program is writing this moment is read whole
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                body = {k: rec[k] for k in ("seq", "ts", "kind", "data")}
            except (ValueError, KeyError, TypeError) as exc:
                return False, count, f"line {lineno}: unreadable record ({type(exc).__name__})"
            if rec.get("prev") != prev:
                return False, count, f"line {lineno}: chain broken (prev does not match)"
            if rec.get("hash") != record_hash(prev, body):
                return False, count, f"line {lineno}: record altered (hash does not match)"
            if rec["seq"] != count + 1:
                return False, count, f"line {lineno}: sequence gap (expected {count + 1}, got {rec['seq']})"
            if rec.get("sig"):
                if public_key and not signature_ok(public_key, rec["hash"], rec["sig"]):
                    return False, count, f"line {lineno}: signature does not match the public key"
                signed += 1
            elif signed:
                if strict:
                    return False, count, f"line {lineno}: unsigned record after signing began"
                late_unsigned.append(rec["seq"])
            prev = rec["hash"]
            count += 1
    if not signed:
        return True, count, "chain intact"
    note = ""
    if late_unsigned:
        note = f"; {len(late_unsigned)} unsigned after signing began (first at record {late_unsigned[0]})"
    if public_key:
        return True, count, f"chain intact, {signed} of {count} records signed, signatures valid{note}"
    return True, count, f"chain intact, {signed} of {count} records signed (signatures not checked: no public key){note}"
