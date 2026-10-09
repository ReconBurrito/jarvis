"""The vault: locked until unlocked, opened only by this brain's identity, its values never shown."""
import json
import subprocess

import httpx
from fakes import FakeOllama, run, text_chunks, tool_chunks
import pytest
from vaults import REAL_SOPS, SOPS, identity, real_vault_file, vault_file

from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.vault import ERROR, LOCKED, NONE, UNLOCKED, Vault, identity_ok, parse_dotenv

TOKEN = "11111111-2222-3333-4444-555555555555"
VALUES = {"JARVIS_PVE_TOKEN_ID": "jarvis@pve!ro", "JARVIS_PVE_TOKEN_SECRET": TOKEN,
          "JARVIS_PVE_HOSTS": "192.0.2.11=pve1,192.0.2.12"}


def test_dotenv_lines_are_read_as_sops_and_people_write_them():
    text = 'A=1\n# a note\nexport B="two words"\nC=\'x=y\'\nsops_mac=ENC[...]\nlower=no\nD=line\\nnext\n  \nNOEQUALS\n'
    assert parse_dotenv(text) == {"A": "1", "B": "two words", "C": "x=y", "D": "line\nnext"}


def test_a_sops_vault_is_locked_until_this_brains_identity_is_there(tmp_path):
    env = tmp_path / "secrets"
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[0])
        return subprocess.run(*args, **kwargs)

    vault = Vault(env, "sops", tmp_path / "run" / "age.key", SOPS, run=counted)
    assert vault.refresh() is True and vault.status == NONE and "no *.enc.env file" in vault.detail
    key, public = identity(tmp_path / "keys")
    vault_file(env / "lab.enc.env", VALUES, public)
    assert vault.refresh() is True and vault.status == LOCKED and "run jarvis-unlock" in vault.detail
    assert vault.get("JARVIS_PVE_TOKEN_SECRET") is None and calls == []
    # Unlocked: the identity appears in /run.
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "age.key").write_bytes(key.read_bytes())
    (tmp_path / "run" / "recipient").write_text(public + "\n")
    assert vault.refresh() is True and vault.status == UNLOCKED and vault.detail == "3 values from 1 file"
    assert vault.get("JARVIS_PVE_TOKEN_SECRET") == TOKEN and vault.names() == sorted(VALUES)
    # Nothing changed: nothing is read again.
    assert vault.refresh() is False and len(calls) == 1
    # Locked again: the values go.
    (tmp_path / "run" / "age.key").unlink()
    assert vault.refresh() is True and vault.status == LOCKED and vault.names() == []


def test_a_vault_not_encrypted_for_this_brain_says_what_to_do(tmp_path):
    env = tmp_path / "secrets"
    _, someone_else = identity(tmp_path / "other")
    vault_file(env / "lab.enc.env", VALUES, someone_else)
    mine, _ = identity(tmp_path / "run", "age.key")
    vault = Vault(env, "sops", mine, SOPS)
    vault.refresh()
    assert vault.status == ERROR and vault.names() == []
    assert "lab.enc.env: is not encrypted for this brain's identity; add its public key to .sops.yaml" in vault.detail
    # Encrypted for both: it opens.
    _, public = identity(tmp_path / "run2")
    vault_file(env / "lab.enc.env", VALUES, someone_else, (tmp_path / "run" / "recipient").read_text().strip())
    vault.refresh()
    assert vault.status == UNLOCKED, vault.detail
    # sops not there, or a file it cannot open for another reason: said, without a value.
    broken = Vault(env, "sops", mine, str(tmp_path / "no-sops"))
    broken.refresh()
    assert broken.status == ERROR and "is not installed" in broken.detail
    (env / "lab.enc.env").write_text("sops_version=3.13.3\nnot age at all\n")
    vault.refresh()
    assert vault.status == ERROR and vault.detail == "lab.enc.env: the key to open it could not be got with this brain's identity (sops code 128)"


