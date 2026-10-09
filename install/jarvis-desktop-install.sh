#!/usr/bin/env bash
# Brings a Jarvis desktop container to the state this release describes. The installer runs it once and
# `update` runs it after every release. Every step looks at what is already there, so running it again
# is safe. This container never holds credentials: no .env folder is made here.
#
#   jarvis-desktop-install.sh           apply
#   jarvis-desktop-install.sh --check   change nothing; say whether the container is as it should be
#
# What it sets up: a Linux desktop streamed to a web browser (the linuxserver.io Webtop image in Docker),
# where Jarvis's panel and Jarvis's browser live. The desktop has no sign-in of its own, and whoever
# reaches its control port can type, click and read the screen. So:
#   1. From outside, a firewall in this container lets in only the addresses named at install (your
#      reverse proxy, which asks who you are), and only to the desktop's HTTPS port.
#   2. From inside, the same firewall keeps the desktop's own user, and with it every web page open in
#      the desktop's browser, away from the desktop's control ports. A page must not be able to drive
#      the desktop it is shown on.
#   3. Chromium keeps its sandbox. The image's launcher starts it with --no-sandbox inside a Proxmox
#      container; ours (desktop/chromium) never does, and the sandbox is looked at after every install.
#   4. The image is named by its digest, so the desktop changes only with a release of Jarvis.
#   5. Jarvis's panel comes from the brain container, over TLS. The brain signs for itself with an authority
#      of its own; the installer on the Proxmox node hands that authority's certificate to this container,
#      and the desktop's browser is told to trust it for the brain's address and for nothing else.
#   6. The desktop's own session must stay up: after every install it is looked at once it has run long
#      enough to tell one that ends and is started over (desktop/session-check.sh, desktop/bwrap).
# A desktop that does not pass these checks is stopped.
set -euo pipefail
here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source=misc/install.func
. "$here/../misc/install.func"

need_root
[ "$(site_get JARVIS_ROLE)" = "desktop" ] || die "this container is not a Jarvis desktop ($JARVIS_SITE)."

SRC="$JARVIS_CHECKOUT/desktop"
DEST=/opt/jarvis-desktop
NAME=jarvis-desktop
PORT=3001                 # the desktop's HTTPS port, the only one opened to anybody
OWN_PORTS="3000, 3001, 8082"  # everything the desktop listens on: HTTP, HTTPS and its control port
DESKTOP_UID=1000          # the user the desktop and its browsers run as (PUID below)
FIREWALL=/etc/systemd/system/jarvis-desktop-firewall.service
DOCKER_NEEDS=/etc/systemd/system/docker.service.d/jarvis-desktop-firewall.conf
IMAGE="$(cat "$SRC/image")"
ALLOW="$(site_get JARVIS_DESKTOP_ALLOW)"
ORIGIN="$(site_get JARVIS_DESKTOP_ORIGIN)"
BRAIN_URL="$(site_get JARVIS_BRAIN_URL)"   # where the brain answers, such as https://192.0.2.20:8443; empty for none
BRAIN_CA="$JARVIS_ETC/brain-ca.pem"        # the brain's certificate authority, put here by the installer on the node
BRAIN_HOST=""
APP_FILES=(chromium bwrap jarvis-session.sh jarvis-session.desktop panel.html home.html Oxanium.ttf
    Oxanium-LICENSE.txt icon.png sandbox-check.sh session-check.sh)

# The settings come from a file a person can edit; they end up in a firewall rule and a Docker file.
[[ "$IMAGE" =~ ^[a-z0-9./-]+@sha256:[0-9a-f]{64}$ ]] || die "desktop/image does not name an image by its digest."
[ -z "$ALLOW" ] || address_list_ok "$ALLOW" \
    || die "JARVIS_DESKTOP_ALLOW in $JARVIS_SITE must be addresses (or networks by their first address, no wider than /8) with commas between, not '$ALLOW'."
[ -z "$ORIGIN" ] || origin_ok "$ORIGIN" \
    || die "JARVIS_DESKTOP_ORIGIN in $JARVIS_SITE must look like https://desktop.example.org, not '$ORIGIN'."

