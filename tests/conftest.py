"""Test bench for the installers.

The installers are run for real, as root, against stand-ins for the Proxmox tools (tests/stubs). The system
folders they write to (/etc, /opt, /usr/bin, ...) are private copies made with overlay mounts in a mount
namespace, so one test plays both the Proxmox node and the container and leaves this machine untouched.

Needs: root, unshare, overlay mounts, git 2.34 or newer, ssh-keygen. Tests are skipped where those are missing.
"""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STUBS = REPO / "tests" / "stubs"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Documentation ranges only: see scripts/leak_gate.py.
MAC = "00:00:5E:00:53:89"
ADDRESS = "192.0.2.89"


# The tests of Jarvis's own code (tests/core) import it, so they need its packages in the Python that runs
# pytest. Where those are missing the folder is left out, and the run says so at its start and at its end.
CORE_RUNS = all(importlib.util.find_spec(name) is not None for name in ("httpx", "cryptography"))
CORE_LEFT_OUT = ("tests/core was NOT run: this Python lacks the packages of brain/requirements.txt "
                 "(make an environment with them and pytest, and run pytest with its python)")


def pytest_ignore_collect(collection_path):
    if not CORE_RUNS and REPO / "tests" / "core" in (collection_path, *collection_path.parents):
        return True
    return None


def pytest_report_header():
    return None if CORE_RUNS else CORE_LEFT_OUT


def pytest_terminal_summary(terminalreporter):
    if not CORE_RUNS:
        terminalreporter.write_line(CORE_LEFT_OUT, yellow=True)


def run(*args, cwd=None, check=True):
    # The machine's own git settings (a signing program, say) must not leak into the tests.
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=check,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=REPO, check=True,
                         stdout=subprocess.PIPE).stdout
    return [name for name in out.decode().split("\0") if name and (REPO / name).is_file()]


