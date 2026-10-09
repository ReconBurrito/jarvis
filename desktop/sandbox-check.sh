#!/bin/bash
# Says whether Chromium's sandbox is really on, on this machine, by starting the browser for a moment and
# looking at one of its page processes. Run as the desktop's user, never as root.
#
#   sandbox-check.sh            exit 0: both layers are on. 1: at least one is off. 2: could not tell.
#
# Two layers make the sandbox, and both must be there:
#   1. its own namespaces: a page process lives in a process-number space of its own, so it cannot see
#      or signal anything else. In /proc/PID/status that shows as more numbers on the NSpid line than
#      this script itself has.
#   2. its own system-call filter on top of whatever filter the container already has: more filters on
#      the Seccomp_filters line than this script itself has.
# A browser started with --no-sandbox shows neither. A browser without --no-sandbox that cannot build its
# sandbox does not start at all, which is reported as "could not tell" with its own message.
set -u

BROWSER="${1:-/usr/bin/chromium-browser}"
WAIT="${JARVIS_SANDBOX_WAIT:-60}"

[ "$(id -u)" -ne 0 ] || { echo "sandbox check: run this as the desktop's user, not as root." >&2; exit 2; }
[ -x "$BROWSER" ] || { echo "sandbox check: $BROWSER is not there." >&2; exit 2; }

work="$(mktemp -d)" || { echo "sandbox check: no folder to work in could be made." >&2; exit 2; }
[ -n "$work" ] && [ -d "$work" ] || { echo "sandbox check: no folder to work in could be made." >&2; exit 2; }
browser=""
# shellcheck disable=SC2329  # called by the trap below
cleanup() {
    local _
    if [ -n "$browser" ]; then
        kill "$browser" 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do   # a browser that will not go is not waited for
            kill -0 "$browser" 2>/dev/null || break
            sleep 0.5
        done
        kill -KILL "$browser" 2>/dev/null
        # Anything the browser left behind, found by the profile folder only this run uses.
        pkill -KILL -f -- "--user-data-dir=$work/profile" 2>/dev/null
        wait "$browser" 2>/dev/null
    fi
    rm -rf "$work"
}
trap cleanup EXIT

field() {  # field NAME PID: what follows "NAME:" in the process's status
    sed -n "s/^$1:[[:space:]]*//p" "/proc/$2/status" 2>/dev/null
}

# The browser gets a home and a scratch folder of its own for this, so that the desktop user's own
# profile is left alone and nothing stays behind.
# JARVIS_SANDBOX_EXTRA exists for this script's own test (it passes --no-sandbox to see the check fail).
mkdir "$work/home" "$work/tmp"
# shellcheck disable=SC2086
HOME="$work/home" TMPDIR="$work/tmp" "$BROWSER" --headless=new --no-first-run --disable-gpu --user-data-dir="$work/profile" \
    ${JARVIS_SANDBOX_EXTRA:-} 'data:text/html,sandbox check' > "$work/log" 2>&1 &
browser=$!

# A page process of that browser: among everything descended from it, the first of type renderer.
page_of() {
    ps -e -o pid=,ppid=,args= | awk -v root="$1" '
        {pid[NR] = $1; parent[$1] = $2; line[$1] = $0}
        END {
            for (i = 1; i <= NR; i++) {
                p = pid[i]
                for (hops = 0; p != "" && p != root && hops < 64; hops++) p = parent[p]
                if (p == root && line[pid[i]] ~ /--type=renderer/) {print pid[i]; exit}
            }
        }'
}

page=""
for _ in $(seq 1 "$((WAIT * 2))"); do
    kill -0 "$browser" 2>/dev/null || break
    page="$(page_of "$browser")"
    [ -z "$page" ] || break
    sleep 0.5
done
if [ -z "$page" ]; then
    echo "sandbox check: the browser did not start a page process. Its last words:" >&2
    grep -v 'dbus' "$work/log" | tail -n 3 >&2
    exit 2
fi

ours="$(field NSpid $$ | wc -w)"
theirs="$(field NSpid "$page" | wc -w)"
base="$(field Seccomp_filters $$)"
# A page process puts its filter on a moment after it appears, and never takes it off again: look for a few
# seconds before saying it is not there.
for _ in $(seq 1 20); do
    mode="$(field Seccomp "$page")"
    filters="$(field Seccomp_filters "$page")"
    [ -n "$filters" ] && [ -n "$base" ] && [ "$filters" -gt "$base" ] && break
    kill -0 "$page" 2>/dev/null || break
    sleep 0.25
done
if [ "$theirs" -eq 0 ] || [ -z "$filters" ] || [ -z "$base" ]; then
    echo "sandbox check: this kernel does not report what is needed (NSpid, Seccomp_filters)." >&2
    exit 2
fi

result=0
if [ "$theirs" -gt "$ours" ]; then
    echo "sandbox layer 1, own namespaces: on"
else
    echo "sandbox layer 1, own namespaces: OFF"
    result=1
fi
if [ "$mode" = "2" ] && [ "$filters" -gt "$base" ]; then
    echo "sandbox layer 2, own system-call filter: on"
else
    echo "sandbox layer 2, own system-call filter: OFF"
    result=1
fi
exit "$result"