brain_ca_ok() {  # one certificate in PEM form, and nothing else in the file
    local body
    [ -s "$BRAIN_CA" ] && [ "$(grep -Ec -- '^-{5}BEGIN CERTIFICATE-{5}$' "$BRAIN_CA")" = "1" ] \
        && [ "$(grep -Ec -- '-{5}' "$BRAIN_CA")" = "2" ] || return 1
    body="$(grep -v -- '^-----' "$BRAIN_CA" | tr -d '\r\n')"
    [ "${#body}" -ge 100 ] && [ "${#body}" -le 8000 ] && [[ "$body" =~ ^[A-Za-z0-9+/]+={0,2}$ ]]
}
if [ -n "$BRAIN_URL" ]; then
    [[ "$BRAIN_URL" =~ ^https://(([0-9]{1,3}\.){3}[0-9]{1,3}):([1-9][0-9]{1,4})$ ]] \
        || die "JARVIS_BRAIN_URL in $JARVIS_SITE must look like https://192.0.2.20:8443, not '$BRAIN_URL'."
    BRAIN_HOST="${BASH_REMATCH[1]}"
    brain_ca_ok || die "the brain's certificate authority ($BRAIN_CA) is missing or is not one certificate. Run the desktop's installer on the Proxmox node again, with var_brain."
fi

gpu() {  # the render device this container was given, if any
    local node
    for node in "${JARVIS_DRI_DIR:-/dev/dri}"/renderD*; do  # the folder can be named for this script's tests
        if [ -c "$node" ]; then
            printf '%s' "$node"
            return 0
        fi
    done
}

timezone() {
    local zone
    zone="$(readlink -f /etc/localtime 2>/dev/null || true)"
    zone="${zone#/usr/share/zoneinfo/}"
    [[ "$zone" =~ ^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$ ]] || zone="Etc/UTC"
    printf '%s' "$zone"
}

# One number for everything the desktop is made from. It is written into the Docker file as a label, so
# Docker makes the desktop anew exactly when one of those files changed.
files_mark() {
    { (cd "$SRC" && cat image "${APP_FILES[@]}"); policy_file; brain_js; } | sha256sum | cut -c1-16
}

# The browser's rules: the ones of this release and, when this desktop has a brain, the brain's certificate
# authority, trusted for the brain's address alone. Both kinds of name are limited: left unsaid, a kind is
# allowed in full, and the authority could then sign for any web site's name. The one name allowed is a
# name no machine can have.
policy_file() {
    if [ -z "$BRAIN_URL" ]; then
        cat "$SRC/policy.json"
        return 0
    fi
    sed '$d' "$SRC/policy.json" | sed '$s/$/,/'
    printf '  "CACertificatesWithConstraints": [{"certificate": "%s", "constraints": {"permitted_cidrs": ["%s/32"], "permitted_dns_names": ["jarvis-brain.invalid"]}}]\n}\n' \
        "$(grep -v -- '^-----' "$BRAIN_CA" | tr -d '\r\n')" "$BRAIN_HOST"
}

brain_js() {  # read by the panel's start page: where to find the brain
    printf '%s\n' "// Written by the Jarvis installer. Where this desktop's brain answers; empty when it has none." \
        "window.JARVIS_BRAIN = \"$BRAIN_URL\";"
}

# What the brain says to this desktop, asked from this container (the desktop's programs have the same address).
brain_answer() {
    curl -s -o /dev/null -m 5 -w '%{http_code}' --cacert "$BRAIN_CA" "$BRAIN_URL/panel/ping" 2>/dev/null || true
}

about_brain() {  # says how things stand with the brain; a brain that is away is no fault of the desktop
    [ -n "$BRAIN_URL" ] || { warn "this desktop has no brain yet, so Jarvis's panel is a placeholder. Run the desktop's installer on the Proxmox node with var_brain."; return 0; }
    case "$(brain_answer)" in
        204) ok "the brain at $BRAIN_URL answers this desktop" ;;
        403) warn "the brain at $BRAIN_URL does not let this desktop in. Run the brain's installer with var_panel_allow=$(own_address)." ;;
        *) warn "the brain at $BRAIN_URL does not answer this desktop. If its installer was not yet run with var_panel_allow=$(own_address), do that; the panel keeps trying." ;;
    esac
}

