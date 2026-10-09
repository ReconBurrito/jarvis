#!/usr/bin/env bash
# Jarvis desktop: creates the container that runs the streamed Linux desktop where Jarvis is used and where
# its browser opens web pages. It holds no credentials. Run as root on a Proxmox VE node:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/ReconBurrito/jarvis/main/ct/jarvis-desktop.sh)"
#
# Without questions, from a file of answers (see defaults/example.vars):
#
#   JARVIS_ANSWERS=/root/jarvis-desktop.vars bash -c "$(curl -fsSL https://raw.githubusercontent.com/ReconBurrito/jarvis/main/ct/jarvis-desktop.sh)"
#
# License: MIT
set -euo pipefail

ROLE=desktop
declare -A ROLE_DEFAULTS=([hostname]=jarvis-desktop [disk]=32 [cpu]=4 [ram]=3072 [swap]=512)

JARVIS_REPO="${JARVIS_REPO:-ReconBurrito/jarvis}"
JARVIS_REF="${JARVIS_REF:-main}"
JARVIS_SRC=""
JARVIS_SRC_TMP=""

# Everything below is inside main, so a download of this script that was cut short runs nothing.
main() {
    trap '[ -z "$JARVIS_SRC_TMP" ] || rm -rf "$JARVIS_SRC_TMP"' EXIT
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] \
        && [ -f "$(dirname "${BASH_SOURCE[0]}")/../misc/build.func" ]; then
        JARVIS_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    else
        # Started straight from the web: take this repository's files, all from one ref.
        JARVIS_SRC_TMP="$(mktemp -d)"
        JARVIS_SRC="$JARVIS_SRC_TMP"
        curl -fsSL "https://codeload.github.com/$JARVIS_REPO/tar.gz/$JARVIS_REF" | tar -xz -C "$JARVIS_SRC" --strip-components=1 \
            || { echo "jarvis-install: could not fetch $JARVIS_REPO at $JARVIS_REF. Nothing was changed." >&2; exit 1; }
    fi
    # shellcheck source=misc/build.func
    . "$JARVIS_SRC/misc/build.func"
    build_main "$@"
}

main "$@"
