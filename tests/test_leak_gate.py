"""The leak gate: what it refuses, what it lets through, and that this repository passes it.

Values the gate refuses are put together at run time, so this file passes the gate itself.
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("leak_gate", REPO / "scripts" / "leak_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def dotted(*parts):
    return ".".join(str(part) for part in parts)


def found(text, deny=()):
    return [what for _, what in gate.scan_text(text, [re.compile(p, re.IGNORECASE) for p in deny])]


@pytest.mark.parametrize("address", [dotted(10, 1, 2, 3), dotted(192, 168, 77, 23), dotted(172, 16, 0, 9),
                                     dotted(172, 31, 255, 254), dotted(100, 88, 4, 5), dotted(169, 254, 7, 7)])
def test_private_addresses_are_refused(address):
    assert found(f"host = {address}") == [f"private address: {address}"]
    assert found(f"https://{address}:8443/x") == [f"private address: {address}"]
    assert found(f"backup.{address}.tar") == [f"private address: {address}"]
    assert found(f"pve.{address}.nip.io and again {address}") == [f"private address: {address}"]


def test_private_addresses_in_other_spellings():
    assert found("host = " + dotted("010", 1, 2, 3)) == ["private address: " + dotted("010", 1, 2, 3)]
    reverse = dotted(23, 77, 168, 192) + ".in-addr.arpa"
    assert found(f"PTR {reverse}.") == [f"private address: {reverse}"]
    assert found("PTR " + dotted(10, 2, 0, 192) + ".in-addr.arpa") == []  # a documentation address, reversed
    local = "fd" + "12:3456:789a:1::1"
    assert found(f"listen [{local}]:8443") == [f"private address: {local}"]
    assert found("2001:db8::1 and ::1 and fe80::1") == []
    # Glued to other dotted numbers it is still an address; such a line needs the marker when it is not one.
    glued = dotted(1, 10, 2, 3, 4)
    assert found("version " + glued) == ["private address: " + dotted(10, 2, 3, 4)]
    assert found("version " + glued + "  # leak-ok") == []
    open_ended = dotted(192, 168, 77) + ".x"
    assert found(f"the lab network is {open_ended}") == [f"private address: {open_ended}"]
    dashed = "-".join(["192", "168", "77", "23"])
    assert found(f"host ip-{dashed}.lan") == [f"private address: {dashed}"]
    overlay = "-".join(["100", "88", "4", "6"])
    assert found(f"peer-{overlay}") == [f"private address: {overlay}"]
    assert found("peers are " + dotted(100, 88, 4) + ".x") == ["private address: " + dotted(100, 88, 4) + ".x"]
    assert found("on 2001-02-03-04-05 and 192.0.2.x and 1.2.x") == []


@pytest.mark.parametrize("text", [
    "192.0.2.10", "198.51.100.7", "203.0.113.200", "127.0.0.1", "0.0.0.0", "8.8.8.8",
    dotted(10, 0, 0, 0) + "/8", dotted(192, 168, 0, 0) + "/16", dotted(172, 16, 0, 0) + "/12",
    "Windows build " + dotted(10, 0, 19045, 1), dotted(172, 32, 0, 1), dotted(100, 63, 0, 1),
    dotted(169, 253, 1, 1), "1.2.3", "v10.2.3", "300.168.1.1",
])
def test_other_addresses_pass(text):
    assert found(text) == []


def test_hardware_addresses():
    mac = ":".join(["02", "42", "AC", "11", "00", "02"])
    dashed = "-".join(["02", "AB", "CD", "EF", "01", "23"])
    assert found(f"hwaddr={mac},ip=dhcp") == [f"hardware address: {mac}"]
    assert found(f"MAC {dashed}.") == [f"hardware address: {dashed}"]
    assert f"hardware address: {mac}" in found(f"mac:{mac}")
    assert found(f"x{mac}y") == [f"hardware address: {mac}"]
    cisco = ".".join(["0242", "ac11", "0002"])
    assert found(f"mac-address {cisco}") == [f"hardware address: {cisco}"]
    assert found("hwaddr=00:00:5E:00:53:5A") == []
    assert found("hwaddr=00:00:5e:00:53:c8") == []
    assert found("at 2001.0203.0405 and 12:30:45 and 1234.5678") == []


def test_keys_fingerprints_and_tokens():
    assert found("-----BEGIN " + "OPENSSH PRIVATE KEY-----") == ["key or certificate block: -----BEGIN O..."]
    assert found("-----BEGIN " + "CERTIFICATE-----") == ["key or certificate block: -----BEGIN C..."]
    assert found("-----begin " + "rsa private key-----") == ["key or certificate block: -----begin r..."]
    assert found("key: LS0tLS1CRUdJTi" + "BPUEVOU1NIIFBSSVZBVEUgS0VZ") == ["key or certificate block, encoded: LS0tLS1CRUdJ..."]
    assert found("PuTTY-User-Key-File-" + "3: ssh-ed25519") == ["PuTTY key: PuTTY-User-K..."]
    assert found("AGE-SECRET-KEY-" + "PQ-1" + "Q" * 58) == ["age secret key: AGE-SECRET-K..."]
    assert found("age-secret-key-" + "1" + "q" * 58) == ["age secret key: age-secret-k..."]
    assert found("PrivateKey = " + "a" * 43 + "=") == ["WireGuard key: PrivateKey =..."]
    assert found("Authorization: PVEAPIToken=" + "jarvis@pve!ro=" + "0" * 8 + "-0000-0000-0000-" + "0" * 12) == [
        "Proxmox API token: PVEAPIToken=..."]
    assert found("-----BEGIN AGE " + "ENCRYPTED FILE-----") == ["age encrypted file: -----BEGIN A..."]
    assert found("sops: ENC" + "[AES256_GCM,data:abc]") == ["sops encrypted value: ENC[AES256_G..."]
    assert found("pin sha256/" + "/" + "A" * 43 + "=") == ["certificate pin: sha256//AAAA..."]
    assert found("JARVIS_AUDIT_SIGNING_" + "KEY=" + "Zm9v" * 8) == ["assigned secret: JARVIS_AUDIT..."]
    assert found("export CLAUDE_CODE_OAUTH_" + "TOKEN='" + "x" * 30 + "'") == ["assigned secret: export CLAUD..."]
    # Names of settings, placeholders, paths and code are not secrets.
    for harmless in ("JARVIS_AUDIT_SIGNING_KEY=", "JARVIS_TOKEN=change-me", "JARVIS_KEY_FILE=/etc/jarvis/secrets/door.pem.example",
                     "JARVIS_PVE_TOKEN_SECRET=${PVE_SECRET_FROM_THE_VAULT}", "    token = read_token_from_environment(name)",
                     "JARVIS_WEBTOP_CDP_KEY  the door's client key, base64"):
        assert found(harmless) == [], harmless
    assert found("ey" + "J" + "a" * 20 + ".ey" + "J" + "b" * 20 + "." + "c" * 20) == ["signed web token: eyJaaaaaaaaa..."]
    assert found("AGE-SECRET-KEY-" + "1" + "Q" * 58) == ["age secret key: AGE-SECRET-K..."]
    assert found("recipient " + "age" + "1" + "q" * 58) == ["age recipient: age1qqqqqqqq..."]
    assert found("SHA256:" + "a" * 43) == ["key fingerprint: SHA256:aaaaa..."]
    assert "key fingerprint: 5e:5e:5e:5e:..." in found(":".join(["5e"] * 32))
    assert found("token gh" + "p_" + "A" * 36) == ["GitHub token: ghp_AAAAAAAA..."]
    assert found("sk-" + "ant-" + "x" * 30) == ["Anthropic token: sk-ant-xxxxx..."]
    # The public half of a release signing key is meant to be published.
    assert found('owner namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake') == []


def test_the_marker_excuses_an_address_but_never_a_key_or_a_site_pattern():
    address = dotted(172, 17, 0, 1)
    assert found(f"docker0 is {address}  # leak-ok") == []
    assert found("-----BEGIN " + "OPENSSH PRIVATE KEY-----  # leak-ok") != []
    assert found("AGE-SECRET-KEY-" + "1" + "Q" * 58 + "  # leak-ok") != []
    assert found("see wiki.example.internal  # leak-ok", deny=[r"example\.internal"]) == [
        r"site pattern /example\.internal/: example.internal"]
    assert found("Example.INTERNAL", deny=[r"example\.internal"]) != []


@pytest.mark.parametrize("path,refused", [
    (".env", True), ("deploy/.env", True), (".env.local", True), ("vault/jarvis.enc.env", True),
    ("site.vars", True), ("ct/my.vars", True), ("cert.pem", True), ("door.key", True), ("id_ed25519", True),
    ("id_ed25519.pub", True), ("keys.txt", True), ("cdp-key.b64", True),
    (".ENV", True), ("prod.Env", True), ("site.VARS", True), ("cert.PEM", True), ("ID_RSA", True), (".envrc", True),
    (".env-prod", True), ("x.env.bak", True), ("ca.crt", True), ("key.ppk", True), ("age-key.txt", True),
    (".netrc", True), (".env~", True), ("jarvis.env~", True), ("lab.vars.bak", True), ("lab.vars~", True),
    ("identity.age", True), ("age-identity.txt", True), ("wg0.conf", True), ("defaults/site.vars", True), ("defaults/lab/example.vars", True), ("x/defaults/example.vars", True),
    ("defaults/example.vars", False), ("defaults/example-desktop.vars", False), ("jarvis.env.example", False),
    ("README.md", False), ("defaults/example-lab.vars", True),
    ("trust/allowed_signers", False), ("src/environment.py", False),
])
def test_file_names(path, refused):
    assert gate.name_refused(path) is refused


@pytest.fixture
def scratch(tmp_path):
    """A repository of its own with the gate and its hooks, beside (not around) whatever else a test makes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    tmp_path = repo

    def git(*args, check=True):
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null")
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args], cwd=tmp_path, env=env,
                              check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    git("init", "-q")
    (tmp_path / "scripts" / "hooks").mkdir(parents=True)
    for name in ("leak_gate.py", "hooks/pre-commit", "hooks/pre-merge-commit", "hooks/commit-msg", "hooks/pre-push"):
        target = tmp_path / "scripts" / name
        target.write_bytes((REPO / "scripts" / name).read_bytes())
        target.chmod(0o755)
    git("config", "core.hooksPath", "scripts/hooks")
    git("add", "-A")
    git("commit", "-q", "-m", "gate")
    return tmp_path, git


