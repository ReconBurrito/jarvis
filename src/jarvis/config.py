"""Jarvis's settings.

They come from site.env, the file the installer writes in /etc/jarvis: plain KEY=value lines, no quoting and
no secrets. The same key in the environment wins, which is how the tests and a one-off trial set one. Nothing
here is secret; secrets never go through this module.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ETC = "JARVIS_ETC"  # the folder site.env and the release record are in; /etc/jarvis unless a test says otherwise
_KEY = re.compile(r"^(JARVIS_[A-Z0-9_]+)=(.*)$")
_URL = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+$")
_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"", "0", "false", "no", "off"}


class SettingsError(Exception):
    """A setting that cannot be used. The message names the key and what it must be."""


def read_site(path: Path) -> dict[str, str]:
    """The JARVIS_ lines of a site.env; a later line for the same key wins, as in the installer."""
    found: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return found
    for line in text.splitlines():
        match = _KEY.match(line)
        if match:
            found[match.group(1)] = match.group(2).strip()
    return found


def _release(etc: Path) -> str:
    try:
        for line in (etc / "release").read_text(encoding="utf-8").splitlines():
            if line.startswith("name="):
                return line[5:].strip()
    except OSError:
        pass
    return ""


@dataclass(frozen=True)
class Settings:
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"                  # as Ollama names it; "none" when no local model was asked for
    embed_model: str = "qwen3-embedding:0.6b"  # for searching notes by meaning; "none" for words only
    data_dir: Path = Path("/var/lib/jarvis")
    audit_text: bool = False                 # keep what was said in the audit log, not only its length and fingerprint
    where: str = ""                          # the machine Jarvis runs on, in words, for its own description
    release: str = ""                        # the installed release, as the installer recorded it
    installing: bool = False                 # a release is being installed right now; the record is the one before
    site: Path = Path("/etc/jarvis/site.env")
    # The service the panel talks to.
    address: str = ""                        # this machine's own address, the one the panel is told and the certificate names
    port: int = 8443                         # fixed: the installer's firewall rule and the desktop's link name it too
    listen: str = "0.0.0.0"
    panel_allow: tuple[str, ...] = ()        # who may talk to the service: addresses, or networks by their first address
    tls_dir: Path = Path("/etc/jarvis/tls")  # server.pem and server.key, made by the installer
    # The secrets: where, and how kept (see jarvis.vault).
    env_dir: Path = Path("/etc/jarvis/secrets")
    secrets_mode: str = "plain"
    identity: Path = Path("/run/jarvis/age.key")   # the unlocked age identity, in memory-backed /run
    sops: str = "/usr/local/bin/sops"
    trust_dir: Path = Path("/etc/jarvis/trust")    # certificates of the lab's systems, put there by the installer

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit" / "audit.jsonl"

    @property
    def notes_dir(self) -> Path:
        """The notes folder in Jarvis's state: the clone of the notes repository, and the vectors made from it."""
        return self.data_dir / "notes"

    @property
    def has_model(self) -> bool:
        return self.model != "none"

    def release_now(self) -> str:
        """The installed release as the record says at this moment. A service that was started in the middle
        of an install must not go on naming the release before."""
        return _release(self.site.parent) or self.release

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        etc = Path(env.get(ETC) or "/etc/jarvis")
        site = etc / "site.env"
        values = read_site(site)
        values.update({key: value for key, value in env.items() if key.startswith("JARVIS_")})

        def get(key: str, default: str) -> str:
            return values.get(key, "").strip() or default

        url = get("JARVIS_OLLAMA_URL", cls.ollama_url).rstrip("/")
        if not _URL.match(url):
            raise SettingsError(f"JARVIS_OLLAMA_URL must be an address such as http://127.0.0.1:11434, not '{url}'.")
        data_dir = Path(get("JARVIS_DATA_DIR", str(cls.data_dir)))
        if not data_dir.is_absolute():
            raise SettingsError(f"JARVIS_DATA_DIR must be a full path, not '{data_dir}'.")
        text = values.get("JARVIS_AUDIT_TEXT", "").strip().lower()
        if text not in _TRUE | _FALSE:
            raise SettingsError(f"JARVIS_AUDIT_TEXT must be on or off, not '{text}'.")
        address = values.get("JARVIS_ADDRESS", "").strip()
        try:
            if address:
                ipaddress.ip_address(address)
        except ValueError:
            raise SettingsError(f"JARVIS_ADDRESS must be an address such as 192.0.2.20, not '{address}'.") from None
        allow = tuple(item.strip() for item in values.get("JARVIS_PANEL_ALLOW", "").split(",") if item.strip())
        for item in allow:
            try:
                ipaddress.ip_network(item)
            except ValueError:
                raise SettingsError("JARVIS_PANEL_ALLOW must be addresses, or networks by their first address, with "
                                    f"commas between; '{item}' is neither.") from None
        mode = get("JARVIS_SECRETS_MODE", cls.secrets_mode)
        if mode not in ("plain", "sops"):
            raise SettingsError(f"JARVIS_SECRETS_MODE must be plain or sops, not '{mode}'.")
        paths = {}
        for key, default in (("JARVIS_ENV_DIR", cls.env_dir), ("JARVIS_IDENTITY", cls.identity)):
            paths[key] = Path(get(key, str(default)))
            if not paths[key].is_absolute():
                raise SettingsError(f"{key} must be a full path, not '{paths[key]}'.")
        return cls(
            env_dir=paths["JARVIS_ENV_DIR"],
            secrets_mode=mode,
            identity=paths["JARVIS_IDENTITY"],
            sops=get("JARVIS_SOPS", cls.sops),
            trust_dir=etc / "trust",
            address=address,
            panel_allow=allow,
            tls_dir=etc / "tls",
            ollama_url=url,
            model=get("JARVIS_LOCAL_MODEL", cls.model),
            embed_model=get("JARVIS_EMBED_MODEL", cls.embed_model),
            data_dir=data_dir,
            audit_text=text in _TRUE,
            where=values.get("JARVIS_HOST_DESCRIPTION", "").strip(),
            release=_release(etc),
            installing=(etc / "release.pending").exists(),
            site=site,
        )