def test_every_file_must_open_or_none_is_used(tmp_path):
    env = tmp_path / "secrets"
    mine, public = identity(tmp_path / "run", "age.key")
    _, other = identity(tmp_path / "other")
    vault_file(env / "a.enc.env", {"A_TOKEN": "aaaaaaaaaaaa"}, public)
    vault_file(env / "b.enc.env", {"B_TOKEN": "bbbbbbbbbbbb"}, other)
    vault = Vault(env, "sops", mine, SOPS)
    vault.refresh()
    assert vault.status == ERROR and vault.detail.startswith("b.enc.env: ") and vault.names() == []


def test_a_plain_vault_reads_env_files_and_leaves_sops_files_alone(tmp_path):
    env = tmp_path / "secrets"
    env.mkdir()
    (env / "lab.env").write_text("JARVIS_PVE_TOKEN_SECRET=" + TOKEN + "\n")
    (env / "other.enc.env").write_text("SHOULD_NOT=be read\n")
    vault = Vault(env, "plain", tmp_path / "unused", SOPS)
    vault.refresh()
    assert vault.status == UNLOCKED and vault.names() == ["JARVIS_PVE_TOKEN_SECRET"] and vault.files == ["lab.env"]


def test_values_are_masked_and_never_shown(tmp_path):
    env = tmp_path / "secrets"
    env.mkdir()
    (env / "lab.env").write_text(f"JARVIS_PVE_TOKEN_SECRET={TOKEN}\nSHORT=abc\nLONGER={TOKEN}-more\n")
    vault = Vault(env, "plain")
    vault.refresh()
    said = f"token {TOKEN}-more and {TOKEN}, abc"
    assert vault.mask(said) == "token [LONGER] and [JARVIS_PVE_TOKEN_SECRET], abc"
    assert TOKEN not in repr(vault) and TOKEN not in json.dumps(vault.about())


def test_an_identity_is_one_private_key_and_nothing_else(tmp_path):
    key, public = identity(tmp_path)
    assert identity_ok(key.read_text())
    assert not identity_ok(public) and not identity_ok("") and not identity_ok(key.read_text() * 2)


# ------------------------------------------------------------------ Jarvis with its vault

CLUSTER = [{"type": "cluster", "name": "lab", "quorate": 1},
           {"type": "node", "name": "pve1", "online": 1}, {"type": "node", "name": "pve2", "online": 0}]


def lab(request):
    """The Proxmox API as the tests play it. It answers only with the token the vault holds."""
    if request.headers.get("authorization") != f"PVEAPIToken=jarvis@pve!ro={TOKEN}":
        return httpx.Response(401)
    if request.url.path == "/api2/json/cluster/status":
        return httpx.Response(200, json={"data": CLUSTER})
    if request.url.path == "/api2/json/cluster/resources":
        nodes = [{"type": "node", "node": "pve1", "cpu": 0.1, "maxcpu": 8, "mem": 1, "maxmem": 4, "uptime": 3600},
                 {"type": "node", "node": "pve2"}]
        if request.url.params.get("type") == "vm":
            return httpx.Response(200, json={"data": [{"type": "lxc", "vmid": 201, "name": "jarvis", "node": "pve2",
                                                         "status": "running", "mem": 1, "maxmem": 2, "token": TOKEN}]})
        return httpx.Response(200, json={"data": nodes})
    return httpx.Response(404)


def settings(tmp_path, **changes):
    values = {"data_dir": tmp_path / "data", "site": tmp_path / "site.env", "env_dir": tmp_path / "secrets",
              "secrets_mode": "sops", "identity": tmp_path / "run" / "age.key", "sops": SOPS, "trust_dir": tmp_path / "trust"}
    values.update(changes)
    return Settings(**values)


def bench(tmp_path, fake=None, unlocked=True, **changes):
    config = settings(tmp_path, **changes)
    key, public = identity(tmp_path / "keys")
    vault_file(config.env_dir / "lab.enc.env", VALUES, public)
    config.trust_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
                    "-subj", "/CN=test", "-keyout", "/dev/null", "-out", str(config.trust_dir / "proxmox-ca.pem")],
                   check=True, capture_output=True)
    if unlocked:
        unlock(config, key)
    jarvis = Jarvis(config, (fake or FakeOllama()).client(), record_vault=True)
    return jarvis, config, key


