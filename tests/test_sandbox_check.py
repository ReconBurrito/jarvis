"""desktop/sandbox-check.sh against a real browser: it must say yes with the sandbox and no without it.

Needs root (to become an ordinary user), a Chromium (JARVIS_TEST_BROWSER, or the one Playwright installs)
and a machine that lets an ordinary user make namespaces. Skipped where one of those is missing.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import REPO

BROWSER = os.environ.get("JARVIS_TEST_BROWSER") or next(
    (p for p in ("/opt/pw-browsers/chromium", shutil.which("chromium") or "", shutil.which("chromium-browser") or "")
     if p and os.access(p, os.X_OK)), "")


@pytest.fixture
def check():
    if os.geteuid() != 0 or not BROWSER or not shutil.which("setpriv"):
        pytest.skip("needs root, setpriv and a Chromium")
    home = Path(tempfile.mkdtemp(prefix="jarvis-sandbox-test-", dir="/tmp"))
    try:
        os.chmod(home, 0o755)
        script = home / "sandbox-check.sh"
        shutil.copy(REPO / "desktop" / "sandbox-check.sh", script)
        os.chmod(script, 0o755)
        os.chown(home, 65534, 65534)

        def run(browser=BROWSER, user=True, **env):
            command = ["env", f"HOME={home}", f"TMPDIR={home}", *[f"{k}={v}" for k, v in env.items()], str(script), browser]
            if user:
                command = ["setpriv", "--reuid", "65534", "--regid", "65534", "--clear-groups", *command]
            return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)

        yield run
    finally:
        subprocess.run(["pkill", "-KILL", "-f", "--", f"--user-data-dir={home}"], check=False)
        shutil.rmtree(home, ignore_errors=True)


def test_it_says_yes_with_the_sandbox_and_no_without(check):
    on = check()
    if on.returncode == 2 and "sandbox" in on.stdout.lower():
        pytest.skip("this machine does not let an ordinary user build the sandbox: " + on.stdout.strip().splitlines()[-1])
    assert on.returncode == 0, on.stdout
    assert on.stdout.splitlines() == ["sandbox layer 1, own namespaces: on", "sandbox layer 2, own system-call filter: on"]

    off = check(JARVIS_SANDBOX_EXTRA="--no-sandbox")
    assert off.returncode == 1, off.stdout
    assert off.stdout.splitlines() == ["sandbox layer 1, own namespaces: OFF", "sandbox layer 2, own system-call filter: OFF"]

    # Only the system-call filter switched off: one layer on, one off, and that is still a no.
    half = check(JARVIS_SANDBOX_EXTRA="--disable-seccomp-filter-sandbox")
    assert half.returncode == 1, half.stdout
    assert half.stdout.splitlines() == ["sandbox layer 1, own namespaces: on", "sandbox layer 2, own system-call filter: OFF"]


def test_it_does_not_guess(check):
    never = check(browser="/bin/false", JARVIS_SANDBOX_WAIT="2")
    assert never.returncode == 2 and "did not start a page process" in never.stdout
    missing = check(browser="/nowhere/chromium")
    assert missing.returncode == 2 and "is not there" in missing.stdout
    root = check(user=False)
    assert root.returncode == 2 and "not as root" in root.stdout


def test_it_leaves_nothing_behind(check):
    check()
    home = [arg for arg in check().args if arg.startswith("HOME=")][0][5:]
    assert sorted(p.name for p in Path(home).iterdir()) == ["sandbox-check.sh"]