def wheels() -> Path | None:
    """The packages brain/requirements.txt names, fetched once so that the installer's pip never needs the
    internet in a test. None when they are not there and cannot be fetched."""
    lock = (REPO / "brain" / "requirements.txt").read_bytes()
    # For the python3 the installer under test will use, which need not be the one running these tests.
    python = subprocess.run(["python3", "-c", "import sys; print(sys.version)"], stdout=subprocess.PIPE).stdout
    name = hashlib.sha256(lock + python).hexdigest()[:16]
    folder = Path.home() / ".cache" / "jarvis-tests" / f"wheels-{name}"
    if not (folder / ".complete").exists():
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        fetched = subprocess.run(["python3", "-m", "pip", "download", "-q", "--disable-pip-version-check", "--only-binary", ":all:",
                                  "--require-hashes", "-r", str(REPO / "brain" / "requirements.txt"), "-d", str(folder)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if fetched.returncode != 0:
            shutil.rmtree(folder, ignore_errors=True)
            return None
        (folder / ".complete").touch()
    return folder


class PretendOllama:
    """What Jarvis's own code talks to in a test: the same pretend Ollama tests/stubs/curl plays for the
    installer, steered by the same files in the test's state folder, plus:
      model-says-nothing     the model answers with no words
      chat-fails             a question to the model is answered with an error
      chat-slow              holds a number: the model takes that many seconds to answer
    """

    def __init__(self, state: Path):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body: str):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _answer(self):
                if int(self.headers.get("Content-Length") or 0):
                    self.rfile.read(int(self.headers["Content-Length"]))
                if not (state / "ollama-running").exists() or (state / "ollama-down").exists():
                    return self._send(503, "{}")
                path = self.path
                with open(state / "calls", "a") as log:
                    log.write(f"jarvis-asks {path}\n")
                if path == "/api/tags":
                    models = (state / "ollama-models").read_text().split() if (state / "ollama-models").exists() else []
                    return self._send(200, json.dumps({"models": [{"name": name} for name in models]}))
                if path == "/api/ps":
                    return self._send(200, json.dumps({"models": []}))
                if path == "/api/chat":
                    if (state / "chat-fails").exists():
                        return self._send(500, '{"error": "the pretend model fails"}')
                    if (state / "chat-slow").exists():
                        time.sleep(float((state / "chat-slow").read_text()))
                    word = "" if (state / "model-says-nothing").exists() else "Ready."
                    lines = [{"message": {"role": "assistant", "content": word}, "done": False},
                             {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 2, "eval_duration": 100_000_000}]
                    return self._send(200, "".join(json.dumps(line) + "\n" for line in lines))
                return self._send(404, "{}")

            do_GET = do_POST = _answer

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def namespace_works(tmp: Path) -> bool:
    if os.geteuid() != 0 or not shutil.which("unshare") or not shutil.which("ssh-keygen"):
        return False
    probe = subprocess.run(["unshare", "-m", "bash", str(STUBS / "namespace.sh"), str(tmp / "probe"), "true"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode == 0


class Bench:
    """One pretend Proxmox node with one pretend container, and the repository the installers fetch."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.state = tmp / "state"
        self.state.mkdir()
        (self.state / "address").write_text(ADDRESS)
        self.keys = tmp / "keys"
        self.keys.mkdir()
        self.owner = self.new_key("owner")
        self.wheels = wheels()
        self.ollama = PretendOllama(self.state)
        self.src = tmp / "src"        # the working copy releases are made in; also what the node "downloaded"
        self.remote = tmp / "remote.git"
        self._make_repository()

    # ------------------------------------------------------------ releases

    def new_key(self, name: str) -> Path:
        key = self.keys / name
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", key)
        return key

    def signers(self, *keys: Path) -> str:
        return "".join(f'{key.name}@example.org namespaces="git" {key.with_suffix(".pub").read_text()}' for key in keys)

    def git(self, *args, check=True):
        return run("git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "-c", "gpg.format=ssh",
                   "-c", "gpg.ssh.program=ssh-keygen", *args, cwd=self.src, check=check)

    def _make_repository(self):
        for name in tracked_files():
            target = self.src / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, target)
        (self.src / "trust" / "allowed_signers").write_text(self.signers(self.owner))
        # A pretend Ollama release, and the release under test names it by its checksum as the real one is named.
        pack = self.tmp / "ollama-pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "lib" / "ollama").mkdir(parents=True)
        (pack / "bin" / "ollama").write_text("#!/bin/sh\necho 'ollama version is 9.9.9'\n")
        (pack / "bin" / "ollama").chmod(0o755)
        (pack / "lib" / "ollama" / "libggml-base.so").write_text("not a library\n")
        archive = self.state / "ollama.tar.zst"
        subprocess.run(f"tar -c -C '{pack}' . | zstd -q -o '{archive}'", shell=True, check=True)
        digest = subprocess.run(["sha256sum", str(archive)], check=True, text=True, stdout=subprocess.PIPE).stdout.split()[0]
        (self.src / "brain" / "ollama").write_text(f"version=v9.9.9\nsha256={digest}\n")
        # And sops: the stand-in in tests/stubs is what the pretend release hands out, named by its checksum.
        shutil.copy(STUBS / "sops", self.state / "sops-download")
        digest = subprocess.run(["sha256sum", str(self.state / "sops-download")], check=True, text=True,
                                stdout=subprocess.PIPE).stdout.split()[0]
        (self.src / "brain" / "sops").write_text(f"version=v9.9.8\nsha256={digest}\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        run("git", "init", "-q", "--bare", "-b", "main", self.remote)
        self.git("remote", "add", "origin", self.remote)
        self.git("push", "-q", "origin", "main")

    def commit(self, message: str, files: dict[str, str] | None = None, push: bool = True):
        for name, text in (files or {}).items():
            path = self.src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        if push:
            self.git("push", "-q", "origin", "main")

    def release(self, version: str, key: Path | None = "owner", files: dict[str, str] | None = None,
                message: str | None = None):
        """A new commit tagged `version`: signed by `key`, or a plain unsigned tag when key is None."""
        self.commit(f"release {version}", files)
        self.tag(version, key, message)
        self.git("push", "-q", "origin", version)

    def tag(self, name: str, key: Path | None = "owner", message: str | None = None, at: str = "HEAD"):
        message = message or f"Jarvis release {name}"
        if key == "owner":
            key = self.owner
        if key is None:
            self.git("tag", "-a", name, "-m", message, at)
        else:
            self.git("-c", f"user.signingkey={key}", "tag", "-s", name, "-m", message, at)

    # ------------------------------------------------------------ running

    def env(self, **extra) -> dict[str, str]:
        env = {
            "PATH": f"{STUBS}:{SYSTEM_PATH}",
            "HOME": "/root",
            "STUB_STATE": str(self.state),
            "STUB_SRC": str(self.src),
            "JARVIS_NET_TRIES": "2",
            "JARVIS_NET_PAUSE": "0",
            "JARVIS_DESKTOP_WAIT": "4",
            "JARVIS_DESKTOP_PAUSE": "0",
            "JARVIS_OLLAMA_WAIT": "3",
            "JARVIS_OLLAMA_PAUSE": "0",
            "JARVIS_SERVICE_WAIT": "3",
            "JARVIS_SERVICE_PAUSE": "0",
            "JARVIS_PLAIN": "1",
            # Jarvis's own code asks the pretend Ollama of this test, and pip takes its packages from the
            # folder they were fetched to once.
            "JARVIS_OLLAMA_URL": self.ollama.url,
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": str(self.wheels),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            # The installers name a GitHub repository; here that name leads to the test's own.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.file://{self.remote}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/test/jarvis.git",
        }
        env.update({key: str(value) for key, value in extra.items()})
        return env

    def ns(self, *command, env=None, check=False, terminal_input: str | None = None):
        """Runs a command in the private file system. With terminal_input it gets a terminal and those keystrokes;
        without, it has no terminal at all, so any question it tried to ask would fail."""
        inner = ["unshare", "-m", "bash", str(STUBS / "namespace.sh"), str(self.tmp / "system"), *map(str, command)]
        env = env or self.env()
        if terminal_input is None:
            return subprocess.run(["setsid", "-w", *inner], env=env, check=check, text=True,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        quoted = " ".join("'" + part.replace("'", "'\\''") + "'" for part in inner)
        return subprocess.run(["script", "-qefc", quoted, "/dev/null"], env=env, check=check, text=True,
                              input=terminal_input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def sh(self, script: str, **kwargs):
        return self.ns("bash", "-c", script, **kwargs)

    def answers(self, role: str = "brain", **settings) -> Path:
        values = {"ctid": "189", "repo": "test/jarvis", "ram": "512", "gpu": "none", "mac": MAC,
                  "vlan": "100", "expect_ip": ADDRESS}
        values.update(settings)
        self.answer_files = getattr(self, "answer_files", 0) + 1
        path = self.tmp / f"{role}-{self.answer_files}.vars"
        path.write_text("# test answers\n" + "".join(f"var_{k}={v}\n" for k, v in values.items() if v is not None))
        return path

    def install(self, role: str = "brain", answers: Path | None = None, **env):
        if role == "brain" and self.wheels is None:
            pytest.skip("needs the packages of brain/requirements.txt, fetched once from PyPI")
        script = self.src / "ct" / ("jarvis.sh" if role == "brain" else "jarvis-desktop.sh")
        answers = answers or self.answers(role)
        return self.ns("bash", script, env=self.env(JARVIS_ANSWERS=answers, **env))

    def calls(self, tool: str | None = None) -> list[str]:
        log = self.state / "calls"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if tool is None or line.startswith(tool + " ")]

    def read(self, path: str) -> str:
        return self.sh(f"cat {path}").stdout

    def head(self) -> str:
        """The commit the container's code is at."""
        return self.sh("git -C /opt/jarvis rev-parse HEAD").stdout.strip()

    def commit_of(self, ref: str) -> str:
        return self.git("rev-parse", f"{ref}^{{commit}}").stdout.strip()

    def version(self) -> str:
        """The release recorded as installed in the container."""
        return self.sh("sed -n 's/^name=//p' /etc/jarvis/release 2>/dev/null").stdout.strip()


@pytest.fixture
def bench(tmp_path):
    if not namespace_works(tmp_path):
        pytest.skip("needs root, unshare with overlay mounts, and ssh-keygen")
    made = Bench(tmp_path)
    yield made
    made.ollama.close()