def unlock(config, key):
    config.identity.parent.mkdir(parents=True, exist_ok=True)
    config.identity.write_bytes(key.read_bytes())


def to_lab(jarvis):
    """The Proxmox client talks to the test's API instead of a real one."""
    for http in jarvis._lab_http:
        http._transport = httpx.MockTransport(lab)


def records(config):
    return [json.loads(line) for line in config.audit_path.read_text().splitlines()]


def test_the_lab_tools_come_with_the_unlock_and_go_with_the_lock(tmp_path):
    jarvis, config, key = bench(tmp_path, unlocked=False)
    assert jarvis.vault.status == LOCKED and jarvis.tools.names() == ["local_status"]
    assert jarvis.lab == dict.fromkeys(("proxmox", "pbs", "opnsense", "dns", "switch"), "the vault is locked") and "jarvis-unlock" in jarvis.lab_note()
    unlock(config, key)
    assert jarvis.refresh() is True and jarvis.vault.status == UNLOCKED
    assert jarvis.tools.names() == ["local_status", "proxmox_cluster_status", "proxmox_guests", "proxmox_node_status", "proxmox_storage"]
    assert jarvis.lab == {"proxmox": ""} and jarvis.lab_note() == ""
    config.identity.unlink()
    assert jarvis.refresh() is True and jarvis.tools.names() == ["local_status"]
    kinds = [(r["kind"], r["data"]["status"]) for r in records(config) if r["kind"] == "vault"]
    assert kinds == [("vault", "locked"), ("vault", "unlocked"), ("vault", "locked")]
    assert TOKEN not in config.audit_path.read_text()
    run(jarvis.close())


def test_what_is_missing_for_a_system_is_said(tmp_path):
    jarvis, config, key = bench(tmp_path)
    (config.trust_dir / "proxmox-ca.pem").unlink()
    vault_file(config.env_dir / "lab.enc.env", {"JARVIS_PVE_TOKEN_ID": "x"}, (tmp_path / "keys" / "recipient").read_text().strip())
    jarvis.refresh()
    assert jarvis.lab == {"proxmox": "not in the vault: JARVIS_PVE_TOKEN_SECRET, JARVIS_PVE_HOSTS"}
    vault_file(config.env_dir / "lab.enc.env", VALUES, (tmp_path / "keys" / "recipient").read_text().strip())
    jarvis.refresh()
    assert jarvis.lab["proxmox"].endswith("proxmox-ca.pem is missing (the installer on the Proxmox node puts it there)")
    assert jarvis.tools.names() == ["local_status"]
    run(jarvis.close())


def test_the_model_is_told_why_it_cannot_read_the_lab_and_is_given_the_tools_when_it_can(tmp_path):
    fake = FakeOllama([text_chunks("Locked, sir."), tool_chunks("proxmox_cluster_status", {}), text_chunks("Both nodes are known.")])
    jarvis, config, key = bench(tmp_path, fake, unlocked=False)

    async def turn(text):
        return [event async for event in jarvis.router.turn("s", text)]

    run(turn("How is the cluster?"))
    system = fake.requests[0]["messages"][0]["content"]
    assert "your vault is locked since your last restart" in system and "jarvis-unlock" in system
    assert "proxmox" not in json.dumps(fake.requests[0].get("tools", []))
    unlock(config, key)
    jarvis.refresh()
    to_lab(jarvis)
    events = run(turn("And now?"))
    assert {"type": "tool", "name": "proxmox_cluster_status", "arguments": {}, "ok": True} in events
    assert "read the state of the Proxmox cluster" in fake.requests[1]["messages"][0]["content"]
    assert "vault is locked" not in fake.requests[1]["messages"][0]["content"]
    # The token never reaches the model, not even where a tool's answer carries it back by mistake.
    from jarvis.tools import TIER_READ_ONLY, Tool

    async def careless(_):
        return {"said": f"the token is {TOKEN}"}

    jarvis.tools.register(Tool("careless", "d", {"type": "object", "properties": {}}, TIER_READ_ONLY, careless))

    async def ask():
        return await jarvis.tools.dispatch("s", "careless", {})

    assert run(ask()) == {"said": "the token is [JARVIS_PVE_TOKEN_SECRET]"}
    assert all(TOKEN not in json.dumps(request) for request in fake.requests) and TOKEN not in config.audit_path.read_text()
    run(jarvis.close())


