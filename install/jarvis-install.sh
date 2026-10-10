#!/usr/bin/env bash
# Brings a Jarvis brain container to the state this release describes. The installer runs it once and
# `update` runs it after every release. Every step looks at what is already there, so running it again
# is safe.
#
#   jarvis-install.sh           apply
#   jarvis-install.sh --check   change nothing that is installed; say whether the container is as it should be
#                               (the model is loaded and asked one question, which Jarvis's audit log records)
#
# The local model: Ollama serves it on this container only (127.0.0.1), on the GPU through Vulkan when the
# container was given a render device. Ollama is taken from its own release by version, and the archive
# must match the checksum this release of Jarvis records (brain/ollama). After every install the model is
# loaded once and Ollama is asked where it runs: a model that sits on the processor although there is a
# GPU counts as a fault.
#
# Jarvis itself is Python. Its packages are the ones brain/requirements.txt names, each by version and
# checksum; pip installs nothing that does not match. They go into an environment of their own under
# /opt/jarvis-venv, which only root can change, and Jarvis's code is read from this release's src folder.
# The check ends with `jarvis doctor`, which asks the model one question through the whole of Jarvis.
#
# Jarvis runs as a service (jarvis.service, user jarvis) on one HTTPS port, for its panel on the desktop:
#   1. A firewall rule in this container lets only the addresses named at install reach that port
#      (var_panel_allow: the desktop container). Nothing else about this container's network is changed.
#   2. The port speaks TLS with a certificate made here, for this machine's address and nothing else, signed
#      by an authority made here whose key only root can read. The desktop's installer takes the
#      authority's certificate from this container and makes its browser trust it for this one address.
set -euo pipefail
here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source=misc/install.func
. "$here/../misc/install.func"

need_root
[ "$(site_get JARVIS_ROLE)" = "brain" ] || die "this container is not a Jarvis brain ($JARVIS_SITE)."
ENV_DIR="$(site_get JARVIS_ENV_DIR)"
ENV_DIR="${ENV_DIR:-/etc/jarvis/secrets}"
SECRETS_MODE="$(site_get JARVIS_SECRETS_MODE)"
SECRETS_MODE="${SECRETS_MODE:-plain}"
STATE=/var/lib/jarvis          # what Jarvis itself keeps; it belongs to the user jarvis
WORK=/var/lib/jarvis-install   # the installer's own scratch folder; root only

