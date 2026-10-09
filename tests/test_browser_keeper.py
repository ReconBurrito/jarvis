"""desktop/jarvis-browser-keeper.sh, against a stand-in for docker: it starts Jarvis's browser as its own user
when it is not running, makes that user in the desktop first, and stops trying for a while when the browser
will not stay."""
import os
import subprocess
import time

from conftest import REPO

KEEPER = REPO / "desktop" / "jarvis-browser-keeper.sh"
DOCKER = """#!/bin/bash
echo "$*" >> "$LOG"
case "$1" in
    inspect) echo "true|image|files" ;;
    exec)
        shift
        if [ "$1" = "-d" ]; then [ -f "$STAYS" ] && : > "$RUNNING"; exit 0; fi
        USER_=root
        while [ "$1" != "jarvis-desktop" ]; do [ "$1" != "-u" ] || USER_="$2"; shift; done
        shift
        case "$1" in
            pgrep) [ -f "$RUNNING" ] ;;
            test) exit 0 ;;
            sh) printf '%s' "$3" > "$SCRIPT"; [ ! -f "$TAKEN" ] ;;
            xhost) echo "xhost as $USER_" >> "$LOG" ;;
            *) exit 64 ;;
        esac
        ;;
esac
"""


def keep(tmp_path, seconds, stays, taken=False):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(DOCKER)
    (bin_ / "docker").chmod(0o755)
    log = tmp_path / "log"
    if stays:
        (tmp_path / "stays").write_text("")
    if taken:
        (tmp_path / "taken").write_text("")
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "LOG": str(log), "STAYS": str(tmp_path / "stays"),
           "RUNNING": str(tmp_path / "running"), "JARVIS_KEEPER_PAUSE": "0.1",
           "SCRIPT": str(tmp_path / "script"), "TAKEN": str(tmp_path / "taken")}
    keeper = subprocess.Popen(["bash", str(KEEPER)], env=env, stderr=subprocess.PIPE, text=True, start_new_session=True)
    time.sleep(seconds)
    os.killpg(keeper.pid, 9)   # its sleep too, which holds the pipe
    _, said = keeper.communicate(timeout=10)
    return log.read_text().splitlines(), said


def starts(lines):
    return [line for line in lines if line.startswith("exec -d ")]


def test_the_browser_is_started_once_as_its_own_user_and_left_alone_while_it_runs(tmp_path):
    lines, said = keep(tmp_path, 1.5, stays=True)
    assert starts(lines) == ["exec -d -u 1001:1001 -w /jarvis-browser -e HOME=/jarvis-browser -e DISPLAY=:1 jarvis-desktop "
                             "/usr/bin/chromium --user-data-dir=/jarvis-browser/profile --class=jarvis-browser "
                             "--no-default-browser-check --remote-debugging-port=9222 file:///opt/jarvis-desktop/home.html"]
    text = "\n".join(lines)
    made = text.index("exec jarvis-desktop sh -ec")
    assert text.count("exec jarvis-desktop sh -ec") == 1 and "useradd -M -u 1001 -g 1001" in text[made:]
    assert "rm -f /jarvis-browser/profile/Singleton*" in text[made:] and made < text.index("exec -d ") and said == ""


def test_a_browser_that_will_not_stay_is_tried_five_times_and_then_left_for_a_while(tmp_path):
    lines, said = keep(tmp_path, 2.5, stays=False)
    assert len(starts(lines)) == 5, lines
    assert "was started five times in two minutes and does not stay open" in said


def test_the_user_is_made_only_as_its_own_and_may_use_the_screen(tmp_path):
    lines, said = keep(tmp_path, 1.5, stays=True)
    script = (tmp_path / "script").read_text()
    assert subprocess.run(["sh", "-n", "-c", script]).returncode == 0
    for line in ('[ "$(getent passwd 1001 | cut -d: -f1)" = jarvis-web ]', '[ "$(id -G jarvis-web)" = 1001 ]'):
        assert line in script, script
    assert "xhost as abc" in lines and lines.index("xhost as abc") < lines.index(starts(lines)[0])


def test_a_number_the_image_gives_another_account_is_not_used(tmp_path):
    lines, said = keep(tmp_path, 1.0, stays=True, taken=True)
    assert starts(lines) == [] and "the image has it for another account" in said