def test_the_doctor_reads_the_cluster_and_tells_a_fault_from_a_warning(tmp_path):
    import io

    from jarvis import cli

    jarvis, config, key = bench(tmp_path)
    to_lab(jarvis)

    def doctor(release_check=False):
        out = io.StringIO()
        original = cli.Jarvis
        cli.Jarvis = lambda *args, **kwargs: jarvis   # the doctor looks at this Jarvis, whose lab is played
        try:
            code = run(cli.doctor(config, True, out, client=FakeOllama().client(), release_check=release_check))
        finally:
            cli.Jarvis = original
        return code, out.getvalue()

    code, said = doctor()
    assert code == 0 and "ok: vault: unlocked, 3 values from 1 file" in said and "ok: proxmox: 1 of 2 nodes online, quorate" in said
    for http in jarvis._lab_http:
        http._transport = httpx.MockTransport(lambda request: httpx.Response(401))
    code, said = doctor()
    assert code == 1 and "FAULT: proxmox: does not answer: 192.0.2.11 refused the read-only token (401)" in said
    code, said = doctor(release_check=True)
    assert code == 0 and "WARNING: proxmox: does not answer" in said, "a system that refuses is not the release's fault"
    assert TOKEN not in said



# ------------------------------------------------------------------ found by review

def test_nothing_sops_says_is_passed_on(tmp_path):
    """sops quotes a value it cannot decrypt in its message. That message must go nowhere."""
    env = tmp_path / "secrets"
    env.mkdir()
    (env / "lab.enc.env").write_text("x\n")
    mine, _ = identity(tmp_path / "run", "age.key")
    secret = "hunter2hunter2-plaintext"

    def sops_quoting(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 25, b"", f"Could not decrypt value: Input string {secret} does not match".encode())

    vault = Vault(env, "sops", mine, SOPS, run=sops_quoting)
    vault.refresh()
    assert vault.status == ERROR and secret not in vault.detail and secret not in json.dumps(vault.about())
    assert vault.detail == "lab.enc.env: a value in it cannot be decrypted (a line added by hand?); open and save it with sops edit (sops code 25)"


@pytest.mark.skipif(not REAL_SOPS, reason="needs the real sops (JARVIS_TEST_SOPS)")
def test_with_the_real_sops(tmp_path):
    env = tmp_path / "secrets"
    mine, public = identity(tmp_path / "run", "age.key")
    real_vault_file(env / "lab.enc.env", VALUES, public)
    assert "sops_age__list_0__map_recipient=" + public in (env / "lab.enc.env").read_text()
    vault = Vault(env, "sops", mine, REAL_SOPS)
    vault.refresh()
    assert vault.status == UNLOCKED and vault.get("JARVIS_PVE_TOKEN_SECRET") == TOKEN, vault.detail
    # A line added to the file by hand, in clear: real sops refuses the file and quotes the value; nothing of it
    # reaches the vault's state.
    secret = "hunter2hunter2-plaintext"
    with open(env / "lab.enc.env", "a") as file:
        file.write(f"ADDED={secret}\n")
    vault.refresh()
    assert vault.status == ERROR and secret not in vault.detail and "(sops code" in vault.detail, vault.detail
    # Another brain's file.
    _, other = identity(tmp_path / "other")
    real_vault_file(env / "lab.enc.env", VALUES, other)
    vault.refresh()
    assert vault.status == ERROR and "is not encrypted for this brain's identity" in vault.detail