firewall_rules() {
    printf '%s\n' "# Written by the Jarvis installer; changed by hand, it is overwritten at the next update." \
        "# In: everything arriving at this container is dropped, except answers to what it asked for" \
        "# itself and the desktop's HTTPS port for the addresses named in JARVIS_DESKTOP_ALLOW." \
        "# Out: the desktop's user (and so every page in its browser) cannot reach the desktop's own ports." \
        "# Only this table is touched, so Docker's own rules stay as they are." \
        "table inet jarvis_desktop" \
        "delete table inet jarvis_desktop" \
        "table inet jarvis_desktop {" \
        "    chain input {" \
        "        type filter hook input priority filter; policy drop;" \
        "        iifname \"lo\" accept" \
        "        ct state established,related accept" \
        "        ct state invalid drop" \
        "        meta l4proto { icmp, ipv6-icmp } accept" \
        "        udp sport 67 udp dport 68 accept" \
        "        udp sport 547 udp dport 546 accept"
    [ -z "$ALLOW" ] || printf '        ip saddr { %s } tcp dport %s counter accept\n' "${ALLOW//,/, }" "$PORT"
    printf '%s\n' "    }" \
        "    chain output {" \
        "        type filter hook output priority filter; policy accept;" \
        "        meta skuid $DESKTOP_UID fib daddr type local tcp dport { $OWN_PORTS } counter reject with tcp reset" \
        "    }" \
        "}"
}

# Loaded before the network comes up, and Docker does not start without it: the desktop never runs
# unguarded, not even for a moment at boot.
firewall_unit() {
    printf '%s\n' "[Unit]" "Description=Jarvis desktop: who may reach this container" \
        "DefaultDependencies=no" "Wants=network-pre.target" "Before=network-pre.target docker.service shutdown.target" \
        "Conflicts=shutdown.target" "" "[Service]" "Type=oneshot" "RemainAfterExit=yes" \
        "ExecStart=/usr/sbin/nft -f $JARVIS_ETC/desktop.nft" "" "[Install]" "WantedBy=multi-user.target"
}

docker_needs() {
    printf '%s\n' "# Written by the Jarvis installer: no Docker without the firewall of the desktop." \
        "[Unit]" "Requires=jarvis-desktop-firewall.service" "After=jarvis-desktop-firewall.service"
}

compose_file() {
    local device
    device="$(gpu)"
    printf '%s\n' "# Written by the Jarvis installer; changed by hand, it is overwritten at the next update." \
        "services:" \
        "  desktop:" \
        "    image: $IMAGE" \
        "    container_name: $NAME" \
        "    labels:" \
        "      jarvis.files: \"$(files_mark)\"" \
        "    # Docker publishes nothing: the firewall of this container decides who gets in." \
        "    network_mode: host" \
        "    shm_size: 1gb" \
        "    # Chromium's sandbox needs namespaces of its own, which Docker's own filter forbids." \
        "    security_opt:" \
        "      - seccomp=unconfined" \
        "    restart: unless-stopped"
    [ -z "$device" ] || printf '%s\n' "    devices:" "      - $device:$device"
    printf '%s\n' "    environment:" \
        "      PUID: \"$DESKTOP_UID\"" \
        "      PGID: \"$DESKTOP_UID\"" \
        "      TZ: \"$(timezone)\"" \
        "      TITLE: \"Jarvis\"" \
        "      START_DOCKER: \"false\"" \
        "      # No shell commands over the viewer's connection, and no way for the desktop's user to become root." \
        "      SELKIES_COMMAND_ENABLED: \"false\"" \
        "      DISABLE_SUDO: \"true\"" \
        "      # Which web address a viewer's page may come from. Empty: only the address it was fetched from." \
        "      SELKIES_ALLOWED_ORIGINS: \"$ORIGIN\""
    [ -z "$device" ] || printf '      DRINODE: "%s"\n      DRI_NODE: "%s"\n' "$device" "$device"
    printf '%s\n' "    volumes:" \
        "      - $DEST/config:/config" \
        "      - $DEST/app:/opt/jarvis-desktop:ro" \
        "      - $DEST/app/chromium:/usr/bin/chromium:ro" \
        "      - $DEST/app/chromium:/usr/local/bin/wrapped-chromium:ro" \
        "      # Picture loaders ask this for a sandbox it cannot build here; ours says so (see the file)." \
        "      - $DEST/app/bwrap:/usr/bin/bwrap:ro" \
        "      - $DEST/app/jarvis-session.desktop:/etc/xdg/autostart/jarvis-session.desktop:ro" \
        "      # The picture a viewer's browser shows in its tab." \
        "      - $DEST/app/icon.png:/usr/share/selkies/www/icon.png:ro" \
        "      - $DEST/policies:/etc/chromium/policies/managed:ro"
}

