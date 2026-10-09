#!/bin/bash
# Jarvis's place on the desktop. Runs inside the desktop, as its user, for as long as the desktop session
# lasts (started by jarvis-session.desktop). It keeps two windows open and where they belong: Jarvis's
# panel docked on the right edge, and Jarvis's browser filling the rest. A window that was closed comes
# back, and so does any window of the two programs that was minimized (this desktop need not have a
# task bar to fetch it from); when the size of the desktop changes (another screen, a resized viewer),
# both are put in place again. Between those moments the windows are left alone, so the browser can be
# moved by hand. The panel has no title bar: it is part of the desktop, not a window to push around.
set -u

PANEL_WIDTH="${JARVIS_PANEL_WIDTH:-400}"
PANEL_URL="${JARVIS_PANEL_URL:-file:///opt/jarvis-desktop/panel.html}"
BROWSER_URL="${JARVIS_BROWSER_URL:-file:///opt/jarvis-desktop/home.html}"
CHROMIUM="${JARVIS_CHROMIUM:-/opt/jarvis-desktop/chromium}"
STATE="${JARVIS_SESSION_DIR:-$HOME/.config/jarvis}"
PAUSE="${JARVIS_SESSION_PAUSE:-2}"

mkdir -p "$STATE"
exec 9>"$STATE/session.lock"
flock -n 9 || exit 0  # one of these per desktop

# The space windows may use: the screen without the desktop's own bars. "x y width height".
work_area() {
    local area
    area="$(xprop -root _NET_WORKAREA 2>/dev/null | sed -n 's/^[^=]*= *\([0-9]*\), *\([0-9]*\), *\([0-9]*\), *\([0-9]*\).*/\1 \2 \3 \4/p')"
    if [ -z "$area" ]; then
        area="0 0 $(xdotool getdisplaygeometry 2>/dev/null)"
    fi
    printf '%s' "$area"
}

# Whether WINDOW is a main window of class CLASS that the window manager looks after, shown or minimized.
# A browser has helper windows of the same class that never appear (they carry no WM_STATE), and dialogs
# that belong to another window; neither counts.
main_window() {
    local facts
    facts="$(xprop -id "$1" WM_STATE WM_CLASS WM_TRANSIENT_FOR _NET_WM_WINDOW_TYPE 2>/dev/null)" || return 1
    grep -q 'window state: \(Normal\|Iconic\)' <<<"$facts" || return 1
    grep -q "^WM_CLASS.*\"$2\"" <<<"$facts" || return 1
    ! grep -q '^WM_TRANSIENT_FOR(WINDOW)' <<<"$facts" || return 1
    ! grep -q '^_NET_WM_WINDOW_TYPE.*_\(DIALOG\|UTILITY\|MENU\|SPLASH\|TOOLTIP\|NOTIFICATION\|POPUP_MENU\|DROPDOWN_MENU\)' <<<"$facts"
}

# The window of class CLASS to look after: the one already chosen (KEPT) for as long as it exists, so that
# a second window of the browser is left to the person who opened it; otherwise the first main window.
window_of() {  # window_of CLASS [KEPT]
    local id
    if [ -n "${2:-}" ] && main_window "$2" "$1"; then
        printf '%s' "$2"
        return 0
    fi
    for id in $(xdotool search --class "$1" 2>/dev/null); do
        if main_window "$id" "$1"; then
            printf '%s' "$id"
            return 0
        fi
    done
}

# Whether the window is minimized. (Asked this way, not by WM_STATE: window managers also call a window
# on another workspace "iconic", and that one must be left where it is.)
minimized() {
    xprop -id "$1" _NET_WM_STATE 2>/dev/null | grep -q '_NET_WM_STATE_HIDDEN'
}

# Whether the window fills the whole screen (F11, or a video shown full screen). It is left alone then.
fullscreen() {
    xprop -id "$1" _NET_WM_STATE 2>/dev/null | grep -q '_NET_WM_STATE_FULLSCREEN'
}

# Brings back every minimized main window of class CLASS, where it was.
restore() {
    local id
    for id in $(xdotool search --class "$1" 2>/dev/null); do
        if main_window "$id" "$1" && minimized "$id"; then
            xdotool windowmap "$id" 2>/dev/null
        fi
    done
}

