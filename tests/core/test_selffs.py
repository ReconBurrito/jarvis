"""jarvis-fsd and Jarvis's tools for its own machine's files. The machine is a made-up tree in a temporary folder
(jarvis-fsd's --root); everything else is the real code, socket included."""
import asyncio
import os
import socket

import pytest
from fakes import run

from jarvis.selffs.daemon import Files, serve
from jarvis.selffs.tools import SelfFiles, make_selffs_tools

KEY = "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----\nmade-up\n-----END " + "OPENSSH PRIVATE" + " KEY-----\n"   # assembled: no key text in the repository


@pytest.fixture
def machine(tmp_path):
    root = tmp_path / "root"
    for folder in ("etc/jarvis/tls", "etc/jarvis/secrets", "etc/ssh", "srv/keys", "opt/app", "proc/1", "root/.ssh",
                   "opt/jarvis/src", "var/lib/jarvis/audit"):
        (root / folder).mkdir(parents=True)
    (root / "etc/jarvis/site.env").write_text("JARVIS_ROLE=brain\n")
    (root / "etc/jarvis/tls/ca.key").write_text("made-up key\n")
    (root / "etc/jarvis/tls/ca.pem").write_text("made-up certificate\n")
    (root / "etc/jarvis/secrets/lab.enc.env").write_text("ENC[made-up]\n")
    (root / "srv/keys/vault.env").write_text("X=1\n")
    (root / "etc/shadow").write_text("root:*:1::::::\n")
    (root / "opt/app/deploy").write_text(KEY)
    (root / "opt/app/notes.txt").write_text("first line\nport = 8443\n")
    (root / "proc/1/environ").write_text("SECRET=x")
    (root / "opt/jarvis/src/vault.py").write_text("def mask(text): ...\n")
    (root / "var/lib/jarvis/audit/audit.jsonl").write_text("{}\n")
    os.symlink("../../etc/jarvis/tls/ca.key", root / "opt/app/sneaky")   # relative: this made-up machine is not at /
    files = Files(str(root), str(tmp_path / "history"), protect=("/srv/keys",))
    return root, files


def test_jarvis_reads_lists_and_finds_its_own_files_and_never_the_keys(machine):
    root, files = machine
    listed = files.handle({"op": "list", "path": "/etc/jarvis"})
    assert {e["name"]: e.get("protected", False) for e in listed["entries"]} == {"secrets": True, "site.env": False, "tls": False}
    assert [e.get("read_only") for e in listed["entries"] if e["name"] == "site.env"] == [True]
    read = files.handle({"op": "read", "path": "/etc/jarvis/site.env"})
    assert read["text"] == "JARVIS_ROLE=brain\n" and len(read["sha256"]) == 64
    for path, why in (("/etc/jarvis/tls/ca.key", "kept from Jarvis"), ("/etc/jarvis/secrets/lab.enc.env", "kept from Jarvis"),
                      ("/srv/keys/vault.env", "kept from Jarvis"), ("/etc/shadow", "kept from Jarvis"),
                      ("/proc/1/environ", "kept from Jarvis"), ("/opt/app/deploy", "holds a private key"),
                      ("/opt/app/sneaky", "kept from Jarvis"), ("/../../etc/passwd", "leads outside"),
                      ("relative/path", "full path")):
        answer = files.handle({"op": "read", "path": path})
        assert why in answer.get("error", ""), (path, answer)
    found = files.handle({"op": "find", "path": "/", "contains": "8443"})
    assert found["found"] == [{"path": "/opt/app/notes.txt", "line": 2, "text": "port = 8443"}]
    everything = [f["path"] for f in files.handle({"op": "find", "path": "/"})["found"]]
    assert "/etc/jarvis/site.env" in everything and not any(p.startswith(("/etc/jarvis/secrets", "/proc", "/srv/keys")) for p in everything)
    assert "/etc/jarvis/tls/ca.key" not in everything and "/etc/shadow" not in everything


