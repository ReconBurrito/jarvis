"""Jarvis put together from its settings: the model, the tools, the vault, the audit log and the turn loop. The
jarvis command and the service both start from here, so both are the same Jarvis."""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Any

import httpx

from .about import SURFACE, describe
from .adapters.dns import make_dns_tool, parse_servers
from .adapters.opnsense import OpnsenseClient, make_opnsense_tools
from .adapters.pbs import PbsClient, make_pbs_tools
from .adapters.proxmox import ProxmoxClient, make_proxmox_tools
from .adapters.switch import SwitchSession, make_switch_tool
from .audit import AuditLog
from .browser.door import Door
from .browser.tools import Browser, make_browser_tools
from .selffs.tools import SelfFiles, make_selffs_tools
from .config import Settings
from .llm import OllamaBackend
from .notes.repo import BrainRepo, is_ssh_remote
from .notes.semantic import Embeddings
from .notes.tools import make_notes_tools
from .notes.write import make_note_write_tools
from .router import Router
from .tools import Tool, ToolRegistry, make_local_status
from .vault import ERROR, LOCKED, NONE, UNLOCKED, Vault

# The lab's systems: name, tool name prefix, the vault values it needs, and the certificate it trusts (in the trust
# folder; None for a system reached without one). A system with none of its values in the vault is not set up, and
# is left out without a word; one with some of them says which are missing.
SYSTEMS = (
    ("proxmox", "proxmox_", ("JARVIS_PVE_TOKEN_ID", "JARVIS_PVE_TOKEN_SECRET", "JARVIS_PVE_HOSTS"), "proxmox-ca.pem"),
    ("pbs", "pbs_", ("JARVIS_PBS_TOKEN_ID", "JARVIS_PBS_TOKEN_SECRET", "JARVIS_PBS_HOSTS"), "pbs.pem"),
    ("opnsense", "opnsense_", ("JARVIS_OPNSENSE_API_KEY", "JARVIS_OPNSENSE_API_SECRET", "JARVIS_OPNSENSE_HOSTS"), "opnsense.pem"),
    ("dns", "dns_", ("JARVIS_DNS_SERVERS", "JARVIS_DNS_LAB_NAME"), None),
    ("switch", "switch_", ("JARVIS_SWITCH_USER", "JARVIS_SWITCH_PASSWORD", "JARVIS_SWITCH_HOST"), "switch_known_hosts"),
    # Jarvis's own notes: a git repository it reaches with a deploy key (base64 of the private key, or the key itself).
    ("notes", "note", ("JARVIS_NOTES_REPO", "JARVIS_NOTES_DEPLOY_KEY"), "github_known_hosts"),
)
# Where each certificate comes from, for the sentence that says it is missing.
TRUST_FROM = {
    "proxmox-ca.pem": "the installer on the Proxmox node puts it there",
    "pbs.pem": "the backup server's own certificate; copy it there",
    "opnsense.pem": "the firewall's own web certificate; copy it there",
    "switch_known_hosts": "the switch's SSH host key, as ssh-keyscan prints it; check it and put it there",
    "github_known_hosts": "the installer puts GitHub's published host keys there",
}


