"""Shared client for read-only lab APIs.

It can only send GET. Every server is verified before any credential is sent: either against a certificate
authority, or against the server's own certificate, pinned. Neither comes from this repository: the installer
puts them on the brain. A host entry may be "address" or "address=name"; with a name the certificate must be
valid for that name.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx


class AdapterError(Exception):
    """Safe to show to the model and to log; never carries a credential."""


def percent(used: Any, total: Any) -> float | None:
    try:
        return round(100 * float(used) / float(total), 1) if float(total) > 0 else None
    except (TypeError, ValueError):
        return None


def gib(value: Any) -> float | None:
    try:
        return round(float(value) / 1024**3, 1)
    except (TypeError, ValueError):
        return None


def hours(seconds: Any) -> float | None:
    try:
        return round(float(seconds) / 3600, 1)
    except (TypeError, ValueError):
        return None


def trust_context(trust_file: Path, pinned: bool = False) -> ssl.SSLContext:
    """TLS settings that trust exactly one file: a CA, or one pinned server certificate."""
    context = ssl.create_default_context(cafile=str(trust_file))
    if pinned:
        # A self-signed server certificate is its own trust anchor. The strict
        # profile rejects that shape; everything else (signature, dates, name) is still checked.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


class ReadOnlyApiClient:
    system = "server"

    def __init__(
        self,
        hosts: list[str],
        auth_header: str,
        port: int,
        trust_file: Path | None = None,
        pinned: bool = False,
        http: httpx.AsyncClient | None = None,
        base_path: str = "/api2/json",
        unwrap: str | None = "data",
    ):
        if not hosts:
            raise ValueError(f"no {self.system} hosts configured")
        self.targets: list[tuple[str, str | None]] = []
        for entry in hosts:
            address, _, name = entry.partition("=")
            address = address.strip()
            if ":" in address and not address.startswith("["):
                address = f"[{address}]"   # an IPv6 address, written as a URL needs it
            self.targets.append((address, name.strip() or None))
        self.hosts = [address for address, _ in self.targets]
        self.port = port
        self._auth = auth_header
        self._base = base_path
        self._unwrap = unwrap
        if http is None:
            if trust_file is None:
                raise ValueError(f"a CA file is required: Jarvis does not talk to {self.system} unverified")
            # trust_env off: lab traffic never goes through a proxy named in the environment.
            http = httpx.AsyncClient(
                verify=trust_context(trust_file, pinned), timeout=httpx.Timeout(8, connect=4), trust_env=False
            )
        self._http = http

    async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET one API path, trying each configured host in turn."""
        failures: list[str] = []
        for host, tls_name in self.targets:
            url = f"https://{host}:{self.port}{self._base}{path}"
            extensions = {"sni_hostname": tls_name} if tls_name else {}
            try:
                resp = await self._http.get(
                    url, params=params, headers={"Authorization": self._auth}, extensions=extensions
                )
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                failures.append(f"{host}: {type(exc).__name__}")
                continue
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    return body[self._unwrap] if self._unwrap else body
                except (ValueError, KeyError, TypeError) as exc:
                    raise AdapterError(f"{host} sent an unreadable answer") from exc
            if resp.status_code in (401, 403):
                raise AdapterError(f"{host} refused the read-only token ({resp.status_code})")
            failures.append(f"{host}: HTTP {resp.status_code}")
        raise AdapterError(f"no {self.system} answered: " + "; ".join(failures))