# Asks the window manager to draw no title bar and no border around the window. (The property that says
# so has a type of its own, which xprop cannot write; a few lines against the X library can. Where
# that is not possible the window keeps its title bar and everything else works as before.)
bare() {
    command -v python3 >/dev/null 2>&1 || return 1
    # -I: only Python's own modules are loaded, none from the folder this script happens to run in.
    python3 -I - "$1" 2>/dev/null <<'PYTHON'
import ctypes
import sys

x = ctypes.CDLL("libX11.so.6")
x.XOpenDisplay.restype = ctypes.c_void_p
x.XInternAtom.restype = ctypes.c_ulong
x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                              ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
x.XFlush.argtypes = [ctypes.c_void_p]
x.XCloseDisplay.argtypes = [ctypes.c_void_p]
display = x.XOpenDisplay(None)
if not display:
    sys.exit(1)
hints = x.XInternAtom(display, b"_MOTIF_WM_HINTS", 0)
# flags: "decorations is set"; functions; decorations: none; input mode; status
value = (ctypes.c_long * 5)(2, 0, 0, 0, 0)
x.XChangeProperty(display, int(sys.argv[1]), hints, hints, 32, 0, ctypes.byref(value), 5)
x.XFlush(display)
x.XCloseDisplay(display)
PYTHON
}

# The window manager draws a frame around each window; its width counts against the space we have.
frame_of() {  # "left right top bottom"
    local frame
    frame="$(xprop -id "$1" _NET_FRAME_EXTENTS 2>/dev/null | sed -n 's/^[^=]*= *\([0-9]*\), *\([0-9]*\), *\([0-9]*\), *\([0-9]*\).*/\1 \2 \3 \4/p')"
    printf '%s' "${frame:-0 0 0 0}"
}

# Where a window really is: "x y width height" of what is inside the frame. (xdotool's own report of a
# window's position is off by the frame under window managers that put windows in frames.)
inside_of() {
    xwininfo -id "$1" 2>/dev/null | awk '
        /Absolute upper-left X:/ {x = $NF} /Absolute upper-left Y:/ {y = $NF}
        /^ *Width:/ {w = $NF} /^ *Height:/ {h = $NF}
        END {if (h != "") print x, y, w, h}'
}

# A window manager applies a move a moment after it was asked for. Waits until the window stays put.
settled() {  # prints where the window is once two looks in a row agree
    local last="" now="" _
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        now="$(inside_of "$1")"
        [ "$now" != "$last" ] || break
        last="$now"
        sleep 0.15
    done
    printf '%s' "$now"
}

put() {  # put WINDOW X Y WIDTH HEIGHT: the window, frame included, takes exactly that space; true when it does
    local window="$1" x="$2" y="$3" width="$4" height="$5" left right top bottom gotx goty gotw goth dx dy
    read -r left right top bottom <<<"$(frame_of "$window")"
    xdotool windowsize "$window" "$((width - left - right))" "$((height - top - bottom))" 2>/dev/null
    xdotool windowmove "$window" "$((x + left))" "$((y + top))" 2>/dev/null
    # Window managers differ in whether a move means the frame or what is inside it: look where the
    # window went, and when it is off by no more than a frame, move it once more by what is missing.
    # (Off by more than that, somebody else moved it meanwhile; the next round starts over.)
    read -r gotx goty gotw goth <<<"$(settled "$window")"
    [ -n "${goth:-}" ] || return 1
    dx=$((x + left - gotx))
    dy=$((y + top - goty))
    if [ "$dx" -ne 0 ] || [ "$dy" -ne 0 ]; then
        if [ "${dx#-}" -le 64 ] && [ "${dy#-}" -le 64 ]; then
            xdotool windowmove "$window" "$((x + left + dx))" "$((y + top + dy))" 2>/dev/null
            read -r gotx goty gotw goth <<<"$(settled "$window")"
        fi
    fi
    [ "$gotx $goty $gotw $goth" = "$((x + left)) $((y + top)) $((width - left - right)) $((height - top - bottom))" ] && return 0
    echo "jarvis-session: window $window is at $gotx $goty $gotw $goth, wanted $((x + left)) $((y + top)) $((width - left - right)) $((height - top - bottom))" >&2
    return 1
}

in_use() {  # whether a browser is running on the profile of that name
    pgrep -f -- "--user-data-dir=$STATE/$1( |\$)" >/dev/null 2>&1
}

# A browser that was killed leaves a lock in its profile, and the next one would wait on it.
unlock() {
    in_use "$1" || rm -f "$STATE/$1"/Singleton*
}