def test_every_change_can_be_undone_and_keys_are_never_written(machine):
    root, files = machine
    before = files.handle({"op": "read", "path": "/opt/app/notes.txt"})
    os.chmod(root / "opt/app/notes.txt", 0o640)
    changed = files.handle({"op": "write", "path": "/opt/app/notes.txt", "content": "new\n", "expect": before["sha256"]})
    assert changed["done"] and (root / "opt/app/notes.txt").read_text() == "new\n"
    assert oct((root / "opt/app/notes.txt").stat().st_mode & 0o777) == "0o640", "the file keeps its mode"
    stale = files.handle({"op": "write", "path": "/opt/app/notes.txt", "content": "x\n", "expect": before["sha256"]})
    assert "changed since it was read" in stale["error"]
    made = files.handle({"op": "mkdir", "path": "/opt/app/conf.d"})
    created = files.handle({"op": "write", "path": "/opt/app/conf.d/jarvis.conf", "content": "a = 1\n", "mode": "600"})
    assert created["created"] and oct((root / "opt/app/conf.d/jarvis.conf").stat().st_mode & 0o777) == "0o600"
    gone = files.handle({"op": "delete", "path": "/opt/app/conf.d/jarvis.conf"})
    assert gone["done"] and not (root / "opt/app/conf.d/jarvis.conf").exists()
    history = [c["what"] for c in files.handle({"op": "changes"})["changes"]]
    assert history == ["delete", "write", "mkdir", "write"]
    # Undone newest first, everything is as it was.
    for change in (gone, created, made, changed):
        undone = files.handle({"op": "undo", "change": change["change"]})
        assert undone.get("done"), undone
    assert (root / "opt/app/notes.txt").read_text() == "first line\nport = 8443\n" and not (root / "opt/app/conf.d").exists()
    assert "already undone" in files.handle({"op": "undo", "change": changed["change"]})["error"]
    # Out of order: refused, nothing lost.
    one = files.handle({"op": "write", "path": "/opt/app/notes.txt", "content": "1\n"})
    files.handle({"op": "write", "path": "/opt/app/notes.txt", "content": "2\n"})
    assert "changed again" in files.handle({"op": "undo", "change": one["change"]})["error"]
    for path, content, why in (("/etc/jarvis/tls/ca.key", "x", "kept from Jarvis"), ("/etc/jarvis/secrets/new.env", "x", "kept from Jarvis"),
                               ("/root/.ssh/authorized_keys", "x", "kept from Jarvis"), ("/opt/app/sneaky", "x", "kept from Jarvis"),
                               ("/opt/app/key", KEY, "holds a private key"), ("/opt/app/deploy", "x", "holds a private key"),
                               ("/opt/app/x", "x" * (1024 * 1024 + 1), "at most"), ("/nowhere/x", "x", "does not exist"),
                               ("/opt/jarvis/src/vault.py", "x", "can be read but not changed"),
                               ("/etc/jarvis/site.env", "x", "can be read but not changed"),
                               ("/var/lib/jarvis/audit/audit.jsonl", "", "can be read but not changed"),
                               ("/opt/app/surrogate", "\ud800", "not Unicode text")):
        answer = files.handle({"op": "write", "path": path, "content": content})
        assert why in answer.get("error", ""), (path, answer)
    assert files.handle({"op": "read", "path": "/opt/jarvis/src/vault.py"})["text"] == "def mask(text): ...\n", "read, not changed"
    os.chmod(root / "opt/app/notes.txt", 0o4755)
    assert files.handle({"op": "write", "path": "/opt/app/notes.txt", "content": "s\n", "mode": "6755"})["done"]
    assert oct((root / "opt/app/notes.txt").stat().st_mode & 0o7777) == "0o755", "set-user and set-group bits never survive"
    assert "with things in it" in files.handle({"op": "delete", "path": "/opt"})["error"]
    assert "kept from Jarvis" in files.handle({"op": "delete", "path": "/etc/shadow"})["error"]
    link = files.handle({"op": "delete", "path": "/opt/app/sneaky"})
    assert "kept from Jarvis" in link["error"], "a link to a key is left alone too"


