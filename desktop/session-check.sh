#!/bin/bash
# Says whether the desktop's own session stays up. Run inside the desktop, as its user.
#
#   session-check.sh            exit 0: it does. 1: it does not (the reasons are printed). 2: could not tell.
#
# A session that cannot start one of its programs properly ends within seconds, and the desktop image
# then starts a new one beside what the old one left behind, again and again, until the display has no
# room for another program. From outside that looks like a working desktop whose bar is missing. So this
# looks for two things: the session program that runs now has run for a while (or is given the time to,
# and is still the same one afterwards), and the desktop's bar runs. A session that was ended once and
# started again, by logging out say, passes: what counts is that the one running now stays.
set -u

STEADY="${JARVIS_SESSION_STEADY:-30}"   # seconds a session must have run; one that does not stay ends sooner
APPEAR="${JARVIS_SESSION_APPEAR:-30}"   # seconds a desktop that just came up is given to start its session

session() { pgrep -o -x xfce4-session 2>/dev/null; }
age_of() { ps -o etimes= -p "$1" 2>/dev/null | tr -d ' '; }

pid="$(session)"
waited=0
while [ -z "$pid" ] && [ "$waited" -lt "$APPEAR" ]; do
    sleep 2
    waited=$((waited + 2))
    pid="$(session)"
done
if [ -z "$pid" ]; then
    echo "the desktop's session program (xfce4-session) is not running"
    exit 1
fi

age="$(age_of "$pid")"
[[ "$age" =~ ^[0-9]+$ ]] || { echo "session check: how long the session has run could not be read." >&2; exit 2; }
result=0
if [ "$age" -lt "$STEADY" ]; then
    sleep "$((STEADY - age))"
    if [ "$(ps -o comm= -p "$pid" 2>/dev/null)" != "xfce4-session" ]; then
        started="$(pgrep -c -f '^dbus-launch --exit-with-session ' 2>/dev/null)"
        echo "the desktop's session ended within $STEADY seconds of its start (sessions started since the desktop came up: ${started:-unknown}): it does not stay up"
        result=1
    fi
fi
if ! pgrep -x xfce4-panel >/dev/null 2>&1; then
    echo "the desktop's bar (xfce4-panel) is not running"
    result=1
fi
exit "$result"