def test_the_hook_refuses_a_commit_and_history_mode_finds_an_old_one(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    (repo / "notes.md").write_text("the switch is at " + dotted(192, 168, 77, 2) + "\n")
    (repo / ".env").write_text("TOKEN=x\n")
    git("add", "-A", "-f")
    refused = git("commit", "-q", "-m", "leak", check=False)
    assert refused.returncode != 0
    assert "notes.md:1: private address" in refused.stdout
    assert ".env: a file of this name" in refused.stdout
    assert "2 finding(s)" in refused.stdout

    # A clean file, but the message names a lab address: refused as well.
    git("reset", "-q")
    (repo / "notes.md").unlink()
    (repo / ".env").unlink()
    (repo / "ok.md").write_text("fine\n")
    git("add", "ok.md")
    refused = git("commit", "-q", "-m", "moved the proxy to " + dotted(192, 168, 77, 30), check=False)
    assert refused.returncode != 0 and "commit message:1: private address" in refused.stdout
    git("reset", "-q")
    (repo / "ok.md").unlink()
    (repo / "notes.md").write_text("the switch is at " + dotted(192, 168, 77, 2) + "\n")
    (repo / ".env").write_text("TOKEN=x\n")
    git("add", "-A", "-f")

    # Committed past the hook, removed again: still found in the history.
    git("commit", "-q", "--no-verify", "-m", "leak")
    git("rm", "-q", "notes.md", ".env")
    git("commit", "-q", "-m", "tidy")
    gate_cmd = [sys.executable, "scripts/leak_gate.py"]
    assert subprocess.run([*gate_cmd, "--all"], cwd=repo, stdout=subprocess.PIPE).returncode == 0
    history = subprocess.run([*gate_cmd, "--history"], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert history.returncode == 1 and ":notes.md:1: private address" in history.stdout

    # And the push hook runs exactly that check.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    pushed = git("push", "-q", str(remote), "HEAD:main", check=False)
    assert pushed.returncode != 0 and ":notes.md:1: private address" in pushed.stdout


def test_a_merge_is_checked_and_a_message_in_the_history_too(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    main = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git("checkout", "-q", "-b", "side")
    (repo / "side.md").write_text("gateway " + dotted(10, 0, 0, 1) + "\n")
    git("add", "-A")
    git("commit", "-q", "--no-verify", "-m", "side work")
    git("checkout", "-q", main)
    (repo / "main.md").write_text("fine\n")
    git("add", "-A")
    git("commit", "-q", "-m", "main work")
    merged = git("merge", "--no-ff", "-m", "merge side", "side", check=False)
    assert merged.returncode != 0 and "side.md:1: private address" in merged.stdout

    git("merge", "--abort", check=False)
    git("commit", "-q", "--no-verify", "--allow-empty", "-m", "note: nas is " + dotted(192, 168, 77, 40))
    history = subprocess.run([sys.executable, "scripts/leak_gate.py", "--history"], cwd=repo, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert history.returncode == 1 and ": commit message:1: private address" in history.stdout


def test_text_in_other_encodings_and_odd_files(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    address = dotted(192, 168, 77, 11)
    (repo / "out.txt").write_bytes(("node " + address + "\r\n").encode("utf-16"))        # PowerShell's > writes this
    (repo / "raw.txt").write_bytes(("node " + address + "\n").encode("utf-16-le"))        # the same, unmarked
    (repo / "2:x").write_text("odd name, clean\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.md").write_text("deep " + address + "\n")
    odd = repo / b"caf\xe9.md".decode("utf-8", "surrogateescape")
    odd.write_text("odd " + address + "\n")
    for scope in ([], ["--all"]):
        git("add", "-A")
        out = subprocess.run([sys.executable, str(repo / "scripts" / "leak_gate.py"), *scope], cwd=repo / "sub", text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # started from a subfolder
        assert out.returncode == 1, out.stdout
        for name in ("out.txt:1:", "raw.txt:1:", "sub/deep.md:1:", "caf\\xe9.md:1:"):
            assert name + " private address: " + address in out.stdout, (name, out.stdout)
        assert "2:x" not in out.stdout and "4 finding(s)" in out.stdout


def test_a_repository_inside_the_repository_is_refused_and_errors_are_refusals(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    inner = repo / "vendor"
    inner.mkdir()
    for command in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", *command], cwd=inner, check=True)
    git("add", "vendor")
    out = subprocess.run([sys.executable, "scripts/leak_gate.py"], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert out.returncode == 1 and "vendor: a repository inside the repository" in out.stdout

    gate_file = str(repo / "scripts" / "leak_gate.py")
    outside = subprocess.run([sys.executable, gate_file, "--all"], cwd=tmp_path.parent, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env={"PATH": "/usr/bin:/bin", "GIT_CEILING_DIRECTORIES": "/"})
    assert outside.returncode == 2 and "could not run" in outside.stdout
    bad = tmp_path / "bad"
    bad.write_bytes(b"fine\n(unclosed\n\xff\xfe\n")
    broken = subprocess.run([sys.executable, gate_file, "--denylist", str(bad)], cwd=repo, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert broken.returncode == 2 and "line 2 is not a regular expression" in broken.stdout


def test_a_site_denylist_is_applied(scratch, tmp_path):
    repo, git = scratch
    deny = tmp_path / "deny"
    deny.write_text("# the lab's own names\nexample\\.internal\n")
    (repo / "doc.md").write_text("open https://wiki.Example.internal/\n")
    git("add", "-A")
    out = subprocess.run([sys.executable, "scripts/leak_gate.py", "--denylist", str(deny)], cwd=repo, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert out.returncode == 1 and "doc.md:1: site pattern" in out.stdout and "1 site patterns" in out.stdout


def test_this_repository_passes(tmp_path):
    empty = tmp_path / "empty"
    empty.write_text("")
    for scope in ("--all", "--history"):
        out = subprocess.run([sys.executable, "scripts/leak_gate.py", scope, "--denylist", str(empty)], cwd=REPO,
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert out.returncode == 0, out.stdout


def test_what_a_push_sends_is_checked_whatever_it_is(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    main = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    address = dotted(192, 168, 77, 23)
    assert git("push", "-q", str(remote), f"HEAD:refs/heads/{main}", check=False).returncode == 0

    # A stash is not pushed, so what is in it does not stand in the way.
    (repo / "wip.md").write_text("try " + address + "\n")
    git("add", "wip.md")
    git("stash", "-q")
    assert git("push", "-q", str(remote), f"HEAD:refs/heads/{main}", check=False).returncode == 0

    # An annotated tag's message, as a release tag has one.
    git("tag", "-a", "v9.9.9", "-m", "Jarvis release v9.9.9, tested on " + address)
    refused = git("push", "-q", str(remote), "v9.9.9", check=False)
    assert refused.returncode != 0 and ": tag message:" in refused.stdout and "private address: " + address in refused.stdout
    git("tag", "-d", "v9.9.9")

    # The name of a branch, and the name of a file.
    git("branch", "fix-" + address)
    refused = git("push", "-q", str(remote), "fix-" + address, check=False)
    assert refused.returncode != 0 and ": its name:1: private address: " + address in refused.stdout
    (repo / (address + ".conf")).write_text("clean inside\n")
    git("add", "-A")
    refused = git("commit", "-q", "-m", "a host file", check=False)
    assert refused.returncode != 0 and "in the file name: private address: " + address in refused.stdout


def test_who_made_a_commit_is_checked_against_the_site_patterns(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    deny = tmp_path / "deny"
    deny.write_text("example\\.internal\n")
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", str(deny))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    (repo / "ok.md").write_text("fine\n")
    git("add", "-A")
    git("-c", "user.email=root@pve.example.internal", "commit", "-q", "-m", "fine")  # what git makes up on a lab machine
    refused = git("push", "-q", str(remote), "HEAD:refs/heads/main", check=False)
    assert refused.returncode != 0 and ": author and committer:1: site pattern" in refused.stdout


def test_a_push_without_site_patterns_is_refused_until_it_is_said_there_are_none(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    monkeypatch.delenv("JARVIS_LEAK_DENYLIST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    refused = git("push", "-q", str(remote), "HEAD:refs/heads/main", check=False)
    assert refused.returncode != 0 and "there is no file of site patterns" in refused.stdout
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    assert git("push", "-q", str(remote), "HEAD:refs/heads/main", check=False).returncode == 0


def test_a_commit_shown_with_its_change_is_judged_by_its_message_only(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    message = tmp_path / "COMMIT_EDITMSG"
    cut = "# ------------------------ >8 ------------------------\n"
    message.write_text("remove the old address\n" + cut + "-host = " + dotted(10, 1, 2, 3) + "\n")
    gate_cmd = [sys.executable, "scripts/leak_gate.py", "--message"]
    assert subprocess.run([*gate_cmd, str(message)], cwd=repo, stdout=subprocess.PIPE).returncode == 0
    message.write_text("# see " + dotted(10, 1, 2, 3) + "\n" + cut)
    assert subprocess.run([*gate_cmd, str(message)], cwd=repo, stdout=subprocess.PIPE).returncode == 1
    missing = subprocess.run([*gate_cmd, str(tmp_path / "nowhere")], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert missing.returncode == 2 and "could not run" in missing.stdout and "Traceback" not in missing.stdout


def test_a_push_reads_only_what_the_remote_does_not_hold_and_all_of_that(scratch, tmp_path, monkeypatch):
    repo, git = scratch
    deny = tmp_path / "deny"
    deny.write_text("first-author\n")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git("remote", "add", "origin", str(remote))
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    # The remote starts with a commit by somebody the site patterns would not let through today.
    (repo / "LICENSE").write_text("MIT\n")
    git("add", "-A")
    git("-c", "user.name=first-author", "commit", "-q", "-m", "first")
    assert git("push", "-q", "origin", "HEAD:refs/heads/main", check=False).returncode == 0
    git("fetch", "-q", "origin")
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", str(deny))

    # A later push is judged by what it adds: an update of the branch, and a new branch off it.
    (repo / "more.md").write_text("fine\n")
    git("add", "-A")
    git("commit", "-q", "-m", "more")
    assert git("push", "-q", "origin", "HEAD:refs/heads/main", check=False).returncode == 0
    assert git("push", "-q", "origin", "HEAD:refs/heads/topic", check=False).returncode == 0
    # To somewhere this clone knows nothing about, everything is read again.
    elsewhere = tmp_path / "elsewhere.git"
    subprocess.run(["git", "init", "-q", "--bare", str(elsewhere)], check=True)
    refused = git("push", "-q", str(elsewhere), "HEAD:refs/heads/main", check=False)
    assert refused.returncode != 0 and ": author and committer:1: site pattern /first-author/" in refused.stdout

    # What is new is read, however the ref is spelled, and a tag on something that is not a commit is refused.
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    git("checkout", "-q", "-b", "nb")
    (repo / "leak.md").write_text("gw " + dotted(10, 9, 8, 7) + "\n")
    git("add", "-A")
    git("commit", "-q", "--no-verify", "-m", "leak")
    refused = git("push", "-q", "origin", "nb@{0 minutes ago}:refs/heads/y", check=False)
    assert refused.returncode != 0 and "leak.md:1: private address" in refused.stdout
    git("checkout", "-q", "-")
    git("tag", "-a", "tree-tag", "-m", "a tree", "nb^{tree}")
    refused = git("push", "-q", "origin", "tree-tag", check=False)
    assert refused.returncode != 0 and "not a commit; its content cannot be checked" in refused.stdout


def test_a_link_to_a_private_conversation_is_refused():
    link = "https://claude.ai/code/" + "session_" + "01ABCdef234567"
    assert any(what.startswith("link to a private conversation") for what in found("Claude-Session: " + link))
    assert found("see https://claude.ai/" + "chat/" + "0123abcd-ef") != []
    assert found("made with https://claude.ai and Claude Code") == []


def test_commits_and_tags_dated_in_a_local_time_zone_are_refused(scratch, monkeypatch):
    repo, git = scratch
    monkeypatch.setenv("JARVIS_LEAK_DENYLIST", "none")
    gate_run = lambda: subprocess.run([sys.executable, "scripts/leak_gate.py", "--history"], cwd=repo, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    clean = gate_run()
    assert clean.returncode == 0, clean.stdout
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-02T03:04:05+0530")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-02T03:04:05+0530")
    git("commit", "-q", "--allow-empty", "-m", "made elsewhere")
    git("tag", "-a", "v9.9.9", "-m", "Jarvis release v9.9.9")
    found_now = gate_run()
    assert found_now.returncode == 1
    for who in ("author", "committer", "tagger"):
        assert f"{who} date: dated in the time zone +0530" in found_now.stdout, found_now.stdout
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-02T10:04:05+0000")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-02T10:04:05+0000")
    git("tag", "-d", "v9.9.9")
    git("commit", "-q", "--amend", "--allow-empty", "--reset-author", "-m", "made in UTC")
    git("tag", "-a", "v9.9.9", "-m", "Jarvis release v9.9.9")
    git("reflog", "expire", "--expire=now", "--all")
    git("gc", "-q", "--prune=now")
    assert gate_run().returncode == 0