in_desktop() { docker exec -u abc -e HOME=/config -e DISPLAY=:1 "$NAME" "$@"; }

code_of() {  # the answer of the desktop's own web server, asked from inside this container, as a number
    curl -sk -o /dev/null -m 5 -w '%{http_code}' "https://127.0.0.1:$PORT$1" 2>/dev/null || true
}

wait_for_answer() {  # the desktop needs a while after a start before it answers
    local tries="${JARVIS_DESKTOP_WAIT:-90}"
    until [ "$(code_of /)" = "200" ]; do
        tries=$((tries - 1))
        [ "$tries" -gt 0 ] || return 1
        sleep "${JARVIS_DESKTOP_PAUSE:-2}"
    done
}

# The web server answers before the desktop behind it does; that one starts last and takes some seconds
# more. Prints the last answer; true once it is an answer of the desktop itself (whatever it says).
wait_for_backend() {
    local tries="${JARVIS_DESKTOP_WAIT:-90}" code
    while true; do
        code="$(code_of /api/)"
        case "$code" in
            502|503|504|000|"") ;;
            *) return 0 ;;
        esac
        tries=$((tries - 1))
        [ "$tries" -gt 0 ] || { printf '%s' "$code"; return 1; }
        sleep "${JARVIS_DESKTOP_PAUSE:-2}"
    done
}