MODEL="$(site_get JARVIS_LOCAL_MODEL)"
MODEL="${MODEL:-qwen3:8b}"
EMBED="$(site_get JARVIS_EMBED_MODEL)"
EMBED="${EMBED:-qwen3-embedding:0.6b}"
model_ok "$MODEL" || die "JARVIS_LOCAL_MODEL in $JARVIS_SITE must be a model as Ollama names it (such as qwen3:8b) or none, not '$MODEL'."
model_ok "$EMBED" || die "JARVIS_EMBED_MODEL in $JARVIS_SITE must be a model as Ollama names it or none, not '$EMBED'."
SOPS_VERSION="$(sed -n 's/^version=//p' "$JARVIS_CHECKOUT/brain/sops")"
SOPS_SHA256="$(sed -n 's/^sha256=//p' "$JARVIS_CHECKOUT/brain/sops")"
[[ "$SOPS_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ "$SOPS_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || die "brain/sops does not name a release and its checksum."
SOPS=/usr/local/bin/sops
KEYS=/var/lib/jarvis-key          # the vault identity, sealed with the owner's passphrase; root only (jarvis-unlock)
TRUST="$JARVIS_ETC/trust"         # certificates of the lab's systems, which the installer on the node puts here
OLLAMA_VERSION="$(sed -n 's/^version=//p' "$JARVIS_CHECKOUT/brain/ollama")"
OLLAMA_SHA256="$(sed -n 's/^sha256=//p' "$JARVIS_CHECKOUT/brain/ollama")"
[[ "$OLLAMA_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ "$OLLAMA_SHA256" =~ ^[0-9a-f]{64}$ ]] \
    || die "brain/ollama does not name a release and its checksum."
OLLAMA_ARCHIVE="https://github.com/ollama/ollama/releases/download/$OLLAMA_VERSION/ollama-linux-amd64.tar.zst"
OLLAMA_API=http://127.0.0.1:11434
OLLAMA_MARK=/usr/local/lib/ollama/.jarvis-release   # which archive is unpacked there
OLLAMA_UNIT=/etc/systemd/system/ollama.service
CONTEXT=16384
PORT=8443                                            # Jarvis's own port: the panel and what the panel asks for
TLS="$JARVIS_ETC/tls"
FIREWALL_RULES="$JARVIS_ETC/brain.nft"
FIREWALL=/etc/systemd/system/jarvis-firewall.service
SERVICE=/etc/systemd/system/jarvis.service
# jarvis-fsd: Jarvis's hands on this machine's files, as root, for the user jarvis alone (src/jarvis/selffs).
FSD=/usr/local/sbin/jarvis-fsd
FS_UNIT=/etc/systemd/system/jarvis-fs.service
ALLOW="$(site_get JARVIS_PANEL_ALLOW)"
[ -z "$ALLOW" ] || address_list_ok "$ALLOW" \
    || die "JARVIS_PANEL_ALLOW in $JARVIS_SITE must be addresses (or networks by their first address, no wider than /8) with commas between, not '$ALLOW'."
VENVS=/opt/jarvis-venv                               # one folder per set of packages, and the link `current`
REQUIREMENTS="$JARVIS_CHECKOUT/brain/requirements.txt"

gpu() {  # the render device this container was given, if any
    local node
    for node in "${JARVIS_DRI_DIR:-/dev/dri}"/renderD*; do  # the folder can be named for this script's tests
        if [ -c "$node" ]; then
            printf '%s' "$node"
            return 0
        fi
    done
}

ollama_unit() {
    printf '%s\n' "# Written by the Jarvis installer; changed by hand, it is overwritten at the next update." \
        "[Unit]" "Description=Ollama, the local model server of Jarvis" "After=network-online.target" \
        "Wants=network-online.target" "" "[Service]" "ExecStart=/usr/local/bin/ollama serve" "User=ollama" \
        "Group=ollama" "Restart=always" "RestartSec=3" \
        "# Answers on this container only." \
        "Environment=\"OLLAMA_HOST=127.0.0.1:11434\"" \
        "Environment=\"OLLAMA_CONTEXT_LENGTH=$CONTEXT\""
    [ -z "$(gpu)" ] || printf '%s\n' "# The GPU is reached through Vulkan." "Environment=\"OLLAMA_VULKAN=1\""
    printf '%s\n' "" "[Install]" "WantedBy=multi-user.target"
}

api() {  # api PATH [JSON]: asks Ollama; prints its answer. A model may take minutes to fetch or load.
    if [ -n "${2:-}" ]; then
        curl -fsS -m "${JARVIS_OLLAMA_PATIENCE:-3600}" -H 'Content-Type: application/json' -d "$2" "$OLLAMA_API$1"
    else
        curl -fsS -m 20 "$OLLAMA_API$1"
    fi
}

wait_for_ollama() {
    local tries="${JARVIS_OLLAMA_WAIT:-60}"
    until api /api/version >/dev/null 2>&1; do
        tries=$((tries - 1))
        [ "$tries" -gt 0 ] || return 1
        sleep "${JARVIS_OLLAMA_PAUSE:-1}"
    done
}

have_model() {  # whether Ollama holds that model already
    api /api/tags 2>/dev/null | python3 -I -c '
import json, sys
want = sys.argv[1] if ":" in sys.argv[1] else sys.argv[1] + ":latest"
names = [m.get("name", "") for m in json.load(sys.stdin).get("models", [])]
sys.exit(0 if want in names else 1)' "$1" 2>/dev/null
}

# Loads the model and prints how much of it sits on the GPU, as a whole number from 0 to 100.
model_on_gpu() {
    api /api/generate "{\"model\": \"$MODEL\", \"prompt\": \"\", \"stream\": false, \"keep_alive\": \"5m\"}" >/dev/null || return 1
    api /api/ps | python3 -I -c '
import json, sys
want = sys.argv[1] if ":" in sys.argv[1] else sys.argv[1] + ":latest"
for model in json.load(sys.stdin).get("models", []):
    if model.get("name") == want and model.get("size"):
        print(round(100 * model.get("size_vram", 0) / model["size"]))
        sys.exit(0)
sys.exit(1)' "$MODEL"
}

# Looks at the running model server and prints what is wrong, one line each; nothing when all is well.
model_faults() {
    local share name
    wait_for_ollama || { echo "Ollama does not answer on $OLLAMA_API (see: journalctl -u ollama)"; return 0; }
    for name in "$MODEL" "$EMBED"; do
        [ "$name" = "none" ] || have_model "$name" || echo "the model $name is not there"
    done
    have_model "$MODEL" || return 0
    if ! share="$(model_on_gpu 2>/dev/null)"; then
        echo "the model $MODEL does not load (see: journalctl -u ollama)"
    elif [ -z "$(gpu)" ]; then
        printf '   %s\n' "no GPU was given to this container: $MODEL runs on the processor" >&2
    elif [ "$share" -eq 0 ]; then
        echo "the model $MODEL runs on the processor although this container has a GPU (see: journalctl -u ollama)"
    elif [ "$share" -lt 100 ]; then
        warn "only $share percent of $MODEL fits on the GPU; the rest runs on the processor and answers will be slow."
    else
        printf '   %s\n' "$MODEL is on the GPU (100 percent)" >&2
    fi
}

# sops reads the vault. It is taken from its own release by version, and must match the checksum this release
# of Jarvis records (brain/sops).
sops_ok() { [ -x "$SOPS" ] && [ "$(sha256sum "$SOPS" | cut -d' ' -f1)" = "$SOPS_SHA256" ]; }

install_sops() {
    local tmp
    sops_ok && return 0
    [ "$(uname -m)" = "x86_64" ] || die "sops is installed on x86_64 only; this is $(uname -m). Use JARVIS_SECRETS_MODE=plain instead."
    install -d -m 0700 -o root -g root "$WORK"
    rm -f "${WORK:?}"/sops.*
    tmp="$(mktemp "$WORK/sops.XXXXXX")"
    printf '   %s\n' "fetching sops $SOPS_VERSION"
    curl -fsSL --retry 3 -o "$tmp" "https://github.com/getsops/sops/releases/download/$SOPS_VERSION/sops-$SOPS_VERSION.linux.amd64" \
        || { rm -f "$tmp"; die "sops $SOPS_VERSION could not be fetched from github.com."; }
    echo "$SOPS_SHA256  $tmp" | sha256sum -c --status - \
        || { rm -f "$tmp"; die "the sops that arrived does not match the checksum this release records. Nothing of it was installed."; }
    install -m 0755 -o root -g root "$tmp" "$SOPS.new"
    mv -f "$SOPS.new" "$SOPS"
    rm -f "$tmp"
}

install_ollama() {
    local tmp strange
    if [ "$(cat "$OLLAMA_MARK" 2>/dev/null)" = "$OLLAMA_VERSION $OLLAMA_SHA256" ] && [ -x /usr/local/bin/ollama ]; then
        return 0
    fi
    [ "$(uname -m)" = "x86_64" ] || die "the local model server is installed on x86_64 only; this is $(uname -m). Set var_model=none to go without a local model."
    # Fetched into a folder only root can touch: what is checked there is what is unpacked.
    install -d -m 0700 -o root -g root "$WORK"
    rm -rf "$WORK"/ollama.*
    tmp="$(mktemp -d "$WORK/ollama.XXXXXX")"
    # shellcheck disable=SC2064  # the folder is named now, for whenever the script ends
    trap "rm -rf '$tmp'" EXIT
    printf '   %s\n' "fetching Ollama $OLLAMA_VERSION (about 2 GB)"
    curl -fsSL --retry 3 -o "$tmp/ollama.tar.zst" "$OLLAMA_ARCHIVE" \
        || die "Ollama $OLLAMA_VERSION could not be fetched from $OLLAMA_ARCHIVE."
    echo "$OLLAMA_SHA256  $tmp/ollama.tar.zst" | sha256sum -c --status - \
        || die "the Ollama archive that arrived does not match the checksum this release records. Nothing of it was installed."
    # Only what belongs under /usr/local/bin and /usr/local/lib may be in it.
    strange="$(zstd -dc "$tmp/ollama.tar.zst" | tar -tf - | grep -Ev '^(\./)?((bin|lib)(/.*)?)?$' | head -n 3 || true)"
    [ -n "$strange" ] || strange="$(zstd -dc "$tmp/ollama.tar.zst" | tar -tf - | grep -E '(^|/)\.\.(/|$)' | head -n 3 || true)"
    [ -z "$strange" ] || die "the Ollama archive holds paths that do not belong in it (${strange//$'\n'/, }). Nothing of it was installed."
    systemctl stop ollama >/dev/null 2>&1 || true
    rm -rf /usr/local/lib/ollama
    zstd -dc "$tmp/ollama.tar.zst" | tar -xf - -C /usr/local --no-same-owner --no-same-permissions \
        || die "the Ollama archive could not be unpacked."
    [ -x /usr/local/bin/ollama ] && [ -d /usr/local/lib/ollama ] || die "the Ollama archive did not hold what was expected."
    printf '%s\n' "$OLLAMA_VERSION $OLLAMA_SHA256" > "$OLLAMA_MARK"
    rm -rf "$tmp"
    trap - EXIT
    RESTART_OLLAMA=1
}

apply_model() {
    local name group faults
    RESTART_OLLAMA=0
    install_ollama
    id ollama >/dev/null 2>&1 || useradd --system --shell /bin/false --user-group --create-home --home-dir /var/lib/ollama ollama
    for group in render video; do  # the GPU's device belongs to the render group
        ! getent group "$group" >/dev/null || id -nG ollama | tr ' ' '\n' | grep -qx "$group" || { usermod -a -G "$group" ollama; RESTART_OLLAMA=1; }
    done
    if ! ollama_unit | cmp -s - "$OLLAMA_UNIT"; then
        ollama_unit | write_file "$OLLAMA_UNIT" 0644
        RESTART_OLLAMA=1
    fi
    systemctl daemon-reload
    systemctl enable ollama >/dev/null 2>&1 || die "Ollama could not be set to start at boot. See: systemctl status ollama"
    if [ "$RESTART_OLLAMA" = "1" ] || ! systemctl is-active --quiet ollama; then
        systemctl restart ollama || die "Ollama does not start. See: journalctl -u ollama"
    fi
    wait_for_ollama || die "Ollama does not answer on $OLLAMA_API. See: journalctl -u ollama"
    for name in "$MODEL" "$EMBED"; do
        [ "$name" != "none" ] || continue
        have_model "$name" && continue
        printf '   %s\n' "fetching the model $name (this can take a long while the first time)"
        api /api/pull "{\"model\": \"$name\", \"stream\": false}" | grep -q '"status": *"success"' \
            || die "the model $name could not be fetched. Check the name (ollama.com/library) and this container's way to the internet."
    done
    faults="$(model_faults)"
    [ -z "$faults" ] || { printf 'jarvis: %s\n' "$faults" >&2; die "the local model is not as it should be (above). What was installed stays in place."; }
    ok "local model $MODEL served by Ollama $OLLAMA_VERSION"
}

# The folder a set of packages lives in is named for the packages and for the Python they were built for.
venv_name() {
    { cat "$REQUIREMENTS"; python3 -I -c 'import sys; print("%d.%d" % sys.version_info[:2])'; } | sha256sum | cut -c1-16
}

venv_has_packages() {  # venv_has_packages FOLDER: it was built to the end and its packages load
    [ -f "$1/.complete" ] && "$1/bin/python" -I -B -c 'import cryptography, httpx' >/dev/null 2>&1
}

jarvis_loads() {  # jarvis_loads FOLDER: this release's own code loads in that environment
    "$1/bin/python" -I -B -c 'import jarvis.cli' >/dev/null 2>&1
}

apply_python() {
    local name dir log old now before
    [ -s "$REQUIREMENTS" ] || die "brain/requirements.txt is missing from this release."
    name="$(venv_name)"
    dir="$VENVS/$name"
    install -d -m 0755 -o root -g root "$VENVS"
    now="$(readlink "$VENVS/current" 2>/dev/null || true)"
    # Built anew only when it is not there or its packages do not load; never because of Jarvis's own code,
    # which is this release's affair and not the environment's.
    if ! venv_has_packages "$dir"; then
        printf '   %s\n' "building Jarvis's Python environment (its packages are fetched from PyPI and checked)"
        rm -rf "$dir"
        log="$(mktemp)"
        if ! python3 -I -m venv "$dir" >"$log" 2>&1 \
            || ! "$dir/bin/python" -I -m pip install --disable-pip-version-check --no-cache-dir --no-input \
                --require-hashes --only-binary :all: -r "$REQUIREMENTS" >"$log" 2>&1; then
            tail -n 8 "$log" >&2
            rm -rf "$dir" "$log"
            if [ "$now" = "$name" ]; then
                die "Jarvis's Python packages could not be installed (above). The environment that was in use was damaged and is gone; update --repair builds it again."
            fi
            die "Jarvis's Python packages could not be installed (above). The environment in use stays as it was."
        fi
        rm -f "$log"
        # Jarvis's own code is read from the release that is installed, wherever an update moves it.
        printf '%s\n' "$JARVIS_CHECKOUT/src" > "$("$dir/bin/python" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/jarvis.pth"
        : > "$dir/.complete"
        venv_has_packages "$dir" || { rm -rf "$dir"; die "Jarvis's Python environment was built but its packages do not load."; }
    fi
    jarvis_loads "$dir" || die "this release's own code does not load in its Python environment (try: $dir/bin/python -I -c 'import jarvis.cli'). The environment was left as it is."
    if [ "$now" != "$name" ]; then
        # The one in use until now is remembered, so going back a release needs nothing from the internet.
        [ -z "$now" ] || { ln -sfn "$now" "$VENVS/before.new"; mv -fT "$VENVS/before.new" "$VENVS/before"; }
        ln -sfn "$name" "$VENVS/current.new"
        mv -fT "$VENVS/current.new" "$VENVS/current"
    fi
    before="$(readlink "$VENVS/before" 2>/dev/null || true)"
    for old in "$VENVS"/*/; do
        old="$(basename "$old")"
        case "$old" in "$name"|"$before"|current|before) continue ;; esac
        rm -rf "${VENVS:?}/$old"
    done
}

describe_host() {  # where Jarvis runs, in words it can use about itself
    local kind
    kind="$(systemd-detect-virt 2>/dev/null || true)"
    case "$kind" in
        lxc|lxc-libvirt|docker|podman|systemd-nspawn|openvz) printf 'a Linux container named %s' "$(hostname)" ;;
        none|"") printf 'a computer named %s' "$(hostname)" ;;
        *) printf 'a virtual machine named %s' "$(hostname)" ;;
    esac
}

# ---------------------------------------------------------------- the service and who may reach it

firewall_rules() {
    printf '%s\n' "# Written by the Jarvis installer; changed by hand, it is overwritten at the next update." \
        "# Jarvis's port is open to the addresses named in JARVIS_PANEL_ALLOW and to this container itself," \
        "# and to nobody else. Nothing else about this container's network is decided here." \
        "table inet jarvis_brain" \
        "delete table inet jarvis_brain" \
        "table inet jarvis_brain {" \
        "    chain input {" \
        "        type filter hook input priority filter; policy accept;" \
        "        iifname \"lo\" accept"
    [ -z "$ALLOW" ] || printf '        ip saddr { %s } tcp dport %s counter accept\n' "${ALLOW//,/, }" "$PORT"
    printf '%s\n' "        tcp dport $PORT counter drop" "    }" "}"
}

# Loaded before the network comes up, and Jarvis does not start without it.
firewall_unit() {
    printf '%s\n' "[Unit]" "Description=Jarvis brain: who may reach Jarvis's port" \
        "DefaultDependencies=no" "Wants=network-pre.target" "Before=network-pre.target jarvis.service shutdown.target" \
        "Conflicts=shutdown.target" "" "[Service]" "Type=oneshot" "RemainAfterExit=yes" \
        "ExecStart=/usr/sbin/nft -f $FIREWALL_RULES" "" "[Install]" "WantedBy=multi-user.target"
}

# Root, because the owner gave Jarvis its whole machine; it answers the user jarvis only, keeps keys and the vault out
# of reach, and keeps a copy of what every change replaced (/var/lib/jarvis-fs) so each can be undone.
fs_unit() {
    printf '%s\n' "# Written by the Jarvis installer; changed by hand, it is overwritten at the next update." \
        "[Unit]" "Description=Jarvis brain: Jarvis's hands on this machine's files (jarvis-fsd)" "Before=jarvis.service" "" \
        "[Service]" "Type=simple" "ExecStart=/usr/bin/python3 -I $FSD --user jarvis --protect $ENV_DIR --protect $KEYS" \
        "Restart=on-failure" "RestartSec=3" "UMask=0077" "LimitCORE=0" "" \
        "[Install]" "WantedBy=multi-user.target"
}

service_unit() {
    printf '%s\n' "# Written by the Jarvis installer; changed by hand, it is overwritten at the next update." \
        "[Unit]" "Description=Jarvis" "After=network-online.target ollama.service jarvis-firewall.service" \
        "Wants=network-online.target" "Requires=jarvis-firewall.service" \
        "# Jarvis reads and changes this machine's files through jarvis-fsd, as the owner chose." \
        "Wants=jarvis-fs.service" "After=jarvis-fs.service" \
        "# A Jarvis that cannot start is not started over for ever." \
        "StartLimitIntervalSec=120" "StartLimitBurst=5" "" \
        "[Service]" "Type=simple" "User=jarvis" "Group=jarvis" "WorkingDirectory=/" \
        "# Not started at all while the installed release is one from before the service existed." \
        "ExecCondition=/usr/bin/test -f $JARVIS_CHECKOUT/src/jarvis/server.py" \
        "ExecStart=$VENVS/current/bin/python -I -m jarvis serve" \
        "Restart=on-failure" "RestartSec=3" "TimeoutStopSec=15" \
        "# The service itself writes its audit log and its notes and nothing else; other files it changes only" \
        "# through jarvis-fsd (jarvis-fs.service), which keeps keys, the vault and its own guards out of reach." \
        "# It holds the vault's values in memory: a crash never writes that memory to the disk." \
        "LimitCORE=0" \
        "NoNewPrivileges=true" "ProtectSystem=strict" "ProtectHome=read-only" "PrivateTmp=true" \
        "ProtectControlGroups=true" "LockPersonality=true" \
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK" \
        "ReadWritePaths=$STATE/audit $STATE/notes" "" "[Install]" "WantedBy=multi-user.target"
}

# Prints what is wrong with the certificates, one line each, for the address given; nothing when all is well.
tls_faults() {
    local address="$1" file
    for file in ca.pem server.pem server.key; do
        [ -s "$TLS/$file" ] || { echo "$TLS/$file is missing"; return 0; }
    done
    openssl verify -CAfile "$TLS/ca.pem" "$TLS/server.pem" >/dev/null 2>&1 \
        || echo "the service's certificate is not signed by this brain's own authority, or is out of date"
    [ "$(openssl x509 -in "$TLS/server.pem" -noout -ext subjectAltName 2>/dev/null | sed -n 's/^ *IP Address://p')" = "$address" ] \
        || echo "the service's certificate is not for this machine's address ($address)"
    [ "$(openssl x509 -in "$TLS/server.pem" -noout -pubkey 2>/dev/null)" = "$(openssl pkey -in "$TLS/server.key" -pubout 2>/dev/null)" ] \
        || echo "the service's key does not belong to its certificate"
    [ "$(stat -c '%U:%G %a' "$TLS/server.key")" = "root:jarvis 640" ] || echo "$TLS/server.key must belong to root, group jarvis, mode 0640"
    [ ! -e "$TLS/ca.key" ] || [ "$(stat -c '%U:%G %a' "$TLS/ca.key")" = "root:root 600" ] || echo "$TLS/ca.key must belong to root alone, mode 0600"
    door_faults
}

# The brain's key for the door to the browser on its desktop: a client certificate from this brain's own
# authority, for client use only. It never leaves the brain; the desktop's door takes it because the desktop
# trusts this authority already (for the panel). Prints what is wrong, one line each.
door_faults() {
    local file
    for file in door-client.pem door-client.key; do
        [ -s "$TLS/$file" ] || { echo "$TLS/$file is missing"; return 0; }
    done
    openssl verify -CAfile "$TLS/ca.pem" -purpose sslclient "$TLS/door-client.pem" >/dev/null 2>&1 \
        || echo "the door certificate is not signed by this brain's own authority for client use, or is out of date"
    [ "$(openssl x509 -in "$TLS/door-client.pem" -noout -pubkey 2>/dev/null)" = "$(openssl pkey -in "$TLS/door-client.key" -pubout 2>/dev/null)" ] \
        || echo "the door key does not belong to its certificate"
    [ "$(stat -c '%U:%G %a' "$TLS/door-client.key")" = "root:jarvis 640" ] || echo "$TLS/door-client.key must belong to root, group jarvis, mode 0640"
}

new_key_and() {  # new_key_and NAME DAYS SUBJECT [openssl req options]: a new key NAME.key.new and certificate NAME.pem.new
    local name="$1" days="$2" subject="$3"
    shift 3
    rm -f "${TLS:?}/${name:?}.key.new" "${TLS:?}/${name:?}.pem.new"
    if ! (umask 077 && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days "$days" -subj "$subject" \
        -keyout "$TLS/$name.key.new" -out "$TLS/$name.pem.new" "$@" >/dev/null 2>&1); then
        rm -f "${TLS:?}/${name:?}.key.new" "${TLS:?}/${name:?}.pem.new"
        die "the certificate $name could not be made (openssl failed). Nothing was changed."
    fi
}

apply_tls() {
    local address="$1" new_authority=0
    install -d -m 0750 -o root -g jarvis "$TLS"
    # The authority is made once. A new one means every desktop has to be told again, so it is replaced only
    # when it is missing or about to run out.
    if [ ! -s "$TLS/ca.pem" ] || [ ! -s "$TLS/ca.key" ] \
        || ! openssl x509 -in "$TLS/ca.pem" -noout -checkend $((90 * 86400)) >/dev/null 2>&1 \
        || [ "$(openssl x509 -in "$TLS/ca.pem" -noout -pubkey 2>/dev/null)" != "$(openssl pkey -in "$TLS/ca.key" -pubout 2>/dev/null)" ]; then
        [ ! -s "$TLS/ca.pem" ] || new_authority=1
        new_key_and ca 3650 "/CN=Jarvis brain authority ($(hostname))" \
            -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign"
        mv -f "$TLS/ca.key.new" "$TLS/ca.key"
        mv -f "$TLS/ca.pem.new" "$TLS/ca.pem"
        rm -f "${TLS:?}/server.pem" "${TLS:?}/server.key" "${TLS:?}/door-client.pem" "${TLS:?}/door-client.key"
    fi
    chown root:root "$TLS/ca.key" "$TLS/ca.pem"
    chmod 0600 "$TLS/ca.key"
    chmod 0644 "$TLS/ca.pem"
    # The service's own certificate names this address and nothing else. It is made anew when the address
    # changed, and in good time before it runs out: every update looks.
    if [ -n "$(tls_faults "$address" 2>/dev/null | grep -v 'must belong to' || true)" ] \
        || ! openssl x509 -in "$TLS/server.pem" -noout -checkend $((60 * 86400)) >/dev/null 2>&1; then
        new_key_and server 825 "/CN=Jarvis brain" -CA "$TLS/ca.pem" -CAkey "$TLS/ca.key" \
            -addext "basicConstraints=critical,CA:FALSE" -addext "keyUsage=critical,digitalSignature" \
            -addext "extendedKeyUsage=serverAuth" -addext "subjectAltName=IP:$address"
        chown root:jarvis "$TLS/server.key.new"
        chmod 0640 "$TLS/server.key.new"
        mv -f "$TLS/server.key.new" "$TLS/server.key"
        mv -f "$TLS/server.pem.new" "$TLS/server.pem"
    fi
    chown root:jarvis "$TLS/server.key"
    chmod 0640 "$TLS/server.key"
    chown root:root "$TLS/server.pem"
    chmod 0644 "$TLS/server.pem"
    if [ -n "$(door_faults 2>/dev/null | grep -v 'must belong to' || true)" ] \
        || ! openssl x509 -in "$TLS/door-client.pem" -noout -checkend $((60 * 86400)) >/dev/null 2>&1; then
        new_key_and door-client 825 "/CN=Jarvis brain door" -CA "$TLS/ca.pem" -CAkey "$TLS/ca.key" \
            -addext "basicConstraints=critical,CA:FALSE" -addext "keyUsage=critical,digitalSignature" \
            -addext "extendedKeyUsage=clientAuth"
        chown root:jarvis "$TLS/door-client.key.new"
        chmod 0640 "$TLS/door-client.key.new"
        mv -f "$TLS/door-client.key.new" "$TLS/door-client.key"
        mv -f "$TLS/door-client.pem.new" "$TLS/door-client.pem"
    fi
    chown root:jarvis "$TLS/door-client.key"
    chmod 0640 "$TLS/door-client.key"
    chown root:root "$TLS/door-client.pem"
    chmod 0644 "$TLS/door-client.pem"
    [ "$new_authority" = "0" ] || warn "this brain has a new certificate authority. Run the desktop's installer again (with var_brain) so that the desktop trusts this brain again."
}

service_answers() {  # asked from this container itself, which may ask only this
    curl -fsS -m 5 --cacert "$TLS/ca.pem" "https://$1:$PORT/api/health" 2>/dev/null | grep -q '"ok": *true'
}

wait_for_service() {
    local tries="${JARVIS_SERVICE_WAIT:-40}"
    until service_answers "$1"; do
        tries=$((tries - 1))
        [ "$tries" -gt 0 ] || return 1
        sleep "${JARVIS_SERVICE_PAUSE:-1}"
    done
}

apply_service() {
    local address before
    address="$(own_address)"
    [[ "$address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "this container has no address on eth0, so Jarvis's service cannot be set up."
    before="$(site_get JARVIS_ADDRESS)"
    if [ "$before" != "$address" ]; then
        site_set JARVIS_ADDRESS "$address"
        [ -z "$before" ] || warn "this brain's address changed from $before to $address. Run the desktop's installer again (with var_brain) so that the desktop looks for the brain where it is now."
    fi
    apply_tls "$address"

    # The rules are tried before they replace the ones on disk: a file that does not load would leave the
    # port without its guard at the next start.
    firewall_rules > "$FIREWALL_RULES.new"
    if ! nft -c -f "$FIREWALL_RULES.new"; then
        rm -f "${FIREWALL_RULES:?}.new"
        die "the firewall rules for these settings are not valid. Nothing was changed; the rules in force stay."
    fi
    chmod 0644 "$FIREWALL_RULES.new"
    mv -f "$FIREWALL_RULES.new" "$FIREWALL_RULES"
    firewall_unit | write_file "$FIREWALL" 0644
    install_command "$JARVIS_CHECKOUT/src/jarvis/selffs/daemon.py" "$FSD"
    fs_unit | write_file "$FS_UNIT" 0644
    service_unit | write_file "$SERVICE" 0644
    systemctl daemon-reload
    systemctl enable jarvis-firewall.service >/dev/null 2>&1 \
        || die "the firewall could not be set to load at start. See: systemctl status jarvis-firewall"
    nft -f "$FIREWALL_RULES" || die "the firewall rules could not be loaded."
    systemctl enable jarvis-fs.service >/dev/null 2>&1 || die "jarvis-fsd could not be set to start at boot. See: systemctl status jarvis-fs"
    systemctl restart jarvis-fs.service || die "jarvis-fsd does not start. See: journalctl -u jarvis-fs"
    systemctl enable jarvis.service >/dev/null 2>&1 || die "Jarvis could not be set to start at boot. See: systemctl status jarvis"
    # Always started anew: the code it runs has just been put in place. A service that does not come up is
    # switched off again, so that a container which goes back a release is not left with it trying.
    systemctl reset-failed jarvis.service >/dev/null 2>&1 || true   # an earlier run of failed starts is not held against this one
    if ! systemctl restart jarvis.service; then
        systemctl disable --now jarvis.service >/dev/null 2>&1 || true
        die "Jarvis's service does not start. See: journalctl -u jarvis"
    fi
    if ! wait_for_service "$address"; then
        systemctl disable --now jarvis.service >/dev/null 2>&1 || true
        die "Jarvis's service does not answer on https://$address:$PORT. See: journalctl -u jarvis"
    fi
    if [ -n "$ALLOW" ]; then
        ok "Jarvis answers on https://$address:$PORT, for $ALLOW and nobody else"
    else
        ok "Jarvis answers on https://$address:$PORT"
        warn "nobody may reach the panel yet: name the desktop container's address (var_panel_allow) and run the installer again."
    fi
}

apply() {
    say "Packages"
    apt_install ca-certificates curl git openssh-client tzdata python3 python3-venv nftables openssl

    say "The jarvis user and its folders"
    ensure_user jarvis
    # Root owns the folder and jarvis owns what is in it, each part made here. So jarvis cannot put a link
    # where root will next create or change something.
    install -d -m 0750 -o root -g jarvis "$STATE"
    [ ! -L "$STATE/audit" ] && { [ ! -e "$STATE/audit" ] || [ -d "$STATE/audit" ]; } \
        || die "$STATE/audit is not a folder (a link or a file is in its place). It was left as it is; look at it and move it away."
    install -d -m 0700 -o jarvis -g jarvis "$STATE/audit"
    [ ! -L "$STATE/notes" ] && { [ ! -e "$STATE/notes" ] || [ -d "$STATE/notes" ]; } \
        || die "$STATE/notes is not a folder (a link or a file is in its place). It was left as it is; look at it and move it away."
    install -d -m 0700 -o jarvis -g jarvis "$STATE/notes"
    [ -n "$(site_get JARVIS_HOST_DESCRIPTION)" ] || site_set JARVIS_HOST_DESCRIPTION "$(describe_host)"
    # Jarvis reads its .env files and never writes them; only root can change what is in here.
    env_dir_ok "$ENV_DIR" || die "$ENV_DIR cannot be the folder for the .env files (JARVIS_ENV_DIR in $JARVIS_SITE)."
    [ "$(readlink -m "$ENV_DIR")" = "$ENV_DIR" ] || die "$ENV_DIR leads elsewhere through a link; name the real folder."
    # A folder that is already there is taken only if it is plainly this one from an earlier run, or empty.
    if [ -e "$ENV_DIR" ] && [ "$(stat -c '%F %U:%G %a' "$ENV_DIR")" != "directory root:jarvis 750" ]; then
        if [ "$(stat -c '%F %u' "$ENV_DIR")" != "directory 0" ] || [ -n "$(ls -A "$ENV_DIR")" ]; then
            die "$ENV_DIR exists and is not an empty folder owned by root; it was left as it is. Name another folder, or empty this one."
        fi
    fi
    install -d -m 0750 -o root -g jarvis "$ENV_DIR"
    if [ "$SECRETS_MODE" = "sops" ]; then
        printf '%s\n' "Jarvis reads its settings from the *.enc.env files in this folder (sops with age)." \
            "They are decrypted in memory only; nothing readable is written to this disk." > "$ENV_DIR/README"
    else
        printf '%s\n' "Jarvis reads its settings from the *.env files in this folder." \
            "Keep them owned by root, group jarvis, mode 0640." > "$ENV_DIR/README"
    fi
    chmod 0644 "$ENV_DIR/README"
    # The files copied in are made readable for Jarvis and nobody else, whoever copied them how.
    find "$ENV_DIR" -maxdepth 1 -type f -name '*.env' -exec chown root:jarvis {} + -exec chmod 0640 {} +

    say "The vault"
    install -d -m 0755 -o root -g root "$TRUST"
    # GitHub's published host keys, for the notes repository: Jarvis talks to GitHub only when they match.
    install -m 0644 -o root -g root "$JARVIS_CHECKOUT/trust/github_known_hosts" "$TRUST/github_known_hosts"
    if [ "$SECRETS_MODE" = "sops" ]; then
        apt_install age
        install_sops
        install -d -m 0700 -o root -g root "$KEYS"
        if [ ! -s "$KEYS/identity.age" ]; then
            warn "this brain has no vault identity yet. As root on the brain (pct enter), run: jarvis-unlock --setup"
        else
            ok "sops $SOPS_VERSION; vault identity $(cat "$KEYS/recipient" 2>/dev/null)"
        fi
    else
        ok "secrets are plain files in $ENV_DIR"
    fi

    say "Commands"
    install_command "$JARVIS_CHECKOUT/bin/update" /usr/bin/update
    install_command "$JARVIS_CHECKOUT/bin/jarvis" /usr/bin/jarvis
    install_command "$JARVIS_CHECKOUT/bin/jarvis-unlock" /usr/sbin/jarvis-unlock

    say "The local model"
    if [ "$MODEL" = "none" ]; then
        ok "no local model was asked for (JARVIS_LOCAL_MODEL=none)"
    else
        apt_install zstd mesa-vulkan-drivers
        apply_model
    fi

    say "Jarvis itself"
    apply_python
    ok "Jarvis's Python environment: $(readlink "$VENVS/current")"

    say "Jarvis's service and who may reach it"
    apply_service
}

check() {
    local wrong=() fault report address before
    id jarvis >/dev/null 2>&1 || wrong+=("the jarvis user is missing")
    [ -d "$STATE" ] || wrong+=("$STATE is missing")
    [ -d "$ENV_DIR" ] || wrong+=("the .env folder $ENV_DIR is missing")
    if [ -d "$ENV_DIR" ] && [ "$(stat -c '%U:%G %a' "$ENV_DIR")" != "root:jarvis 750" ]; then
        wrong+=("$ENV_DIR must belong to root, group jarvis, mode 0750")
    fi
    cmp -s "$JARVIS_CHECKOUT/bin/update" /usr/bin/update || wrong+=("/usr/bin/update is not the one of this release")
    cmp -s "$JARVIS_CHECKOUT/bin/jarvis" /usr/bin/jarvis || wrong+=("/usr/bin/jarvis is not the one of this release")
    cmp -s "$JARVIS_CHECKOUT/bin/jarvis-unlock" /usr/sbin/jarvis-unlock || wrong+=("/usr/sbin/jarvis-unlock is not the one of this release")
    if [ "$SECRETS_MODE" = "sops" ]; then
        sops_ok || wrong+=("$SOPS is not sops $SOPS_VERSION as this release records it")
        [ "$(stat -c '%U:%G %a' "$KEYS" 2>/dev/null)" = "root:root 700" ] || wrong+=("$KEYS must belong to root alone, mode 0700")
        command -v age >/dev/null || wrong+=("age is not installed")
    fi
    while IFS= read -r fault; do
        [ -z "$fault" ] || wrong+=("$fault must belong to root, group jarvis, mode 0640")
    done <<<"$(find "$ENV_DIR" -maxdepth 1 -type f -name '*.env' ! \( -user root -group jarvis -perm 0640 \) 2>/dev/null)"
    [ "$(stat -c '%U:%G %a' "$STATE" 2>/dev/null)" = "root:jarvis 750" ] || wrong+=("$STATE must belong to root, group jarvis, mode 0750")
    if [ -L "$STATE/audit" ] || [ ! -d "$STATE/audit" ] || [ "$(stat -c '%U:%G %a' "$STATE/audit")" != "jarvis:jarvis 700" ]; then
        wrong+=("$STATE/audit must be a folder of the user jarvis, mode 0700")
    fi
    if [ -L "$STATE/notes" ] || [ ! -d "$STATE/notes" ] || [ "$(stat -c '%U:%G %a' "$STATE/notes")" != "jarvis:jarvis 700" ]; then
        wrong+=("$STATE/notes must be a folder of the user jarvis, mode 0700")
    fi
    if ! cmp -s "$JARVIS_CHECKOUT/trust/github_known_hosts" "$TRUST/github_known_hosts"; then
        wrong+=("$TRUST/github_known_hosts is not GitHub's host keys as this release records them")
    fi
    # Looked at before anything in it is run.
    if [ -n "$(find "$VENVS" ! -user root -print -quit 2>/dev/null)" ]; then
        wrong+=("something under $VENVS does not belong to root")
    elif [ "$(readlink "$VENVS/current" 2>/dev/null)" != "$(venv_name)" ] || ! venv_has_packages "$VENVS/current"; then
        wrong+=("Jarvis's Python environment is not the one this release names, or its packages do not load")
    elif ! jarvis_loads "$VENVS/current"; then
        wrong+=("this release's own code does not load in Jarvis's Python environment")
    fi
    if [ "$MODEL" != "none" ]; then
        if [ "$(cat "$OLLAMA_MARK" 2>/dev/null)" != "$OLLAMA_VERSION $OLLAMA_SHA256" ] || [ ! -x /usr/local/bin/ollama ]; then
            wrong+=("Ollama is not the release this version of Jarvis names ($OLLAMA_VERSION)")
        fi
        ollama_unit | cmp -s - "$OLLAMA_UNIT" || wrong+=("$OLLAMA_UNIT is not the one of this release")
        [ "$(systemctl is-enabled ollama 2>/dev/null)" = "enabled" ] || wrong+=("Ollama is not set to start at boot")
        if [ "${#wrong[@]}" -eq 0 ]; then
            while IFS= read -r fault; do
                [ -z "$fault" ] || wrong+=("$fault")
            done <<<"$(model_faults)"
        fi
    fi
    address="$(own_address)"
    while IFS= read -r fault; do
        [ -z "$fault" ] || wrong+=("$fault")
    done <<<"$(tls_faults "$address")"
    openssl x509 -in "$TLS/server.pem" -noout -checkend $((14 * 86400)) >/dev/null 2>&1 \
        || wrong+=("the service's certificate runs out within two weeks; update --repair makes a new one")
    [ "$(site_get JARVIS_ADDRESS)" = "$address" ] || wrong+=("JARVIS_ADDRESS in $JARVIS_SITE is not this container's address ($address)")
    nft list table inet jarvis_brain >/dev/null 2>&1 || wrong+=("the firewall table jarvis_brain is not loaded")
    firewall_rules | cmp -s - "$FIREWALL_RULES" || wrong+=("$FIREWALL_RULES is not what the settings ask for")
    firewall_unit | cmp -s - "$FIREWALL" || wrong+=("$FIREWALL is not the one of this release")
    service_unit | cmp -s - "$SERVICE" || wrong+=("$SERVICE is not the one of this release")
    cmp -s "$JARVIS_CHECKOUT/src/jarvis/selffs/daemon.py" "$FSD" || wrong+=("$FSD is not the one of this release")
    fs_unit | cmp -s - "$FS_UNIT" || wrong+=("$FS_UNIT is not the one of this release")
    [ "$(systemctl is-enabled jarvis-fs.service 2>/dev/null)" = "enabled" ] || wrong+=("jarvis-fsd is not set to start at boot")
    systemctl is-active --quiet jarvis-fs.service || wrong+=("jarvis-fsd is not running (see: journalctl -u jarvis-fs)")
    [ "$(systemctl is-enabled jarvis-firewall.service 2>/dev/null)" = "enabled" ] || wrong+=("the firewall is not set to load at start")
    [ "$(systemctl is-enabled jarvis.service 2>/dev/null)" = "enabled" ] || wrong+=("Jarvis is not set to start at boot")
    if [ "${#wrong[@]}" -eq 0 ]; then
        if ! systemctl is-active --quiet jarvis.service; then
            wrong+=("Jarvis's service is not running (see: journalctl -u jarvis)")
        elif ! wait_for_service "$address"; then
            wrong+=("Jarvis's service does not answer on https://$address:$PORT (see: journalctl -u jarvis)")
        fi
    fi
    # Last, through the whole of Jarvis: its settings, its audit log, its tools, and one question to the model.
    # It runs as the user jarvis, away from this terminal. What is wrong with the log Jarvis keeps is said
    # but does not count against the release: an update must not hang on a file Jarvis itself writes.
    if [ "${#wrong[@]}" -eq 0 ]; then
        if report="$(setsid -w jarvis doctor --release-check </dev/null 2>&1)"; then
            printf '%s\n' "$report" >&2
        else
            while IFS= read -r fault; do
                [[ "$fault" != *FAULT:* ]] || wrong+=("${fault#*FAULT: }")
            done <<<"$report"
            [ "${#wrong[@]}" -gt 0 ] || wrong+=("jarvis doctor failed: $(tail -n 1 <<<"$report")")
        fi
    fi
    if [ "${#wrong[@]}" -gt 0 ]; then
        printf 'jarvis: not as it should be: %s\n' "${wrong[@]}" >&2
        # This is the check that ends an install, and the release gone back to has no service of its own (or
        # there is none to go back to): nothing would stop or restart this release's service, so it is
        # switched off here. A release with a service starts its own again when it is put back.
        if [ -e "$JARVIS_ETC/release.pending" ]; then
            before="$(sed -n 's/^commit=//p' "$JARVIS_ETC/release" 2>/dev/null | head -n 1)"
            if ! [[ "$before" =~ ^[0-9a-f]{40}$ ]] || ! git -C "$JARVIS_CHECKOUT" cat-file -e "$before:src/jarvis/server.py" 2>/dev/null; then
                systemctl disable --now jarvis.service >/dev/null 2>&1 || true
                printf 'jarvis: %s\n' "Jarvis's service was switched off: this release did not pass its check, and the one before has no service." >&2
            fi
        fi
        return 1
    fi
    ok "Jarvis brain: as it should be"
}

case "${1:-}" in
    "") apply ;;
    --check) check ;;
    *) die "unknown option '$1'. Use no option, or --check." ;;
esac
