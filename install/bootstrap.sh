#!/usr/bin/env bash
# First run inside a new Jarvis container, started by the installer on the Proxmox node. It takes the
# settings the installer handed over, fetches the code, checks the release and runs the install script of
# that release. Running it again is safe.
set -euo pipefail
here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source=misc/install.func
. "$here/install.func"
# shellcheck source=misc/release.func
. "$here/release.func"

need_root
exec 9>/run/jarvis-update.lock
flock -n 9 || die "an update is running in this container. Try again when it has finished."
site_merge "$JARVIS_ETC/site.env.install"
script="install/$(script_for_role "$(site_get JARVIS_ROLE)")"
url="$(site_get JARVIS_REPO_URL)"
[ -n "$url" ] || die "$JARVIS_SITE names no repository (JARVIS_REPO_URL)."
JARVIS_RELEASE_CHANNEL="$(site_get JARVIS_RELEASE_CHANNEL)"
JARVIS_RELEASE_BRANCH="$(site_get JARVIS_RELEASE_BRANCH)"
export JARVIS_RELEASE_CHANNEL JARVIS_RELEASE_BRANCH

say "Fetching Jarvis"
apt_install ca-certificates git openssh-client
release_clone "$url"
release_check_record
if [ "$(release_state)" = "changed" ]; then
    die "the code in $JARVIS_CHECKOUT was changed by hand since it was installed. Nothing was changed. To put those changes aside and go on, run in the container: update --discard"
fi
release_fetch
release_resolve
release_check_floor
ok "$RELEASE_NAME from $url"
release_install "$RELEASE_NAME" "$RELEASE_COMMIT" "$script" \
    || die "Jarvis $RELEASE_NAME did not install completely. Run the installer on the Proxmox node again to carry on."
say "Jarvis $RELEASE_NAME is installed"