class Jarvis:
    """Everything one conversation needs, put together from the settings."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, surface: str = SURFACE,
                 record_vault: bool = False):
        self.settings = settings
        self.record_vault = record_vault   # the service records each change of the vault; a command run once does not
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10, read=None))
        self.audit = AuditLog(settings.audit_path)
        self.vault = Vault(settings.env_dir, settings.secrets_mode, settings.identity, settings.sops,
                           watch=tuple(settings.trust_dir / trust for *_, trust in SYSTEMS if trust)
                           + settings.door_files + (settings.fs_socket,))
        # Nothing a tool returns may carry a value of the vault on to the model.
        self.tools = ToolRegistry(self.audit, redact=lambda text: self.vault.mask(text))
        self.tools.register(make_local_status(settings.ollama_url, self.client))
        self.lab: dict[str, str] = {}    # system: why it has no tools ("" when it has them)
        self.browser = ""                # why Jarvis has no browser tools ("" when it has them, None when no desktop is set up)
        self.files = False               # whether Jarvis has its own machine's files (jarvis-fsd is there)
        self._lab_http: list[httpx.AsyncClient] = []
        self._closing: list[httpx.AsyncClient] = []
        self.notes: BrainRepo | None = None
        self._old_notes: list[BrainRepo] = []
        self._tasks: set[asyncio.Task] = set()
        self.backend = OllamaBackend(settings.ollama_url, settings.model, self.client) if settings.has_model else None
        self.router = Router(
            [self.backend] if self.backend else [], self.tools, self.audit, audit_text=settings.audit_text,
            describe=lambda names, model: describe(names, model, where=settings.where, surface=surface, lab=self.lab_note()),
        )
        try:
            self.refresh()
        except Exception as exc:  # whatever the vault holds, Jarvis starts; it says why it cannot read the lab
            self.vault.status, self.vault.detail = "error", f"the vault could not be read ({type(exc).__name__})"
            self.lab = {name: "the vault cannot be read" for name, *_ in SYSTEMS}

    # ------------------------------------------------------------------ the vault and the lab's tools

    def refresh(self) -> bool:
        """Reads the vault again if it changed, and gives the lab's tools to Jarvis or takes them away to match.
        True when something changed. Cheap when nothing did: it looks at file times only."""
        if not self.vault.refresh():
            return False
        self.apply_vault()
        return True

    def apply_vault(self) -> None:
        """Gives the lab's tools to Jarvis or takes them away, to match what the vault now holds. The clients of
        the tools taken away are closed."""
        for _, prefix, _, _ in SYSTEMS:
            self.tools.remove(prefix)
        retired, self._lab_http = self._lab_http, []
        for http in retired:
            self._retire(http)
        if self.notes is not None:
            # The old repository object lets its key go; a new one is made if the vault still names the notes.
            self._old_notes.append(self.notes)
            self._later(self._close_notes(self.notes))
            self.notes = None
        lab: dict[str, str] = {}
        for system in SYSTEMS:
            why = self._system(*system)
            if why is not None:
                lab[system[0]] = why
        self.lab = lab
        self.browser = self._browser()
        self.files = self._files()
        record: dict[str, Any] = {"status": self.vault.status, "files": self.vault.files, "values": len(self.vault.names()),
                                  "tools": [name for name in self.tools.names() if name != "local_status"]}
        if self.vault.status != UNLOCKED:
            record["detail"] = self.vault.detail
        if self.record_vault:
            self.audit.append("vault", record)

    def _system(self, name: str, prefix: str, needs: tuple[str, ...], trust: str | None) -> str | None:
        """Gives Jarvis one system's tools when it can. "" when it did, why not when it did not, None when the
        vault does not mention the system at all."""
        if self.vault.status != UNLOCKED:
            return "the vault is locked" if self.vault.status == LOCKED else "the vault cannot be read"
        missing = [need for need in needs if not self.vault.get(need)]
        if len(missing) == len(needs):
            return None
        if missing:
            return "not in the vault: " + ", ".join(missing)
        cert = self.settings.trust_dir / trust if trust else None
        if name == "notes" and not is_ssh_remote(self.vault.get(needs[0]).strip()):
            cert = None   # a repository on this machine (a folder) is reached without a host key
        if cert is not None and not cert.is_file():
            return f"{cert} is missing ({TRUST_FROM[trust]})"
        value = self.vault.get
        hosts = [item.strip() for item in (value(needs[2]) if len(needs) > 2 else "").split(",") if item.strip()]
        try:
            if name == "proxmox":
                client = ProxmoxClient(hosts, value(needs[0]), value(needs[1]), cert)
                tools = make_proxmox_tools(client)
            elif name == "pbs":
                client = PbsClient(hosts, value(needs[0]), value(needs[1]), cert)
                tools = make_pbs_tools(client)
            elif name == "opnsense":
                client = OpnsenseClient(hosts, value(needs[0]), value(needs[1]), cert)
                tools = make_opnsense_tools(client)
            elif name == "notes":
                return self._notes(value(needs[0]).strip(), value(needs[1]), cert)
            elif name == "switch":
                client = None
                if len(hosts) != 1:
                    raise ValueError("one switch address")
                tools = [make_switch_tool(SwitchSession(hosts[0], value(needs[0]), value(needs[1]), cert))]
            else:
                client = None
                tools = [make_dns_tool(parse_servers(value(needs[0])), value(needs[1]).strip())]
        except (ValueError, OSError) as exc:
            return f"cannot be set up: {type(exc).__name__}"
        if client is not None:
            self._lab_http.append(client._http)
        for tool in tools:
            self.tools.register(tool)
        return ""

    def _browser(self) -> str | None:
        """Gives Jarvis the browser on the owner's desktop when its door is set up. It needs nothing from the vault:
        the brain's own certificate opens the door. None when no desktop has handed its door over."""
        self.tools.remove("browser_")
        door = Door(*self.settings.door_files)
        if not door.set_up():
            return None
        why = door.ready()
        if why:
            return why
        for tool in make_browser_tools(Browser(door)):
            self.tools.register(tool)
        return ""

    def _files(self) -> bool:
        """Gives Jarvis its own machine's files when jarvis-fsd runs (its socket is there). No vault needed."""
        self.tools.remove("fs_")
        if not self.settings.fs_socket.is_socket():
            return False
        for tool in make_selffs_tools(SelfFiles(self.settings.fs_socket)):
            self.tools.register(tool)
        return True

    def _notes(self, remote: str, key: str, known_hosts) -> str:
        """Gives Jarvis its notes: the repository is cloned or brought up to date in the background."""
        raw = key.strip().encode()
        if b"PRIVATE KEY-----" not in raw:
            try:
                raw = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return "cannot be set up: JARVIS_NOTES_DEPLOY_KEY is neither a private key nor one in base64"
        if is_ssh_remote(remote) and b"PRIVATE KEY-----" not in raw:
            return "cannot be set up: JARVIS_NOTES_DEPLOY_KEY does not hold a private key (a public key in its place?)"
        repo = BrainRepo(self.settings.notes_dir / "repo", remote, raw, known_hosts,
                         has_secret=self.vault.holds_secret)
        model = self.settings.embed_model
        embeddings = None if model == "none" else Embeddings(self.settings.ollama_url, model,
                                                               self.settings.notes_dir / "vectors.json", self.client)
        for tool in make_notes_tools(repo, embeddings) + make_note_write_tools(repo, self.audit):
            self.tools.register(tool)
        self.notes = repo
        self._later(self._start_notes(repo))
        return ""

    async def _start_notes(self, repo: BrainRepo) -> None:
        """Clones or refreshes the notes, and says in the lab's state whether that worked."""
        await repo.start()
        if repo is self.notes:
            fresh = repo.ready and not repo.detail.startswith("local copy only")
            self.lab["notes"] = "" if fresh else repo.detail

    async def _close_notes(self, repo: BrainRepo) -> None:
        await repo.close()
        if repo in self._old_notes:
            self._old_notes.remove(repo)

    def _later(self, work) -> None:
        """Runs work in the background when there is a loop to run it; a command run once starts it when used."""
        try:
            task = asyncio.get_running_loop().create_task(work)
        except RuntimeError:
            work.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _retire(self, http: httpx.AsyncClient) -> None:
        try:
            asyncio.get_running_loop().create_task(http.aclose())
        except RuntimeError:  # no loop running (a command that ends anyway): closed at the end
            self._closing.append(http)

    def lab_note(self) -> str:
        """For the model: why it cannot read the lab, in one sentence, when it cannot."""
        if self.vault.status == LOCKED:
            return ("Your tools for the lab's systems are switched off because your vault is locked since your last "
                    "restart. The owner unlocks it on the brain by running jarvis-unlock as root; until then say so when "
                    "asked about the lab.")
        if self.vault.status in (ERROR, NONE) and self.settings.secrets_mode == "sops":
            return ("Your tools for the lab's systems are switched off because your vault cannot be read: "
                    f"{self.vault.detail}.")
        return ""

    def lab_tools(self) -> list[Tool]:
        return [tool for tool in self.tools._tools.values() if tool.name != "local_status"]

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for repo in self._old_notes + ([self.notes] if self.notes else []):
            await repo.close()
        for http in self._lab_http + self._closing:
            await http.aclose()
        await self.client.aclose()