def test_a_changed_value_is_put_in_force_and_the_old_one_still_masked_until_then(tmp_path):
    jarvis, config, key = bench(tmp_path)
    public = (tmp_path / "keys" / "recipient").read_text().strip()
    first = jarvis._lab_http[0]
    rotated = dict(VALUES, JARVIS_PVE_TOKEN_SECRET="99999999-8888-7777-6666-555555555555", JARVIS_PVE_HOSTS="192.0.2.9")
    vault_file(config.env_dir / "lab.enc.env", rotated, public)
    assert jarvis.vault.read() is True
    assert jarvis.vault.mask(TOKEN) == "[JARVIS_PVE_TOKEN_SECRET]", "nothing changes before commit"
    jarvis.vault.commit()
    jarvis.apply_vault()
    assert jarvis.vault.get("JARVIS_PVE_TOKEN_SECRET") == rotated["JARVIS_PVE_TOKEN_SECRET"]
    assert jarvis._lab_http and jarvis._lab_http[0] is not first and first in jarvis._closing
    run(jarvis.close())
    assert first.is_closed


def test_a_new_certificate_is_noticed_without_a_change_in_the_vault(tmp_path):
    jarvis, config, key = bench(tmp_path)
    (config.trust_dir / "proxmox-ca.pem").unlink()
    assert jarvis.refresh() is True and jarvis.lab["proxmox"].endswith("is missing (the installer on the Proxmox node puts it there)")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
                    "-subj", "/CN=test", "-keyout", "/dev/null", "-out", str(config.trust_dir / "proxmox-ca.pem")],
                   check=True, capture_output=True)
    assert jarvis.refresh() is True and jarvis.lab == {"proxmox": ""}
    run(jarvis.close())


def test_what_cannot_be_read_is_an_error_and_never_a_crash(tmp_path):
    env = tmp_path / "secrets"
    mine, public = identity(tmp_path / "run", "age.key")
    vault_file(env / "lab.enc.env", VALUES, public)
    for run_sops, said in (
        (lambda *a, **k: subprocess.CompletedProcess(a[0], 0, b"A_TOKEN=\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\n", b""), "holds a value that is not UTF-8 text"),
        (lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")), "cannot be run (PermissionError)"),
        (lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(a[0], 30)), "sops took too long"),
    ):
        vault = Vault(env, "sops", mine, SOPS, run=run_sops)
        assert vault.refresh() is True and vault.status == ERROR and said in vault.detail, vault.detail
    # A failure is tried again after a while, even when nothing changed.
    calls = []
    vault = Vault(env, "sops", mine, SOPS, run=lambda *a, **k: calls.append(1) or subprocess.CompletedProcess(a[0], 1, b"", b""))
    vault.refresh()
    vault.refresh()
    assert len(calls) == 1
    vault._retry_at = 0
    vault.refresh()
    assert len(calls) == 2
    # A public key where the identity belongs.
    mine.write_text(public + "\n")
    vault = Vault(env, "sops", mine, SOPS)
    vault.refresh()
    assert vault.status == ERROR and "does not hold an age identity (a public key in its place?)" in vault.detail
    # And a Jarvis whose vault throws still starts, and says so.
    config = settings(tmp_path / "j", env_dir=env, identity=mine, sops=str(tmp_path / "nothing"))
    jarvis = Jarvis(config, FakeOllama().client())
    assert jarvis.vault.status == ERROR and jarvis.tools.names() == ["local_status"]
    run(jarvis.close())


def test_values_are_masked_however_they_are_spelt(tmp_path):
    env = tmp_path / "secrets"
    env.mkdir()
    quoted, two_lines = 'pa"ss\\word99', "line1\\nline2xx"
    (env / "lab.env").write_text(f"QUOTED_TOKEN={quoted}\nLINES_TOKEN={two_lines}\n")
    config = settings(tmp_path, env_dir=env, secrets_mode="plain")
    jarvis = Jarvis(config, FakeOllama().client())
    plain_values = {name: jarvis.vault.get(name) for name in ("QUOTED_TOKEN", "LINES_TOKEN")}
    assert plain_values == {"QUOTED_TOKEN": 'pa"ss\\word99', "LINES_TOKEN": "line1\nline2xx"}
    from jarvis.tools import TIER_READ_ONLY, Tool

    async def careless(_):
        return {"said": [plain_values["QUOTED_TOKEN"], {plain_values["LINES_TOKEN"]: 1}], "n": 3}

    jarvis.tools.register(Tool("careless", "d", {"type": "object", "properties": {}}, TIER_READ_ONLY, careless))

    async def ask():
        return await jarvis.tools.dispatch("s", "careless", {})

    assert run(ask()) == {"said": ["[QUOTED_TOKEN]", {"[LINES_TOKEN]": 1}], "n": 3}
    run(jarvis.close())


