"""desktop/jarvis-session.sh on a real window manager: XFCE's xfwm4 (and its panel, when installed) on a
virtual screen, with a real Chromium. The screen is resized the way the desktop image resizes it.

Needs Xvfb, xfwm4, dbus-run-session, xdotool, xprop, xwininfo, xrandr and a Chromium (JARVIS_TEST_BROWSER,
or the one Playwright installs). Skipped where one is missing. The browser runs without its sandbox here
only because this test runs as root; the sandbox has a test of its own.
"""
import os
import re
import shutil
import signal
import subprocess
import time

import pytest

from conftest import REPO

BROWSER = os.environ.get("JARVIS_TEST_BROWSER") or next(
    (p for p in ("/opt/pw-browsers/chromium", shutil.which("chromium") or "", shutil.which("chromium-browser") or "")
     if p and os.access(p, os.X_OK)), "")
NEEDED = ("Xvfb", "xfwm4", "dbus-run-session", "xdotool", "xprop", "xwininfo", "xrandr")
PANEL = 400


class Desktop:
    made = 0

    def __init__(self, tmp):
        self.tmp = tmp
        Desktop.made += 1
        self.number = 80 + (os.getpid() * 7 + Desktop.made) % 900
        self.display = f":{self.number}"
        self.env = dict(os.environ, DISPLAY=self.display, HOME=str(tmp / "home"), XDG_RUNTIME_DIR=str(tmp / "run"),
                        JARVIS_CHROMIUM=str(tmp / "browser"), JARVIS_PANEL_URL=f"file://{REPO}/desktop/panel.html",
                        JARVIS_BROWSER_URL=f"file://{REPO}/desktop/home.html",
                        JARVIS_PANEL_WIDTH=str(PANEL), JARVIS_SESSION_GRACE="10", JARVIS_SESSION_PAUSE="1")
        self.children = []
        for folder in ("home", "run"):
            (tmp / folder).mkdir(mode=0o700)
        launcher = tmp / "browser"
        launcher.write_text(f'#!/bin/sh\nexec "{BROWSER}" --no-sandbox --no-first-run --password-store=basic --disable-gpu '
                            '--disable-background-networking --disable-component-update --disable-sync "$@"\n')
        launcher.chmod(0o755)

    def start(self, *command):
        log = open(self.tmp / "log", "ab")
        child = subprocess.Popen(command, env=self.env, start_new_session=True, stdout=log, stderr=log)
        self.children.append(child)
        return child

    def out(self, *command):
        return subprocess.run(command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout

    def up(self):
        self.start("Xvfb", self.display, "-screen", "0", "2560x1440x24", "+extension", "RANDR")
        self.wait(lambda: "dimensions" in self.out("xdpyinfo") or self.out("xdotool", "getdisplaygeometry"), "the screen")
        # The window manager is started again should it give up at its first try (it does, now and then,
        # when the settings service of a new session bus is not ready), and XFCE's bar once it is there.
        panel = "(sleep 3; xfce4-panel --disable-wm-check) & " if shutil.which("xfce4-panel") else ""
        self.start("dbus-run-session", "--", "sh", "-c",
                   f"(while :; do xfwm4 --compositor=off; sleep 1; done) & {panel}sleep 100000")
        self.wait(lambda: "window id" in self.out("xprop", "-root", "_NET_SUPPORTING_WM_CHECK"), "the window manager")
        self.resize(1600, 900)
        if panel:  # XFCE's bar takes a strip of the screen; give it a moment to claim it
            try:
                self.wait(lambda: (self.work_area() or (0, 0))[1] > 0, "XFCE's bar", seconds=15)
            except AssertionError:
                pass

    def resize(self, width, height):
        name = f"{width}x{height}"
        self.out("xrandr", "--newmode", name, "100.00", str(width), str(width + 48), str(width + 80), str(width + 160),
                 str(height), str(height + 3), str(height + 8), str(height + 30), "-hsync", "+vsync")
        self.out("xrandr", "--addmode", "screen", name)
        self.out("xrandr", "--output", "screen", "--mode", name)
        self.wait(lambda: self.out("xdotool", "getdisplaygeometry").split() == [str(width), str(height)], f"a {name} screen")

    def wait(self, test, what, seconds=40):
        end = time.time() + seconds
        while time.time() < end:
            value = test()
            if value:
                return value
            time.sleep(0.5)
        log = (self.tmp / "log").read_text(errors="replace")[-1500:] if (self.tmp / "log").exists() else ""
        raise AssertionError(f"waited {seconds} s for {what}; windows now: {self.places()}, work area {self.work_area()}\n{log}")

    def said(self):
        """What the session script itself reported."""
        log = (self.tmp / "log").read_text(errors="replace") if (self.tmp / "log").exists() else ""
        return "\n".join(line for line in log.splitlines() if line.startswith("jarvis-session:"))

    def work_area(self):
        numbers = re.findall(r"\d+", self.out("xprop", "-root", "_NET_WORKAREA"))[:4]
        return tuple(map(int, numbers)) if len(numbers) == 4 else None

    def windows(self, name):
        found = []
        for window in self.out("xdotool", "search", "--class", name).split():
            if re.search(r"window state: (Normal|Iconic)", self.out("xprop", "-id", window, "WM_STATE")):
                found.append(window)
        return found

    def minimized(self, window):
        return "_NET_WM_STATE_HIDDEN" in self.out("xprop", "-id", window, "_NET_WM_STATE")

    def frame(self, window):
        return self.out("xprop", "-id", window, "_NET_FRAME_EXTENTS").split("=")[-1].strip()

    def owner(self, window):
        found = re.search(r"= (\d+)", self.out("xprop", "-id", window, "_NET_WM_PID"))
        return found.group(1) if found else None

    def place(self, name):
        """Where the window is, frame included: (x, y, width, height), or None."""
        windows = self.windows(name)
        if not windows:
            return None
        info = self.out("xwininfo", "-id", windows[0])
        try:
            x, y, width, height = (int(re.search(pattern, info).group(1)) for pattern in (
                r"Absolute upper-left X:\s+(-?\d+)", r"Absolute upper-left Y:\s+(-?\d+)", r"Width:\s+(\d+)", r"Height:\s+(\d+)"))
        except AttributeError:
            return None
        frame = re.findall(r"\d+", self.out("xprop", "-id", windows[0], "_NET_FRAME_EXTENTS").split("=")[-1])
        left, right, top, bottom = map(int, frame) if len(frame) == 4 else (0, 0, 0, 0)
        return (x - left, y - top, width + left + right, height + top + bottom)

    def places(self):
        return {"panel": self.place("jarvis-panel"), "browser": self.place("jarvis-browser")}

    def docked(self):
        area = self.work_area()
        if not area:
            return False
        x, y, width, height = area
        return self.places() == {"panel": (x + width - PANEL, y, PANEL, height), "browser": (x, y, width - PANEL, height)}

    def at_rest(self):
        """Docked, and still docked a few rounds of the script later: it has finished with both windows."""
        if not self.docked():
            return False
        time.sleep(3)
        return self.docked()

    def down(self):
        # Everything started on this desktop carries its home folder in its environment, whatever became
        # of its parent: browsers, the window manager, the session bus and what that bus started.
        mark = f"HOME={self.tmp}/home".encode()
        for _ in range(3):
            for entry in os.listdir("/proc"):
                if not entry.isdigit() or int(entry) == os.getpid():
                    continue
                try:
                    with open(f"/proc/{entry}/environ", "rb") as handle:
                        if mark in handle.read().split(b"\0"):
                            os.kill(int(entry), signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.3)
        for child in self.children:
            child.wait(timeout=10)
        for leftover in (f"/tmp/.X{self.number}-lock", f"/tmp/.X11-unix/X{self.number}"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


@pytest.fixture
def desktop(tmp_path):
    missing = [tool for tool in NEEDED if not shutil.which(tool)]
    if missing or not BROWSER:
        pytest.skip("needs " + ", ".join(missing or ["a Chromium"]))
    desk = Desktop(tmp_path)
    try:
        desk.up()
        yield desk
    finally:
        desk.down()


def test_the_panel_docks_right_and_the_browser_fills_the_rest(desktop):
    desktop.start(str(REPO / "desktop" / "jarvis-session.sh"))
    desktop.wait(desktop.at_rest, "both windows in place")

    # The viewer is resized: smaller, then larger.
    for size in ((1280, 720), (1920, 1080)):
        desktop.resize(*size)
        desktop.wait(desktop.at_rest, f"both windows in place on {size}")

    # The panel is part of the desktop: no title bar, no border. The browser keeps what it has.
    panel = desktop.windows("jarvis-panel")[0]
    browser = desktop.windows("jarvis-browser")[0]
    assert desktop.frame(panel) == "0, 0, 0, 0"

    # A minimized window comes back by itself, as the same window and to the same place: this desktop
    # need not have a task bar to fetch it from.
    for window in (browser, panel):
        desktop.out("xdotool", "windowminimize", window)
        desktop.wait(lambda: desktop.minimized(window), "the window to be minimized", seconds=10)
        desktop.wait(lambda: not desktop.minimized(window), "the minimized window to come back", seconds=15)
        desktop.wait(desktop.at_rest, "both windows in place after one was minimized")
    assert desktop.windows("jarvis-panel") == [panel] and desktop.windows("jarvis-browser") == [browser]

    # A window moved by hand stays where it was put.
    desktop.out("xdotool", "windowsize", "--sync", browser, "900", "600")
    desktop.out("xdotool", "windowmove", "--sync", browser, "150", "150")
    time.sleep(6)
    assert desktop.place("jarvis-browser")[2:] == (900, 600) and not desktop.docked(), desktop.said()

    # A panel that is gone comes back, and both are put in place again. (The new window may get the old
    # window's number, so it is told apart by the process that owns it.)
    owner = desktop.owner(panel)
    assert owner
    desktop.out("xdotool", "windowkill", panel)
    desktop.wait(lambda: [o for o in map(desktop.owner, desktop.windows("jarvis-panel")) if o and o != owner], "a new panel window")
    desktop.wait(desktop.at_rest, "both windows in place after the panel came back")
    assert len(desktop.windows("jarvis-panel")) == 1 and len(desktop.windows("jarvis-browser")) == 1
    assert desktop.frame(desktop.windows("jarvis-panel")[0]) == "0, 0, 0, 0"   # the new panel has no title bar either


def test_a_panel_opened_again_loses_its_title_bar_every_time(desktop):
    """The new panel window often gets the number the old one had; it is a new window all the same."""
    desktop.start(str(REPO / "desktop" / "jarvis-session.sh"))
    desktop.wait(desktop.at_rest, "both windows in place")
    for round_ in range(3):
        panel = desktop.windows("jarvis-panel")[0]
        owner = desktop.owner(panel)
        desktop.out("xdotool", "windowkill", panel)
        desktop.wait(lambda: [o for o in map(desktop.owner, desktop.windows("jarvis-panel")) if o and o != owner],
                     f"a new panel window (round {round_})")
        desktop.wait(desktop.at_rest, "both windows in place after the panel came back")
        assert desktop.frame(desktop.windows("jarvis-panel")[0]) == "0, 0, 0, 0", round_


def test_a_browser_that_fills_the_screen_is_left_alone_until_it_no_longer_does(desktop):
    desktop.start(str(REPO / "desktop" / "jarvis-session.sh"))
    desktop.wait(desktop.at_rest, "both windows in place")
    browser = desktop.windows("jarvis-browser")[0]
    desktop.out("xdotool", "windowactivate", "--sync", browser)
    desktop.out("xdotool", "key", "F11")
    desktop.wait(lambda: "_NET_WM_STATE_FULLSCREEN" in desktop.out("xprop", "-id", browser, "_NET_WM_STATE"), "a full screen browser")
    desktop.resize(1280, 720)        # the viewer is resized meanwhile
    time.sleep(6)
    assert desktop.said() == ""      # nothing was pushed around, nothing given up on
    desktop.out("xdotool", "key", "F11")
    desktop.wait(desktop.at_rest, "both windows in place on the new size after full screen ended")


def test_only_one_session_script_runs_per_desktop(desktop):
    first = desktop.start(str(REPO / "desktop" / "jarvis-session.sh"))
    desktop.wait(desktop.at_rest, "both windows in place")
    second = desktop.start(str(REPO / "desktop" / "jarvis-session.sh"))
    assert second.wait(timeout=10) == 0 and first.poll() is None
    time.sleep(4)
    assert len(desktop.windows("jarvis-panel")) == 1 and len(desktop.windows("jarvis-browser")) == 1


def test_windows_whose_moves_mean_the_frame_are_placed_exactly_too(desktop):
    """Chromium asks for moves that mean the inside of its window. Most programs' moves mean the frame;
    the script must get those right as well, should a browser ever behave that way. A clock stands in."""
    if not shutil.which("xclock"):
        pytest.skip("needs xclock")
    launcher = desktop.tmp / "browser"
    launcher.write_text("""#!/bin/sh
# Stands in for the browser: opens a plain window and gives it the class the script asked for.
for word in "$@"; do case "$word" in --class=*) class="${word#--class=}" ;; esac; done
xclock -name "clock-$class" &
for try in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    id="$(xdotool search --classname "clock-$class" | head -n 1)"
    [ -z "$id" ] || break
    sleep 0.25
done
xdotool set_window --class "$class" "$id"
wait
""")
    desktop.start(str(REPO / "desktop" / "jarvis-session.sh"))
    desktop.wait(desktop.at_rest, "both windows in place")
    browser = desktop.windows("jarvis-browser")[0]
    assert "window gravity" not in desktop.out("xprop", "-id", browser, "WM_NORMAL_HINTS") or \
        "Static" not in desktop.out("xprop", "-id", browser, "WM_NORMAL_HINTS")
    assert desktop.frame(browser) != "0, 0, 0, 0"                       # the stand-in browser has a frame
    assert desktop.frame(desktop.windows("jarvis-panel")[0]) == "0, 0, 0, 0"   # and the panel lost its own
    desktop.resize(1280, 720)
    desktop.wait(desktop.at_rest, "both windows in place on the smaller screen")


def test_a_second_browser_window_is_left_to_whoever_opened_it(desktop):
    """Ctrl+N, or a page that opens a window: the script keeps to the window it chose and does not push
    the two around each time one of them comes to the front."""
    desktop.start(str(REPO / "desktop" / "jarvis-session.sh"))
    desktop.wait(desktop.at_rest, "both windows in place")
    first = desktop.windows("jarvis-browser")
    assert len(first) == 1
    desktop.start(str(desktop.tmp / "browser"), f"--user-data-dir={desktop.tmp}/home/.config/jarvis/browser",
                  "--class=jarvis-browser", "--new-window", "about:blank")
    desktop.wait(lambda: len(desktop.windows("jarvis-browser")) == 2, "a second browser window")
    second = [w for w in desktop.windows("jarvis-browser") if w != first[0]][0]
    desktop.out("xdotool", "windowsize", "--sync", second, "700", "500")
    desktop.out("xdotool", "windowmove", "--sync", second, "200", "200")

    def rect(window):
        info = desktop.out("xwininfo", "-id", window)
        return tuple(int(re.search(pattern, info).group(1)) for pattern in (r"Width:\s+(\d+)", r"Height:\s+(\d+)"))

    docked = rect(first[0])
    for _ in range(3):
        for window in (first[0], second):
            desktop.out("xdotool", "windowactivate", "--sync", window)
            time.sleep(2.5)
            assert rect(second) == (700, 500) and rect(first[0]) == docked, desktop.said()
    assert desktop.said() == ""

    # Minimized, the second window comes back as well, and where it was.
    desktop.out("xdotool", "windowminimize", second)
    desktop.wait(lambda: desktop.minimized(second), "the second window to be minimized", seconds=10)
    desktop.wait(lambda: not desktop.minimized(second), "the second window to come back", seconds=15)
    assert rect(second) == (700, 500) and rect(first[0]) == docked


def test_a_browser_kept_from_outside_is_not_started_here_but_is_put_in_its_place(desktop):
    """On the installed desktop Jarvis's browser runs as a user of its own, started from outside the desktop
    (desktop/jarvis-browser-keeper.sh); the script only places its window."""
    kept = desktop.tmp / "browser-kept"
    kept.write_text("kept from outside\n")
    desktop.env["JARVIS_BROWSER_KEPT"] = str(kept)
    desktop.start(str(REPO / "desktop" / "jarvis-session.sh"))
    desktop.wait(lambda: len(desktop.windows("jarvis-panel")) == 1, "the panel")
    time.sleep(12)   # longer than the grace before a missing window is started
    assert desktop.windows("jarvis-browser") == [], "the script started no browser of its own"
    desktop.start(str(desktop.tmp / "browser"), f"--user-data-dir={desktop.tmp}/elsewhere", "--class=jarvis-browser",
                  f"file://{REPO}/desktop/home.html")
    desktop.wait(desktop.at_rest, "both windows in place")