# Looks at the running desktop and prints what is wrong with it, one line each; prints nothing when all is
# well. Used by apply (which stops a desktop that fails) and by --check (which only reports).
desktop_faults() {
    local code verdict status=0 target
    wait_for_answer || { echo "the desktop does not answer on https://127.0.0.1:$PORT"; return 0; }
    # The web server in front of the desktop must still reach the control port behind it (it runs as
    # another user than the desktop's, so the rule that keeps pages away does not apply to it).
    code="$(wait_for_backend)" || echo "the desktop's web server cannot reach the desktop behind it (answer $code)"
    # And the desktop's own user must not: tried from inside, as that user, the way a page would.
    [ "$(in_desktop id -u 2>/dev/null)" = "$DESKTOP_UID" ] \
        || echo "the desktop's user is not user $DESKTOP_UID, so the firewall rule that keeps pages off the desktop's ports does not apply"
    for target in 127.0.0.1/3000 127.0.0.1/3001 127.0.0.1/8082 ::1/3000 ::1/3001 ::1/8082; do
        if in_desktop bash -c "exec 3<>/dev/tcp/$target" 2>/dev/null; then
            echo "a program of the desktop's user can connect to the desktop's own port ${target#*/} (${target%/*}): a web page could drive the desktop"
        fi
    done
    # shellcheck disable=SC2016  # $tool is for the shell inside the desktop
    in_desktop sh -c 'for tool in xdotool xprop xwininfo pgrep flock awk ps; do command -v "$tool" >/dev/null || exit 1; done' \
        || echo "the desktop image lacks a program the session script needs"
    # Every way to start the browser must be ours: the two launchers of the image, and no menu entry or
    # running browser with the sandbox switched off.
    in_desktop sh -c 'cmp -s /usr/bin/chromium /opt/jarvis-desktop/chromium && cmp -s /usr/local/bin/wrapped-chromium /opt/jarvis-desktop/chromium' \
        || echo "the image's Chromium launchers are not replaced by ours"
    in_desktop cmp -s /usr/bin/bwrap /opt/jarvis-desktop/bwrap \
        || echo "the image's bwrap is not replaced by ours: the desktop's programs cannot load their pictures"
    if in_desktop grep -rqs -e --no-sandbox /usr/share/applications /etc/xdg/autostart; then
        echo "a menu or autostart entry of the desktop starts a program with --no-sandbox"
    fi
    # (Written so that the line looking for it does not find itself.)
    if in_desktop sh -c 'ps -e -o args= | grep -q -e "[-]-no-sandbox"'; then
        echo "a program in the desktop is running with --no-sandbox"
    fi
    # The reason this container exists: pages from the internet run here, inside Chromium's sandbox.
    verdict="$(in_desktop /opt/jarvis-desktop/sandbox-check.sh /usr/bin/chromium 2>&1)" || status=$?
    printf '%s\n' "$verdict" | sed 's/^/   /' >&2
    case "$status" in
        0) ;;
        1) echo "Chromium's sandbox is NOT fully on in this container (see the lines above)" ;;
        *) echo "Chromium's sandbox could not be checked (see the lines above)" ;;
    esac
    # Last, because it may wait: the desktop's session must have run long enough to tell that it stays up.
    status=0
    verdict="$(in_desktop /opt/jarvis-desktop/session-check.sh 2>&1)" || status=$?
    case "$status" in
        0) ;;
        1) printf '%s\n' "$verdict" ;;
        *) echo "whether the desktop's session stays up could not be checked: ${verdict:-no answer}" ;;
    esac
}

apply() {
    local file faults

    say "Packages"
    apt_install ca-certificates curl git openssh-client tzdata nftables docker.io docker-compose-v2

    say "Commands"
    install_command "$JARVIS_CHECKOUT/bin/update" /usr/bin/update

    say "Who may reach this container"
    # The rules are tried before they replace the ones on disk: a file that does not load would leave
    # the container without a firewall at its next start.
    firewall_rules > "$JARVIS_ETC/desktop.nft.new"
    if ! nft -c -f "$JARVIS_ETC/desktop.nft.new"; then
        rm -f "$JARVIS_ETC/desktop.nft.new"
        die "the firewall rules for these settings are not valid. Nothing was changed; the rules in force stay."
    fi
    chmod 0644 "$JARVIS_ETC/desktop.nft.new"
    mv -f "$JARVIS_ETC/desktop.nft.new" "$JARVIS_ETC/desktop.nft"
    install -d -m 0755 "$(dirname "$DOCKER_NEEDS")"
    firewall_unit | write_file "$FIREWALL" 0644
    docker_needs | write_file "$DOCKER_NEEDS" 0644
    systemctl daemon-reload
    systemctl enable jarvis-desktop-firewall.service >/dev/null 2>&1 \
        || die "the firewall could not be set to load at start. See: systemctl status jarvis-desktop-firewall"
    nft -f "$JARVIS_ETC/desktop.nft" || die "the firewall rules could not be loaded."
    if [ -n "$ALLOW" ]; then
        ok "port $PORT is open to $ALLOW and to nobody else"
    else
        warn "nobody may open the desktop yet: name your reverse proxy's address (var_desktop_allow) and run the installer again."
    fi
    [ -n "$ORIGIN" ] || warn "no web address is named for the desktop (var_desktop_origin): a viewer connects only if your reverse proxy passes the name you typed on to the desktop (the Host header)."

    say "The desktop's files"
    install -d -m 0755 "$DEST" "$DEST/app" "$DEST/policies"
    [ -d "$DEST/config" ] || install -d -m 0755 -o "$DESKTOP_UID" -g "$DESKTOP_UID" "$DEST/config"
    for file in "${APP_FILES[@]}"; do
        case "$file" in
            *.sh|chromium|bwrap) install -m 0755 "$SRC/$file" "$DEST/app/$file" ;;
            *) install -m 0644 "$SRC/$file" "$DEST/app/$file" ;;
        esac
    done
    policy_file | write_file "$DEST/policies/jarvis.json" 0644
    brain_js | write_file "$DEST/app/brain.js" 0644
    compose_file | write_file "$DEST/compose.yaml" 0644

    say "The desktop (the first time, an image of about 2 GB is fetched)"
    systemctl enable --now docker >/dev/null 2>&1 || die "Docker does not start in this container. See: journalctl -u docker"
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        docker compose -f "$DEST/compose.yaml" pull -q || die "the desktop image could not be fetched."
    fi
    docker compose -f "$DEST/compose.yaml" up -d --remove-orphans --quiet-pull >/dev/null \
        || die "the desktop did not start. See: docker logs $NAME"

    say "Looking at the desktop"
    faults="$(desktop_faults)"
    if [ -n "$faults" ]; then
        # A desktop that is not as it should be is not left running.
        docker compose -f "$DEST/compose.yaml" stop >/dev/null 2>&1 || true
        printf 'jarvis: %s\n' "$faults" >&2
        die "the desktop was stopped because of the above. Docker, the firewall and the files stay in place. See: docker logs $NAME"
    fi
    ok "desktop running from $IMAGE"
    about_brain
}

