"""desktop/session-check.sh and desktop/bwrap, with stand-ins for the programs they ask.

What these two are for was found on a real desktop: its programs could not load a single picture, its
session ended every half minute and was started again beside the leftovers. The stand-in for bwrap was
also tried against the real picture loader of Ubuntu 26.04 (glycin 2.1.5): with a bwrap that fails the way
it does in the desktop, loading stops with "Loader process exited early"; with ours, pictures load.
"""
import os
import subprocess

from conftest import REPO

FAKES = {
    # ps: the age of a process, or its name. The session "ends" once the check has slept, when the test says so.
    "ps": """#!/bin/sh
case "$1 $2 $3" in
    "-o etimes= -p") [ -n "$FAKE_AGE" ] || exit 1; echo "  $FAKE_AGE" ;;
    "-o comm= -p")
        if [ "$FAKE_ENDS" = "1" ] && [ -s "$FAKE_SLEPT" ]; then exit 1; fi
        echo xfce4-session ;;
    *) exit 64 ;;
esac
""",
    "pgrep": """#!/bin/sh
case "$*" in
    "-o -x xfce4-session")
        if [ "$FAKE_SESSION" = "late" ] && [ ! -s "$FAKE_SLEPT" ]; then exit 1; fi
        [ "$FAKE_SESSION" != "0" ] || exit 1
        echo 4242 ;;
    "-c -f ^dbus-launch --exit-with-session ") echo 4 ;;
    "-x xfce4-panel") [ "$FAKE_PANEL" = "1" ] ;;
    *) exit 64 ;;
esac
""",
    "sleep": '#!/bin/sh\necho "$1" >> "$FAKE_SLEPT"\n',
}


def check(tmp_path, age="600", session="1", panel="1", ends="0", **settings):
    fakes = tmp_path / "bin"
    fakes.mkdir(exist_ok=True)
    for name, text in FAKES.items():
        (fakes / name).write_text(text)
        (fakes / name).chmod(0o755)
    slept = tmp_path / "slept"
    slept.unlink(missing_ok=True)
    env = {"PATH": f"{fakes}:/usr/bin:/bin", "FAKE_AGE": age, "FAKE_SESSION": session, "FAKE_PANEL": panel,
           "FAKE_ENDS": ends, "FAKE_SLEPT": str(slept), **settings}
    done = subprocess.run([str(REPO / "desktop" / "session-check.sh")], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return done.returncode, done.stdout, slept.read_text().split() if slept.exists() else []


def test_a_session_that_has_run_for_a_while_passes_at_once(tmp_path):
    assert check(tmp_path) == (0, "", [])
    assert check(tmp_path, age="30") == (0, "", [])


def test_a_young_session_is_given_the_time_and_passes_when_it_is_still_there(tmp_path):
    assert check(tmp_path, age="8") == (0, "", ["22"])                  # 30 seconds by default
    assert check(tmp_path, age="8", JARVIS_SESSION_STEADY="12") == (0, "", ["4"])


def test_a_session_that_ends_meanwhile_fails(tmp_path):
    code, said, slept = check(tmp_path, age="8", ends="1", panel="0")
    assert code == 1 and slept == ["22"]
    assert "the desktop's session ended within 30 seconds of its start (sessions started since the desktop came up: 4): it does not stay up" in said
    assert "the desktop's bar (xfce4-panel) is not running" in said


def test_a_desktop_that_just_came_up_is_given_time_to_start_its_session(tmp_path):
    assert check(tmp_path, session="late") == (0, "", ["2"])
    code, said, slept = check(tmp_path, session="0", JARVIS_SESSION_APPEAR="6")
    assert code == 1 and said == "the desktop's session program (xfce4-session) is not running\n" and slept == ["2", "2", "2"]


def test_a_missing_bar_fails(tmp_path):
    assert check(tmp_path, panel="0") == (1, "the desktop's bar (xfce4-panel) is not running\n", [])


def test_no_answer_about_the_desktops_age_is_not_a_pass(tmp_path):
    code, said, _ = check(tmp_path, age="")
    assert code == 2 and "could not be read" in said


def test_the_stand_in_for_bwrap_says_no_in_the_words_the_picture_loader_knows():
    """glycin (2.0.1 and later) loads pictures without a sandbox when bwrap's complaint holds one of a few
    sentences; this is one of them, and the stand-in must fail whatever it is asked."""
    for arguments in ([], ["--version"], ["--unshare-all", "--ro-bind", "/usr", "/usr", "/usr/bin/true"]):
        done = subprocess.run([str(REPO / "desktop" / "bwrap"), *arguments], text=True, capture_output=True)
        assert done.returncode == 1 and done.stdout == ""
        assert "No permissions to create a new namespace" in done.stderr
    assert os.access(REPO / "desktop" / "bwrap", os.X_OK)