def test_every_system_in_the_vault_gets_its_tools_and_one_left_out_is_not_mentioned(tmp_path):
    jarvis, config, key = bench(tmp_path)
    public = (tmp_path / "keys" / "recipient").read_text().strip()
    lab_values = dict(VALUES, JARVIS_PBS_TOKEN_ID="jarvis@pbs!ro", JARVIS_PBS_TOKEN_SECRET="99999999-8888-7777-6666-555555555555",
                      JARVIS_PBS_HOSTS="192.0.2.40=pbs.lab.example", JARVIS_OPNSENSE_API_KEY="test-api-key-not-real",
                      JARVIS_OPNSENSE_API_SECRET="test-api-secret-not-real", JARVIS_OPNSENSE_HOSTS="192.0.2.1=firewall.lab.example",
                      JARVIS_DNS_SERVERS="192.0.2.53=dns1,192.0.2.54=dns2", JARVIS_DNS_LAB_NAME="jarvis.lab.example", JARVIS_SWITCH_USER="viewer",
                      JARVIS_SWITCH_PASSWORD="switch-password-not-real", JARVIS_SWITCH_HOST="192.0.2.2")
    vault_file(config.env_dir / "lab.enc.env", lab_values, public)
    jarvis.refresh()
    # The pinned certificates are the owner's to put there; until then each system says so.
    assert jarvis.lab["pbs"].endswith("pbs.pem is missing (the backup server's own certificate; copy it there)")
    assert jarvis.lab["opnsense"].endswith("opnsense.pem is missing (the firewall's own web certificate; copy it there)")
    assert jarvis.lab["switch"].endswith("switch_known_hosts is missing (the switch's SSH host key, as ssh-keyscan prints it; check it and put it there)")
    assert jarvis.lab["proxmox"] == "" and jarvis.lab["dns"] == ""
    for name in ("pbs.pem", "opnsense.pem"):
        (config.trust_dir / name).write_bytes((config.trust_dir / "proxmox-ca.pem").read_bytes())
    (config.trust_dir / "switch_known_hosts").write_text("192.0.2.2 ssh-ed25519 made-up-key-never-read-by-this-test\n")
    assert jarvis.refresh() is True, "a certificate put in place is noticed"
    assert jarvis.lab == {"proxmox": "", "pbs": "", "opnsense": "", "dns": "", "switch": ""}
    assert [name for name in jarvis.tools.names() if name != "local_status"] == sorted([
        "proxmox_cluster_status", "proxmox_guests", "proxmox_node_status", "proxmox_storage",
        "pbs_datastores", "pbs_backups", "pbs_failed_tasks", "opnsense_status", "dns_health", "switch_status"])
    # Where a system is, is not masked; how to get in, is.
    assert jarvis.vault.mask("jarvis.lab.example and 192.0.2.53=dns1,192.0.2.54=dns2") == \
        "jarvis.lab.example and 192.0.2.53=dns1,192.0.2.54=dns2"
    assert jarvis.vault.mask("key test-api-secret-not-real") == "key [JARVIS_OPNSENSE_API_SECRET]"
    assert jarvis.vault.mask("192.0.2.2 switch-password-not-real") == "192.0.2.2 [JARVIS_SWITCH_PASSWORD]"
    # A system the vault does not mention is left out without a word; a bad value is said, and the rest still work.
    vault_file(config.env_dir / "lab.enc.env", dict(VALUES, JARVIS_DNS_SERVERS="dns1.example", JARVIS_DNS_LAB_NAME="x.example"), public)
    jarvis.refresh()
    assert jarvis.lab == {"proxmox": "", "dns": "cannot be set up: ValueError"}
    assert [name for name in jarvis.tools.names() if not name.startswith("proxmox_")] == ["local_status"]
    run(jarvis.close())