start_panel() {
    unlock panel
    "$CHROMIUM" --user-data-dir="$STATE/panel" --class=jarvis-panel --no-default-browser-check \
        --app="$PANEL_URL" >/dev/null 2>&1 9>&- &
}

start_browser() {
    unlock browser
    "$CHROMIUM" --user-data-dir="$STATE/browser" --class=jarvis-browser --no-default-browser-check \
        "$BROWSER_URL" >/dev/null 2>&1 9>&- &
}

arrange() {  # arrange PANEL BROWSER: true when every window that is there took its place
    local x y width height panel="$1" browser="$2" side fine=0
    read -r x y width height <<<"$(work_area)"
    [ -n "${height:-}" ] || return 1
    side="$PANEL_WIDTH"
    [ "$side" -le $((width / 2)) ] || side=$((width / 2))
    [ -z "$panel" ] || put "$panel" "$((x + width - side))" "$y" "$side" "$height" || fine=1
    [ -z "$browser" ] || put "$browser" "$x" "$y" "$((width - side))" "$height" || fine=1
    return "$fine"
}

# A window that is gone is opened again. A browser needs a while before its window shows, the first time
# most of all: nothing is started again within GRACE seconds, nor while a browser on that profile is still
# coming up (unless it has shown nothing for three times as long).
GRACE="${JARVIS_SESSION_GRACE:-20}"
due() {  # due NAME STARTED STARTS: whether the window of that profile should be opened now
    local since=$((SECONDS - $2))
    # Five starts in a row without a window ever showing: something is wrong that more starts will not mend.
    [ "$3" -lt 5 ] || return 1
    [ "$since" -ge "$GRACE" ] || return 1
    ! in_use "$1" || [ "$since" -ge $((GRACE * 3)) ]
}

# Behind the windows, while they open: the colour of the panel, where no desktop program paints over it.
! command -v xsetroot >/dev/null 2>&1 || xsetroot -solid '#04101a' 2>/dev/null || true

panel=""
browser=""
bares=0
panel_started=$((-GRACE * 3))
browser_started=$((-GRACE * 3))
panel_starts=0
browser_starts=0
seen=""
tries=0
while true; do
    panel="$(window_of jarvis-panel "$panel")"
    browser="$(window_of jarvis-browser "$browser")"
    [ -z "$panel" ] || panel_starts=0
    [ -z "$browser" ] || browser_starts=0
    # The panel's title bar is taken off whenever it has one: a panel that was opened again is a new
    # window, even when it got the old one's number. Three tries, should this desktop not allow it.
    if [ -z "$panel" ] || [ "$(frame_of "$panel")" = "0 0 0 0" ]; then
        bares=0
    elif [ "$bares" -lt 3 ]; then
        bare "$panel" || true
        bares=$((bares + 1))
        seen=""  # its frame is gone a moment later: put both in place with what is true then
        sleep 0.3
    fi
    restore jarvis-panel
    restore jarvis-browser
    # A window that fills the screen is not pushed back into its place; that waits until it no longer does.
    if { [ -n "$panel" ] && fullscreen "$panel"; } || { [ -n "$browser" ] && fullscreen "$browser"; }; then
        sleep "$PAUSE"
        continue
    fi
    if [ -z "$panel" ] && due panel "$panel_started" "$panel_starts"; then
        start_panel
        panel_started=$SECONDS
        panel_starts=$((panel_starts + 1))
        [ "$panel_starts" -lt 5 ] || echo "jarvis-session: the panel was started five times and no window of it showed; not trying again" >&2
    fi
    if [ -z "$browser" ] && due browser "$browser_started" "$browser_starts"; then
        start_browser
        browser_started=$SECONDS
        browser_starts=$((browser_starts + 1))
        [ "$browser_starts" -lt 5 ] || echo "jarvis-session: the browser was started five times and no window of it showed; not trying again" >&2
    fi
    now="$(work_area) $panel $browser"
    if [ "$now" != "$seen" ]; then
        # Something changed: the desktop's size, or a window appeared. Put both in place. A window that
        # did not take its place (it was still opening, or the screen is too small for it) is tried
        # twice more; after that it is left where it is, like a window moved by hand.
        tries=$((tries + 1))
        if arrange "$panel" "$browser" || [ "$tries" -ge 3 ]; then
            seen="$now"
            tries=0
        fi
    else
        tries=0
    fi
    sleep "$PAUSE"
done