check() {
    local wrong=() state want file fault

    cmp -s "$JARVIS_CHECKOUT/bin/update" /usr/bin/update || wrong+=("/usr/bin/update is not the one of this release")
    nft list table inet jarvis_desktop >/dev/null 2>&1 || wrong+=("the firewall table jarvis_desktop is not loaded")
    [ "$(systemctl is-enabled jarvis-desktop-firewall.service 2>/dev/null)" = "enabled" ] \
        || wrong+=("the firewall is not set to load at start")
    firewall_rules | cmp -s - "$JARVIS_ETC/desktop.nft" || wrong+=("$JARVIS_ETC/desktop.nft is not what the settings ask for")
    firewall_unit | cmp -s - "$FIREWALL" || wrong+=("$FIREWALL is not the one of this release")
    docker_needs | cmp -s - "$DOCKER_NEEDS" || wrong+=("Docker is not tied to the firewall ($DOCKER_NEEDS)")
    compose_file | cmp -s - "$DEST/compose.yaml" || wrong+=("$DEST/compose.yaml is not what this release and the settings ask for")
    for file in "${APP_FILES[@]}"; do
        cmp -s "$SRC/$file" "$DEST/app/$file" || wrong+=("$DEST/app/$file is not the one of this release")
    done
    policy_file | cmp -s - "$DEST/policies/jarvis.json" || wrong+=("$DEST/policies/jarvis.json is not the one of this release and its settings")
    brain_js | cmp -s - "$DEST/app/brain.js" || wrong+=("$DEST/app/brain.js is not what the settings ask for")

    want="true|$IMAGE|$(files_mark)"
    state="$(docker inspect -f '{{.State.Running}}|{{.Config.Image}}|{{index .Config.Labels "jarvis.files"}}' "$NAME" 2>/dev/null || true)"
    if [ "$state" != "$want" ]; then
        wrong+=("the desktop is not running from this release's image and files (is: ${state:-not there})")
    else
        while IFS= read -r fault; do
            [ -z "$fault" ] || wrong+=("$fault")
        done <<<"$(desktop_faults)"
    fi

    if [ "${#wrong[@]}" -gt 0 ]; then
        printf 'jarvis: not as it should be: %s\n' "${wrong[@]}" >&2
        return 1
    fi
    about_brain
    ok "Jarvis desktop: as it should be"
}

case "${1:-}" in
    "") apply ;;
    --check) check ;;
    *) die "unknown option '$1'. Use no option, or --check." ;;
esac
