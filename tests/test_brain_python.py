"""Jarvis's own Python: an environment built from pinned and checked packages, the jarvis command, and the
doctor that ends the install check. The packages come from a folder they were fetched to once
(tests/conftest.py), and the model is a pretend one."""
from conftest import REPO

CHECK = "bash /opt/jarvis/install/jarvis-install.sh --check"
LOCK = (REPO / "brain" / "requirements.txt").read_text()


def state(bench, name):
    path = bench.state / name
    return path.read_text() if path.exists() else ""


def environments(bench):
    """The environments themselves, without the two links that name the one in use and the one before it."""
    return sorted(name for name in bench.sh("ls /opt/jarvis-venv").stdout.split() if name not in ("current", "before"))


def test_install_builds_the_environment_and_the_doctor_passes(bench):
    bench.release("v0.1.0")
    done = bench.install("brain")
    assert done.returncode == 0, done.stdout
    assert "building Jarvis's Python environment" in done.stdout and "ok: Jarvis's Python environment: " in done.stdout
    # The check that ends the install went through the whole of Jarvis, to the model and back.
    assert "ok: Jarvis (a release is being installed); settings from /etc/jarvis/site.env" in done.stdout
    assert "ok: Jarvis v0.1.0; settings from /etc/jarvis/site.env" in bench.sh(CHECK).stdout
    assert "ok: tools: local_status reads this machine" in done.stdout
    assert "ok: qwen3:8b answered in " in done.stdout and "audit record 1" in done.stdout
    assert "jarvis-asks /api/chat" in state(bench, "calls")
    bench.sh("truncate -s 0 /var/lib/jarvis/audit/audit.jsonl")
    # Only root can change the environment; only jarvis can read what Jarvis keeps.
    assert bench.sh("find /opt/jarvis-venv ! -user root | wc -l").stdout == "0\n"
    assert bench.sh("stat -c '%U:%G %a' /var/lib/jarvis /var/lib/jarvis/audit /var/lib/jarvis/audit/audit.jsonl").stdout \
        == "root:jarvis 750\njarvis:jarvis 700\njarvis:jarvis 644\n"
    assert bench.sh("runuser -u jarvis -- touch /var/lib/jarvis/mine").returncode != 0
    assert len(environments(bench)) == 1 and bench.sh("readlink /opt/jarvis-venv/current").stdout.strip() == environments(bench)[0]
    assert bench.read("/opt/jarvis-venv/current/lib/python3*/site-packages/jarvis.pth") == "/opt/jarvis/src\n"
    assert "JARVIS_HOST_DESCRIPTION=a " in bench.read("/etc/jarvis/site.env")
    # Nothing was written into the release's own folder by running it.
    assert bench.sh("git -C /opt/jarvis status --porcelain --ignored").stdout == ""

    # Again: nothing is built a second time.
    again = bench.install("brain")
    assert again.returncode == 0 and "building Jarvis's Python environment" not in again.stdout, again.stdout


