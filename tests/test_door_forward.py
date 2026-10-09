"""desktop/jarvis-door-forward.sh with the real ss and socat: a connection through the door goes on only to
Jarvis's browser's user listening on loopback, never to another program that took the port."""
import os
import shutil
import socket
import subprocess
import sys

import pytest

from conftest import REPO

FORWARD = REPO / "desktop" / "jarvis-door-forward.sh"
ECHO = ("import socket, sys; s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
        "s.bind((sys.argv[1], int(sys.argv[2]))); s.listen(1); print('ready', flush=True); c, _ = s.accept(); "
        "c.sendall(b'answer from ' + sys.argv[3].encode()); c.close()")
pytestmark = pytest.mark.skipif(os.geteuid() != 0 or not all(map(shutil.which, ("ss", "socat", "setpriv"))),
                                reason="needs root, ss, socat and setpriv")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def through_door(uid, where="127.0.0.1"):
    port = free_port()
    listener = subprocess.Popen(["setpriv", "--reuid", str(uid), "--regid", str(uid), "--clear-groups", sys.executable, "-c",
                                 ECHO, where, str(port), f"uid {uid}"], stdout=subprocess.PIPE, text=True)
    try:
        assert listener.stdout.readline().strip() == "ready"
        done = subprocess.run(["bash", str(FORWARD)], input="", capture_output=True, text=True, timeout=20,
                              env=dict(os.environ, JARVIS_DEBUG_PORT=str(port), JARVIS_BROWSER_UID="1001"))
        return done
    finally:
        listener.kill()
        listener.wait()


def test_the_door_goes_on_to_the_browsers_user_and_to_nobody_else():
    passed = through_door(1001)
    assert passed.returncode == 0 and passed.stdout == "answer from uid 1001", passed
    for uid, where in ((1000, "127.0.0.1"), (0, "127.0.0.1"), (1001, "0.0.0.0")):
        refused = through_door(uid, where)
        assert refused.returncode == 1 and refused.stdout == "", (uid, where, refused)
        assert "nothing of Jarvis's browser listens" in refused.stderr
