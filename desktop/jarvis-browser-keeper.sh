#!/bin/bash
# Keeps Jarvis's browser open on the desktop, as a user of its own (uid 1001), not as the desktop's user.
# Runs on the desktop's container, outside Docker, as jarvis-browser.service. The desktop's firewall keeps that
# user off every private address (the brain, the lab, the desktop's own ports) and lets it reach the internet
# and the name servers; so whatever page the browser shows, and whatever Jarvis does in it through the door,
# stays on the internet. Its profile is its own: nothing the desktop's user signed in to is in it.
# The desktop's session script puts the window in its place; this script only starts it when it is not running.
set -u
NAME="${JARVIS_DESKTOP_NAME:-jarvis-desktop}"
BROWSER_UID=1001
PAUSE="${JARVIS_KEEPER_PAUSE:-3}"
URL="file:///opt/jarvis-desktop/home.html"
starts=()

in_desktop() { docker exec "$NAME" "$@" </dev/null; }

while true; do
    sleep "$PAUSE"
    [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | cut -d'|' -f1)" = "true" ] || continue
    if in_desktop pgrep -u "$BROWSER_UID" -f -- "--class=jarvis-browser" >/dev/null 2>&1; then
        continue
    fi
    in_desktop test -S /tmp/.X11-unix/X1 || continue   # the desktop's screen is not up yet
    # Five starts within two minutes and it still does not stay: say so and wait a while.
    now=$SECONDS
    recent=()
    for at in "${starts[@]}"; do
        [ $((now - at)) -ge 120 ] || recent+=("$at")
    done
    starts=("${recent[@]}")
    if [ "${#starts[@]}" -ge 5 ]; then
        echo "jarvis-browser-keeper: Jarvis's browser was started five times in two minutes and does not stay open; trying again in ten minutes" >&2
        starts=()
        sleep 600
        continue
    fi
    # The desktop is made anew by every update, and the user with it. The number must be ours alone: an account
    # of the image that has it (or belongs to more groups) is not used.
    if ! in_desktop sh -ec "getent group $BROWSER_UID >/dev/null || groupadd -g $BROWSER_UID jarvis-web
            getent passwd $BROWSER_UID >/dev/null || useradd -M -u $BROWSER_UID -g $BROWSER_UID -d /jarvis-browser -s /bin/false jarvis-web
            [ \"\$(getent passwd $BROWSER_UID | cut -d: -f1)\" = jarvis-web ]
            [ \"\$(getent group $BROWSER_UID | cut -d: -f1)\" = jarvis-web ]
            [ \"\$(id -G jarvis-web)\" = $BROWSER_UID ]
            rm -f /jarvis-browser/profile/Singleton*"; then
        echo "jarvis-browser-keeper: the user $BROWSER_UID could not be made in the desktop, or the image has it for another account; Jarvis's browser is not started" >&2
        sleep 60
        continue
    fi
    # That user may show windows on the desktop's screen, and no other user is let in by this.
    docker exec -u abc -e DISPLAY=:1 "$NAME" xhost +SI:localuser:jarvis-web </dev/null >/dev/null 2>&1 \
        || echo "jarvis-browser-keeper: the desktop's screen could not be opened to Jarvis's browser's user (xhost); if the browser does not show, that is why" >&2
    starts+=("$now")
    docker exec -d -u "$BROWSER_UID:$BROWSER_UID" -w /jarvis-browser -e HOME=/jarvis-browser -e DISPLAY=:1 "$NAME" \
        /usr/bin/chromium --user-data-dir=/jarvis-browser/profile --class=jarvis-browser --no-default-browser-check \
        --remote-debugging-port=9222 "$URL" </dev/null \
        || echo "jarvis-browser-keeper: Jarvis's browser could not be started" >&2
done