def test_the_jarvis_command(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    assert bench.sh("jarvis --version").stdout == "v0.1.0\n"
    asked = bench.sh("cd /root && jarvis chat Good evening 2>/dev/null")
    assert asked.returncode == 0 and asked.stdout == "Ready.\n"
    noted = bench.sh("jarvis chat Good evening 2>&1 >/dev/null").stdout
    assert noted.startswith("[qwen3:8b, audit record ") and noted.endswith("tokens a second]\n")
    # Started by root, it ran as jarvis: the log is still that user's, and it is intact.
    assert bench.sh("stat -c %U /var/lib/jarvis/audit/audit.jsonl").stdout == "jarvis\n"
    assert bench.sh("jarvis audit").stdout == "/var/lib/jarvis/audit/audit.jsonl: 3 records, chain intact\n"
    assert bench.sh("runuser -u jarvis -- jarvis doctor --quick").returncode == 0
    other = bench.sh("runuser -u nobody -- jarvis --version")
    assert other.returncode == 1 and "run this as root or as the user jarvis" in other.stdout
    # A conversation: two lines typed, then the end of input.
    talk = bench.sh("printf 'Hello\\nAnd again\\n' | jarvis 2>/dev/null")
    assert talk.returncode == 0 and talk.stdout.count("Ready.") == 2, talk.stdout
    # What was said is not in the log, only its length and fingerprint.
    assert bench.sh("grep -c 'Good evening' /var/lib/jarvis/audit/audit.jsonl").stdout == "0\n"
    # A release without the command (one from before it existed, gone back to) says so.
    bench.sh("mv /opt/jarvis/src/jarvis/__main__.py /root/aside")
    older = bench.sh("jarvis --version")
    assert older.returncode == 1 and "the release installed here has no jarvis command" in older.stdout
    bench.sh("mv /root/aside /opt/jarvis/src/jarvis/__main__.py && rm /opt/jarvis-venv/current")
    gone = bench.sh("jarvis --version")
    assert gone.returncode == 1 and "As root, run: update --repair" in gone.stdout


def test_started_by_root_jarvis_does_not_own_the_terminal_and_ctrl_c_still_reaches_it(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    (bench.state / "chat-slow").write_text("3")
    # In a terminal: the script root started has it, and what runs as jarvis has none (the "?").
    seen = bench.sh("jarvis chat Hello >/dev/null 2>&1 & sleep 1.5; ps -eo user:12,tty:8,args | grep -- '-m [j]arvis chat'; wait",
                    terminal_input="")
    lines = [line.split(None, 2) for line in seen.stdout.splitlines() if "-m jarvis chat" in line]
    assert {(user, tty == "?") for user, tty, command in lines if user == "jarvis"} == {("jarvis", True)}, seen.stdout
    assert any(user == "jarvis" for user, tty, command in lines), seen.stdout
    # Ctrl-C arrives at the script; Jarvis gets it from there, gives up the turn properly, and says so by its exit.
    stopped = bench.sh("""python3 -I -c '
import signal, subprocess, time
p = subprocess.Popen(["jarvis", "chat", "Tell me a long story"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(1.5)
p.send_signal(signal.SIGINT)
print("exit", p.wait(timeout=20), repr(p.stdout.read()))
'""")
    assert "exit 130 " in stopped.stdout, stopped.stdout
    assert bench.sh("tail -n 1 /var/lib/jarvis/audit/audit.jsonl").stdout.count('"kind":"turn_aborted"') == 1
    (bench.state / "chat-slow").unlink()
    # Typed at a terminal, line by line, to the end of input.
    talk = bench.sh("jarvis", terminal_input="Hello\nAnd again\n\x04")
    assert talk.returncode == 0 and talk.stdout.count("Ready.") == 2, talk.stdout


def test_what_the_jarvis_user_plants_is_not_followed_by_root(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    # A link where root creates Jarvis's folder. (Only root can put it there now; an older install let jarvis.)
    bench.sh("mv /var/lib/jarvis/audit /var/lib/jarvis/audit-aside && ln -s /etc/jarvis /var/lib/jarvis/audit")
    refused = bench.sh("update --repair")
    assert refused.returncode == 1 and "/var/lib/jarvis/audit is not a folder (a link or a file is in its place)" in refused.stdout
    assert bench.sh("stat -c '%U:%G' /etc/jarvis").stdout == "root:root\n"
    assert bench.sh("bash /opt/jarvis/install/jarvis-install.sh --check").returncode == 1
    bench.sh("rm /var/lib/jarvis/audit && mv /var/lib/jarvis/audit-aside /var/lib/jarvis/audit")
    # Python files in the folder root happens to stand in when it types update.
    planted = "open('/root/planted', 'a').write('ran as root\\n')\n"
    for name in ("json.py", "venv.py", "sys.py", "sysconfig.py", "pip.py", "jarvis.py", "httpx.py"):
        bench.sh(f"printf %s \"{planted}\" > /var/lib/jarvis/audit/{name}")
    bench.sh("rm -rf /opt/jarvis-venv")
    mended = bench.sh("cd /var/lib/jarvis/audit && update --repair && jarvis doctor && jarvis --version")
    assert mended.returncode == 0, mended.stdout
    assert bench.sh("test -e /root/planted || echo nothing-ran").stdout == "nothing-ran\n"
    # An install from before this release left the folder to jarvis; this one takes it back.
    bench.sh("chown jarvis:jarvis /var/lib/jarvis")
    assert bench.sh("update --repair").returncode == 0
    assert bench.sh("stat -c '%U:%G %a' /var/lib/jarvis").stdout == "root:jarvis 750\n"


def test_a_release_whose_code_does_not_load_costs_nothing(bench):
    """Same packages, broken code: the environment in use is not touched, and the way back needs no internet."""
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    before = environments(bench)
    bench.release("v0.2.0", files={"src/jarvis/cli.py": "raise RuntimeError('this release is broken')\n"})
    failed = bench.sh("update", env=bench.env(PIP_FIND_LINKS=bench.tmp / "no-packages-here"))
    assert failed.returncode == 1, failed.stdout
    assert "this release's own code does not load in its Python environment" in failed.stdout
    assert "building Jarvis's Python environment" not in failed.stdout
    assert bench.version() == "v0.1.0" and environments(bench) == before
    assert bench.sh(CHECK).returncode == 0 and bench.sh("jarvis chat Hello 2>/dev/null").stdout == "Ready.\n"


def test_going_back_after_new_packages_uses_the_environment_kept_from_before(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    first = environments(bench)
    bench.release("v0.2.0", files={"brain/requirements.txt": LOCK + "# one more line\n"})
    (bench.state / "chat-fails").touch()   # the new release installs, and then fails its check
    failed = bench.sh("update")
    assert failed.returncode == 1 and failed.stdout.count("building Jarvis's Python environment") == 1, failed.stdout
    assert bench.version() == "v0.1.0" and bench.sh("readlink /opt/jarvis-venv/current").stdout.strip() == first[0]
    assert len(environments(bench)) == 2
    # Tried again, and again: the environment gone back to is never the one cleared away.
    for _ in range(2):
        again = bench.sh("update")
        assert again.returncode == 1 and "building Jarvis's Python environment" not in again.stdout, again.stdout
        assert len(environments(bench)) == 2 and bench.sh("readlink /opt/jarvis-venv/current").stdout.strip() == first[0]
    (bench.state / "chat-fails").unlink()
    done = bench.sh("update", env=bench.env(PIP_FIND_LINKS=bench.tmp / "no-packages-here"))
    assert done.returncode == 0 and bench.version() == "v0.2.0", done.stdout
    assert len(environments(bench)) == 2 and bench.sh("readlink /opt/jarvis-venv/before").stdout.strip() == first[0]


def test_a_damaged_audit_log_is_said_but_does_not_hold_back_an_update(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    bench.sh("jarvis chat Hello")
    bench.sh("runuser -u jarvis -- sh -c 'echo not-a-record >> /var/lib/jarvis/audit/audit.jsonl'")
    bench.release("v0.2.0")
    done = bench.sh("update")
    assert done.returncode == 0 and bench.version() == "v0.2.0", done.stdout
    assert "WARNING: audit log: the last record of /var/lib/jarvis/audit/audit.jsonl cannot be read" in done.stdout
    # The owner's own doctor calls it a fault, and Jarvis does not talk over a log it cannot continue.
    looked = bench.sh("jarvis doctor")
    assert looked.returncode == 1 and "FAULT: audit log: the last record of" in looked.stdout
    refused = bench.sh("jarvis chat Hello")
    assert refused.returncode == 1 and "Jarvis will not add to a log it cannot continue" in refused.stdout


def test_a_package_that_does_not_match_its_checksum_is_not_installed(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    before = environments(bench)
    line = next(line for line in LOCK.splitlines() if line.strip().startswith("--hash=sha256:"))
    bad = "\n".join("    --hash=sha256:" + "0" * 64 + (" \\" if text.rstrip().endswith("\\") else "")
                    if text.strip().startswith("--hash=sha256:") else text for text in LOCK.splitlines()) + "\n"
    assert line and bad != LOCK
    bench.release("v0.2.0", files={"brain/requirements.txt": bad})
    failed = bench.sh("update")
    assert failed.returncode == 1, failed.stdout
    assert "Jarvis's Python packages could not be installed" in failed.stdout and "HASH" in failed.stdout.upper()
    # The release was not taken, and the environment in use is the one from before.
    assert bench.version() == "v0.1.0" and environments(bench) == before
    assert bench.sh(CHECK).returncode == 0 and bench.sh("jarvis --version").stdout == "v0.1.0\n"


def test_new_packages_get_a_new_environment_and_the_one_before_is_kept_for_the_way_back(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    first = environments(bench)
    bench.release("v0.2.0", files={"brain/requirements.txt": LOCK + "# one more line\n"})
    done = bench.sh("update")
    assert done.returncode == 0 and "building Jarvis's Python environment" in done.stdout, done.stdout
    second = [name for name in environments(bench) if name not in first]
    assert len(second) == 1 and len(environments(bench)) == 2
    assert bench.sh("readlink /opt/jarvis-venv/current").stdout.strip() == second[0]
    bench.release("v0.3.0", files={"brain/requirements.txt": LOCK + "# another line\n"})
    assert bench.sh("update").returncode == 0
    assert len(environments(bench)) == 2 and first[0] not in environments(bench) and second[0] in environments(bench)


def test_the_check_notices_what_is_wrong_with_jarvis_itself(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    for break_it, mend_it, words in (
        ("mv /opt/jarvis-venv/current /opt/jarvis-venv/aside", "mv /opt/jarvis-venv/aside /opt/jarvis-venv/current",
         "Jarvis's Python environment is not the one this release names, or its packages do not load"),
        ("mv /opt/jarvis-venv/current/lib /opt/jarvis-venv/current/lib-aside", "mv /opt/jarvis-venv/current/lib-aside /opt/jarvis-venv/current/lib",
         "Jarvis's Python environment is not the one this release names, or its packages do not load"),
        ("mv /opt/jarvis/src/jarvis/cli.py /root/cli-aside", "mv /root/cli-aside /opt/jarvis/src/jarvis/cli.py",
         "this release's own code does not load in Jarvis's Python environment"),
        ("chown jarvis /var/lib/jarvis", "chown root /var/lib/jarvis", "/var/lib/jarvis must belong to root, group jarvis, mode 0750"),
        ("chown root /var/lib/jarvis/audit/audit.jsonl", "chown jarvis /var/lib/jarvis/audit/audit.jsonl",
         "the audit log /var/lib/jarvis/audit/audit.jsonl cannot be written by the user jarvis"),
        ("chown jarvis /opt/jarvis-venv/current/pyvenv.cfg", "chown root /opt/jarvis-venv/current/pyvenv.cfg",
         "something under /opt/jarvis-venv does not belong to root"),
        ("echo '# changed' >> /usr/bin/jarvis", "cp /opt/jarvis/bin/jarvis /usr/bin/jarvis", "/usr/bin/jarvis is not the one of this release"),
        ("chmod 755 /var/lib/jarvis/audit", "chmod 700 /var/lib/jarvis/audit", "/var/lib/jarvis/audit must be a folder of the user jarvis, mode 0700"),
    ):
        assert bench.sh(break_it).returncode == 0, break_it
        looked = bench.sh(CHECK)
        assert looked.returncode == 1 and words in looked.stdout, (break_it, looked.stdout)
        assert bench.sh(mend_it).returncode == 0, mend_it
        assert bench.sh(CHECK).returncode == 0, mend_it
    for flag, words in (("chat-fails", "qwen3:8b did not answer"), ("model-says-nothing", "qwen3:8b answered with nothing")):
        (bench.state / flag).touch()
        looked = bench.sh(CHECK)
        assert looked.returncode == 1 and words in looked.stdout, (flag, looked.stdout)
        (bench.state / flag).unlink()
    # Mended by the repair, whatever it was.
    bench.sh("rm -rf /opt/jarvis-venv /usr/bin/jarvis /var/lib/jarvis/audit")
    assert bench.sh(CHECK).returncode == 1
    mended = bench.sh("update --repair")
    assert mended.returncode == 0 and "Jarvis v0.1.0: repaired" in mended.stdout, mended.stdout


def test_without_a_model_jarvis_is_installed_and_says_what_it_lacks(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(model="none"))
    assert done.returncode == 0, done.stdout
    assert "no local model is set (JARVIS_LOCAL_MODEL=none in /etc/jarvis/site.env)" in done.stdout
    asked = bench.sh("jarvis chat Hello")
    assert asked.returncode == 1 and "so there is nothing to answer with" in asked.stdout
    assert "jarvis-asks" not in state(bench, "calls")


def test_packages_that_cannot_be_fetched_stop_the_install_with_the_reason(bench):
    bench.release("v0.1.0")
    failed = bench.install("brain", PIP_FIND_LINKS=bench.tmp / "empty")
    assert failed.returncode == 1 and "Jarvis's Python packages could not be installed" in failed.stdout
    assert bench.sh("ls /opt/jarvis-venv").stdout == ""
    done = bench.install("brain")
    assert done.returncode == 0, done.stdout