def test_the_tools_talk_to_jarvis_fsd_over_its_socket_and_strangers_are_refused(machine, tmp_path):
    root, files = machine
    path = tmp_path / "run" / "socket"

    async def scene():
        server = asyncio.create_task(serve(files, str(path), {os.getuid()}, None))
        for _ in range(50):
            if path.exists():
                break
            await asyncio.sleep(0.05)
        tools = {t.name: t for t in make_selffs_tools(SelfFiles(path))}
        read = await tools["fs_read"].handler({"path": "/etc/jarvis/site.env"})
        wrote = await tools["fs_write"].handler({"path": "/opt/app/new.txt", "content": "hello\n"})
        changes = await tools["fs_changes"].handler({})
        undone = await tools["fs_undo"].handler({"change": wrote["change"]})
        modes = (oct(path.stat().st_mode & 0o777), oct(path.parent.stat().st_mode & 0o777))
        server.cancel()
        return read, wrote, changes, undone, modes

    read, wrote, changes, undone, modes = run(scene())
    assert read["text"] == "JARVIS_ROLE=brain\n" and wrote["done"] and changes["changes"][0]["what"] == "write"
    assert undone["done"] and not (root / "opt/app/new.txt").exists()
    assert modes == ("0o660", "0o750")

    async def stranger():
        other = tmp_path / "run2" / "socket"
        server = asyncio.create_task(serve(files, str(other), {os.getuid() + 12345}, None))
        for _ in range(50):
            if other.exists():
                break
            await asyncio.sleep(0.05)
        answer = await SelfFiles(other).ask({"op": "read", "path": "/etc/jarvis/site.env"})
        server.cancel()
        return answer

    assert run(stranger()) == {"error": "not for this user"}
    missing = run(SelfFiles(tmp_path / "nothing").ask({"op": "list", "path": "/"}))
    assert "jarvis-fsd does not answer" in missing["error"]


def test_jarvis_has_the_file_tools_only_while_jarvis_fsd_runs(tmp_path):
    from fakes import FakeOllama

    from jarvis.config import Settings
    from jarvis.core import Jarvis

    sock = tmp_path / "fs.socket"
    config = Settings(data_dir=tmp_path / "data", site=tmp_path / "site.env", env_dir=tmp_path / "secrets",
                      trust_dir=tmp_path / "trust", tls_dir=tmp_path / "tls", fs_socket=sock)
    jarvis = Jarvis(config, FakeOllama().client())
    assert jarvis.files is False and not any(n.startswith("fs_") for n in jarvis.tools.names())
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(sock))
    try:
        assert jarvis.refresh() is True and jarvis.files is True
        assert {"fs_read", "fs_write", "fs_undo"} <= set(jarvis.tools.names())
    finally:
        listener.close()
        sock.unlink()
    assert jarvis.refresh() is True and not any(n.startswith("fs_") for n in jarvis.tools.names())
    run(jarvis.close())


def test_the_installed_file_runs_alone_as_the_installer_starts_it(tmp_path):
    """The installer copies daemon.py to /usr/local/sbin/jarvis-fsd and root runs it with python3 -I: it must need
    nothing of Jarvis's package or environment."""
    import pwd
    import shutil
    import subprocess
    import sys
    import time
    from pathlib import Path

    alone = tmp_path / "jarvis-fsd"
    shutil.copy(Path(__file__).parents[2] / "src/jarvis/selffs/daemon.py", alone)
    (tmp_path / "root/etc").mkdir(parents=True)
    (tmp_path / "root/etc/hostname").write_text("brain\n")
    sock = tmp_path / "run/socket"
    me = pwd.getpwuid(os.getuid()).pw_name
    daemon = subprocess.Popen([sys.executable, "-I", str(alone), "--user", me, "--root", str(tmp_path / "root"),
                               "--history", str(tmp_path / "history"), "--socket", str(sock)], cwd=tmp_path)
    try:
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        assert run(SelfFiles(sock).ask({"op": "read", "path": "/etc/hostname"}))["text"] == "brain\n"
        assert oct((tmp_path / "history").stat().st_mode & 0o777) == "0o700"
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)


