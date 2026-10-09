#!/bin/bash
# One connection through the brain's door (jarvis-door.service runs this for each, its standard input and
# output being the brain's TLS connection): on to the DevTools port of Jarvis's browser, but only when what
# listens there is that browser's user. While the browser is down any program could take the port and pose
# as the browser to the brain.
set -u
PORT="${JARVIS_DEBUG_PORT:-9222}"
BROWSER_UID="${JARVIS_BROWSER_UID:-1001}"
listeners="$(ss -Hltne "sport = :$PORT" 2>/dev/null)"
if [ "$(printf '%s\n' "$listeners" | grep -c .)" != "1" ] \
    || ! [[ "$listeners" =~ [[:space:]]127\.0\.0\.1:${PORT}[[:space:]] ]] \
    || ! [[ "$listeners" =~ [[:space:]]uid:${BROWSER_UID}[[:space:]] ]]; then
    echo "jarvis-door: nothing of Jarvis's browser listens on 127.0.0.1:$PORT; the connection is closed" >&2
    exit 1
fi
exec socat STDIO "TCP:127.0.0.1:$PORT"
