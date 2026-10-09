#!/usr/bin/env bash
# Runs one command on a private copy of the system folders the installers write to, so a test can play both
# the Proxmox node and the container without touching this machine. Started by tests/conftest.py under
# `unshare -m`. The copy lives in $1 and is found again by the next call with the same folder.
set -euo pipefail
work="$1"; shift
for folder in etc usr/local usr/bin usr/sbin opt root var/lib home srv run; do
    mkdir -p "$work/overlay/$folder/upper" "$work/overlay/$folder/work"
    mount -t overlay overlay -o "lowerdir=/$folder,upperdir=$work/overlay/$folder/upper,workdir=$work/overlay/$folder/work" "/$folder"
done
# The pretend container has no render group until a test makes one, whatever this machine has.
if [ ! -e "$work/overlay/prepared" ]; then
    sed -i '/^render:/d' /etc/group
    [ ! -f /etc/gshadow ] || sed -i '/^render:/d' /etc/gshadow
    : > "$work/overlay/prepared"
fi
here="$(dirname "$(readlink -f "$0")")"
install -d /usr/local/sbin
for tool in ip apt-get curl docker nft systemctl dpkg-query; do  # put in place once; a test may then replace one
    [ -e "/usr/local/sbin/$tool" ] || install -m 0755 "$here/$tool" /usr/local/sbin/
done
exec "$@"