def test_no_link_hard_link_bind_or_pipe_leads_around_what_is_kept(machine, tmp_path):
    root, files = machine
    os.link(root / "etc/jarvis/tls/ca.key", root / "opt/app/hardlink")
    (root / "opt/app/shadowlink").symlink_to("../../etc/shadow")
    os.mkfifo(root / "opt/app/pipe")
    for path in ("/opt/app/hardlink", "/opt/app/shadowlink", "/opt/app/pipe"):
        assert "error" in files.handle({"op": "read", "path": path}), path
    found = files.handle({"op": "find", "path": "/opt", "contains": "root"})   # finished: the pipe did not hang it
    assert found["found"] == [], found
    assert "error" in files.handle({"op": "write", "path": "/opt/app/hardlink", "content": "x"})
    # The vault's folder reached through a link of its own (as when it is a link to another disk).
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "brain.env").write_text("TOKEN=made-up\n")
    (root / "etc/jarvis/secrets/lab.enc.env").unlink()
    (root / "etc/jarvis/secrets").rmdir()
    (root / "etc/jarvis/secrets").symlink_to(real)
    (root / "srv/mirror").symlink_to(real)   # and through another name for the same folder
    fresh = Files(str(root), str(tmp_path / "history2"), protect=("/srv/keys",))
    for path in ("/etc/jarvis/secrets/brain.env", "/srv/mirror/brain.env"):
        assert "error" in fresh.handle({"op": "read", "path": path}), path


def test_reads_come_in_whole_lines_and_folders_belong_to_their_parent(machine):
    root, files = machine
    (root / "opt/app/long.txt").write_text("".join(f"line {n} TOKEN=abcdef{n}\n" for n in range(100)))
    part = files.handle({"op": "read", "path": "/opt/app/long.txt", "offset": 25, "length": 30})
    assert part["text"].startswith("line ") and part["text"].endswith("\n"), part["text"]
    assert part["offset"] <= 25 and part["next_offset"] >= 55
    os.chown(root / "opt/app", 4321, 4321)
    made = files.handle({"op": "mkdir", "path": "/opt/app/sub"})
    info = (root / "opt/app/sub").stat()
    assert made["done"] and (info.st_uid, oct(info.st_mode & 0o777)) == (4321, "0o755")
    wrote = files.handle({"op": "write", "path": "/opt/app/sub/new.txt", "content": "x\n"})
    assert (root / "opt/app/sub/new.txt").stat().st_uid == 4321
    files.handle({"op": "delete", "path": "/opt/app/sub/new.txt"})
    gone = files.handle({"op": "delete", "path": "/opt/app/sub"})
    assert files.handle({"op": "undo", "change": gone["change"]})["done"]
    info = (root / "opt/app/sub").stat()
    assert (info.st_uid, oct(info.st_mode & 0o777)) == (4321, "0o755"), "a folder comes back as it was"
    assert wrote["done"]


def test_a_change_undone_after_its_place_was_kept_is_not_made(machine, tmp_path):
    root, files = machine
    wrote = files.handle({"op": "write", "path": "/opt/app/later.txt", "content": "x\n"})
    files.handle({"op": "delete", "path": "/opt/app/later.txt"})
    later = Files(str(root), str(tmp_path / "history"), protect=("/srv/keys", "/opt/app"))
    deleted = later.handle({"op": "changes"})["changes"][0]
    assert "kept from Jarvis" in later.handle({"op": "undo", "change": deleted["id"]})["error"]
    assert not (root / "opt/app/later.txt").exists() and wrote["done"]
    assert later.handle({"op": "nonsense"}) == {"error": "unknown request 'nonsense'"}
    assert "error" in later.handle({"op": "read", "path": "/opt/app/notes.txt", "offset": float("inf")})
    assert "error" in later.handle({"op": "undo", "change": "9" * 5000})
