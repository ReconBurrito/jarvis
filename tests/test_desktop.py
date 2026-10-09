"""The desktop container's install: firewall, Docker file, files for the desktop, and the checks on it.

Docker, systemctl and the loading of rules are stand-ins here (tests/stubs). The firewall rules themselves
are loaded into the real nft in test_firewall.py, the sandbox check runs against a real browser in
test_sandbox_check.py, and the session script runs on a real window manager in test_session.py.
"""
import json
import re
import subprocess

import pytest

from conftest import REPO

ALLOW = "192.0.2.7,198.51.100.0/24"
ORIGIN = "https://desktop.example.org"
IMAGE = (REPO / "desktop" / "image").read_text().strip()
CHECK = "bash /opt/jarvis/install/jarvis-desktop-install.sh --check"


def typed(*lines):
    return "".join(line + "\n" for line in lines)


def desktop(bench, **settings):
    values = {"desktop_allow": ALLOW, "desktop_origin": ORIGIN}
    values.update(settings)
    return bench.install("desktop", bench.answers("desktop", **values))


def update(bench, *options):
    return bench.sh("update " + " ".join(options), env=bench.env())


def state(bench, name):
    path = bench.state / name
    return path.read_text() if path.exists() else ""


def test_the_image_is_named_by_its_digest():
    assert re.fullmatch(r"lscr\.io/linuxserver/webtop@sha256:[0-9a-f]{64}", IMAGE)


def test_the_browser_policy_names_only_what_the_desktop_brings():
    import json
    policy = json.loads((REPO / "desktop" / "policy.json").read_text())
    assert policy["PasswordManagerEnabled"] is False and policy["SyncDisabled"] is True
    page = policy["NewTabPageLocation"]
    assert page.startswith("file:///opt/jarvis-desktop/") and (REPO / "desktop" / page.rsplit("/", 1)[1]).is_file()
    session = (REPO / "desktop" / "jarvis-session.sh").read_text()
    assert f'BROWSER_URL="${{JARVIS_BROWSER_URL:-{page}}}"' in session


def test_install_sets_up_firewall_files_and_desktop(bench):
    bench.release("v0.1.0")
    done = desktop(bench)
    assert done.returncode == 0, done.stdout
    for line in ("port 3001 is open to " + ALLOW + " and to nobody else", "sandbox layer 1, own namespaces: on",
                 "sandbox layer 2, own system-call filter: on", "Jarvis desktop: as it should be"):
        assert line in done.stdout, line

    site = bench.read("/etc/jarvis/site.env")
    assert f"JARVIS_DESKTOP_ALLOW={ALLOW}" in site and f"JARVIS_DESKTOP_ORIGIN={ORIGIN}" in site

    rules = bench.read("/etc/jarvis/desktop.nft")
    assert "type filter hook input priority filter; policy drop;" in rules
    assert "        ip saddr { 192.0.2.7, 198.51.100.0/24 } tcp dport 3001 counter accept\n" in rules
    assert "        meta skuid 1000 fib daddr type local tcp dport { 3000, 3001, 8082 } counter reject with tcp reset\n" in rules
    assert rules.count(" accept") == 7   # lo, established, icmp, DHCP, DHCPv6, the named addresses, and the output policy
    assert state(bench, "nft-loaded") == rules and bench.sh("ls /etc/jarvis/desktop.nft.new").returncode != 0
    assert "jarvis-desktop-firewall.service" in state(bench, "enabled") and "docker" in state(bench, "enabled")
    unit = bench.read("/etc/systemd/system/jarvis-desktop-firewall.service")
    for line in ("ExecStart=/usr/sbin/nft -f /etc/jarvis/desktop.nft", "WantedBy=multi-user.target", "Wants=network-pre.target",
                 "Before=network-pre.target docker.service shutdown.target"):
        assert line + "\n" in unit, line
    needs = bench.read("/etc/systemd/system/docker.service.d/jarvis-desktop-firewall.conf")
    assert "Requires=jarvis-desktop-firewall.service\nAfter=jarvis-desktop-firewall.service\n" in needs

    compose = bench.read("/opt/jarvis-desktop/compose.yaml")
    for line in (f"    image: {IMAGE}", "    container_name: jarvis-desktop", "    network_mode: host",
                 "      - seccomp=unconfined", "    restart: unless-stopped", '      START_DOCKER: "false"',
                 '      SELKIES_COMMAND_ENABLED: "false"', '      DISABLE_SUDO: "true"',
                 f'      SELKIES_ALLOWED_ORIGINS: "{ORIGIN}"',
                 "      - /opt/jarvis-desktop/config:/config",
                 "      - /opt/jarvis-desktop/app:/opt/jarvis-desktop:ro",
                 "      - /opt/jarvis-desktop/app/chromium:/usr/bin/chromium:ro",
                 "      - /opt/jarvis-desktop/app/chromium:/usr/local/bin/wrapped-chromium:ro",
                 "      - /opt/jarvis-desktop/app/bwrap:/usr/bin/bwrap:ro",
                 "      - /opt/jarvis-desktop/app/jarvis-session.desktop:/etc/xdg/autostart/jarvis-session.desktop:ro",
                 "      - /opt/jarvis-desktop/app/icon.png:/usr/share/selkies/www/icon.png:ro",
                 "      - /opt/jarvis-desktop/policies:/etc/chromium/policies/managed:ro"):
        assert line + "\n" in compose, line
    assert "devices:" not in compose and "DRI" not in compose and "ports:" not in compose
    assert "privileged" not in compose and "docker.sock" not in compose
    assert state(bench, "docker-running") == compose and state(bench, "docker-state") == "up\n"

    files = bench.sh("cd /opt/jarvis-desktop && stat -c '%a %u:%g %n' app/* policies/* config").stdout.splitlines()
    assert files == ["644 0:0 app/Oxanium-LICENSE.txt", "644 0:0 app/Oxanium.ttf", "644 0:0 app/brain.js", "755 0:0 app/bwrap",
                     "755 0:0 app/chromium", "644 0:0 app/home.html", "644 0:0 app/icon.png",
                     "644 0:0 app/jarvis-session.desktop", "755 0:0 app/jarvis-session.sh", "644 0:0 app/panel.html",
                     "755 0:0 app/sandbox-check.sh", "755 0:0 app/session-check.sh", "644 0:0 policies/jarvis.json",
                     "755 1000:1000 config"], files
    # The pages are opened from disk and must bring everything they show: nothing is fetched from anywhere.
    for page in ("panel.html", "home.html"):
        text = bench.read(f"/opt/jarvis-desktop/app/{page}")
        assert "http://" not in text.replace("http://www.w3.org/2000/svg", "") and "https://" not in text, page
        assert 'url("Oxanium.ttf")' in text and 'href="icon.png"' in text
    # Without a brain the panel's start page is told so, and stays what it is.
    assert bench.read("/opt/jarvis-desktop/app/brain.js").splitlines()[-1] == 'window.JARVIS_BRAIN = "";'
    assert json.loads(bench.read("/opt/jarvis-desktop/policies/jarvis.json")) == json.loads((REPO / "desktop" / "policy.json").read_text())
    assert "WARNING: this desktop has no brain yet" in done.stdout
    assert bench.sh("cmp /opt/jarvis-desktop/app/Oxanium.ttf /opt/jarvis/desktop/Oxanium.ttf && echo same").stdout == "same\n"
    launcher = [line for line in bench.read("/opt/jarvis-desktop/app/chromium").splitlines() if not line.startswith("#")]
    assert not any("--no-sandbox" in line for line in launcher)
    assert any(line.startswith("exec /usr/bin/chromium-browser ") for line in launcher)
    assert any(" --hide-crash-restore-bubble " in line for line in launcher), "an update is not a crash to ask about"

    # The image was fetched once and the desktop made once; a second install changes nothing.
    assert len([c for c in bench.calls("docker") if " pull " in c]) == 1 and state(bench, "docker-made") == "made\n"
    again = desktop(bench)
    assert again.returncode == 0, again.stdout
    assert len([c for c in bench.calls("docker") if " pull " in c]) == 1 and state(bench, "docker-made") == "made\n"


def test_the_docker_file_is_what_docker_will_read(bench):
    yaml = pytest.importorskip("yaml")
    bench.release("v0.1.0")
    assert desktop(bench).returncode == 0
    service = yaml.safe_load(bench.read("/opt/jarvis-desktop/compose.yaml"))["services"]["desktop"]
    assert service["image"] == IMAGE and service["network_mode"] == "host" and service["security_opt"] == ["seccomp=unconfined"]
    assert service["environment"] == {
        "PUID": "1000", "PGID": "1000", "TZ": service["environment"]["TZ"], "TITLE": "Jarvis", "START_DOCKER": "false",
        "SELKIES_COMMAND_ENABLED": "false", "DISABLE_SUDO": "true", "SELKIES_ALLOWED_ORIGINS": ORIGIN}
    assert len(service["labels"]["jarvis.files"]) == 16 and len(service["volumes"]) == 8
    assert set(service) == {"image", "container_name", "labels", "network_mode", "shm_size", "security_opt", "restart",
                            "environment", "volumes"}


def test_without_answers_nobody_may_open_the_desktop_and_no_page_from_elsewhere_may_connect(bench):
    yaml = pytest.importorskip("yaml")
    bench.release("v0.1.0")
    done = bench.install("desktop")
    assert done.returncode == 0, done.stdout
    assert "nobody may open the desktop yet" in done.stdout and "no web address is named for the desktop" in done.stdout
    rules = bench.read("/etc/jarvis/desktop.nft")
    assert "ip saddr" not in rules and "policy drop" in rules and "meta skuid 1000" in rules
    # Not the image's default, which lets a page from any address connect.
    service = yaml.safe_load(bench.read("/opt/jarvis-desktop/compose.yaml"))["services"]["desktop"]
    assert service["environment"]["SELKIES_ALLOWED_ORIGINS"] == ""

    # Named later, by running the installer again: the firewall follows, the desktop is not made anew for it.
    named = desktop(bench, desktop_origin=None)
    assert named.returncode == 0, named.stdout
    assert "ip saddr { 192.0.2.7, 198.51.100.0/24 } tcp dport 3001 counter accept" in bench.read("/etc/jarvis/desktop.nft")
    assert state(bench, "nft-loaded") == bench.read("/etc/jarvis/desktop.nft")
    assert state(bench, "docker-made") == "made\n"


def test_a_desktop_that_fails_its_checks_is_stopped_and_not_recorded(bench):
    bench.release("v0.1.0")
    for flag, value, words in (
        ("sandbox", "1", "Chromium's sandbox is NOT fully on in this container"),
        ("sandbox", "2", "Chromium's sandbox could not be checked"),
        ("no-answer", "", "the desktop does not answer on https://127.0.0.1:3001"),
        ("no-backend", "", "the desktop's web server cannot reach the desktop behind it (answer 502)"),
        ("no-tools", "", "the desktop image lacks a program the session script needs"),
        ("no-launchers", "", "the image's Chromium launchers are not replaced by ours"),
        ("no-bwrap", "", "the image's bwrap is not replaced by ours: the desktop's programs cannot load their pictures"),
        ("session", "1", "the desktop's session ended within 30 seconds of its start (sessions started since the desktop came up: 4): it does not stay up"),
        ("session", "2", "whether the desktop's session stays up could not be checked: session check: how long"),
        ("other-uid", "", "the desktop's user is not user 1000"),
        ("pages-reach", "127.0.0.1/8082", "can connect to the desktop's own port 8082 (127.0.0.1): a web page could drive the desktop"),
        ("pages-reach", "::1/3001", "can connect to the desktop's own port 3001 (::1)"),
        ("menu-no-sandbox", "", "a menu or autostart entry of the desktop starts a program with --no-sandbox"),
        ("running-no-sandbox", "", "a program in the desktop is running with --no-sandbox"),
    ):
        (bench.state / flag).write_text(value)
        refused = desktop(bench)
        assert refused.returncode == 1 and words in refused.stdout, (flag, value, refused.stdout)
        assert "the desktop was stopped because of the above" in refused.stdout
        assert state(bench, "docker-state") == "stopped\n" and bench.version() == "", flag
        (bench.state / flag).unlink()
    assert desktop(bench).returncode == 0 and bench.version() == "v0.1.0" and state(bench, "docker-state") == "up\n"


def test_the_desktop_behind_the_web_server_is_given_time_to_start(bench):
    """The web server answers at once; the desktop behind it starts last and some seconds later."""
    bench.release("v0.1.0")
    (bench.state / "slow-backend").write_text("2")
    done = desktop(bench)
    assert done.returncode == 0, done.stdout
    assert state(bench, "slow-backend") == "0\n" and state(bench, "docker-state") == "up\n"


def test_failures_before_the_desktop_starts_say_what_failed(bench):
    bench.release("v0.1.0")
    for flag, words in (("fail-pull", "the desktop image could not be fetched"), ("fail-up", "the desktop did not start"),
                        ("no-docker", "Docker does not start in this container"),
                        ("no-firewall-unit", "the firewall could not be set to load at start"),
                        ("fail-nft", "the firewall rules could not be loaded")):
        (bench.state / flag).write_text("")
        refused = desktop(bench)
        assert refused.returncode == 1 and words in refused.stdout, (flag, refused.stdout)
        (bench.state / flag).unlink()
        assert bench.version() == ""
    assert desktop(bench).returncode == 0


def test_rules_that_do_not_load_never_replace_the_ones_on_disk(bench):
    """Otherwise the next start of the container would load nothing and leave the desktop open."""
    if subprocess.run(["unshare", "-n", "/usr/sbin/nft", "list", "tables"], capture_output=True).returncode != 0:
        pytest.skip("needs the real nft to judge the rules")
    bench.release("v0.1.0")
    assert desktop(bench).returncode == 0
    good = bench.read("/etc/jarvis/desktop.nft")
    # A rule the real nft refuses, slipped past the patterns by a release that widened them.
    script = (REPO / "install" / "jarvis-desktop-install.sh").read_text()
    widened = script.replace('[ -z "$ALLOW" ] || address_list_ok "$ALLOW" \\\n', 'true \\\n')
    assert widened != script
    bench.release("v0.2.0", files={"install/jarvis-desktop-install.sh": widened})
    bench.sh("sed -i 's/^JARVIS_DESKTOP_ALLOW=.*/JARVIS_DESKTOP_ALLOW=192.0.2.300/' /etc/jarvis/site.env")
    failed = update(bench)
    assert failed.returncode == 1 and "the firewall rules for these settings are not valid" in failed.stdout, failed.stdout
    assert bench.read("/etc/jarvis/desktop.nft") == good and state(bench, "nft-loaded") == good
    assert bench.sh("ls /etc/jarvis/desktop.nft.new").returncode != 0


def test_a_release_that_changes_the_desktops_files_makes_it_anew(bench):
    bench.release("v0.1.0")
    assert desktop(bench).returncode == 0
    before = state(bench, "docker-running")
    bench.release("v0.2.0", files={"README.md": "other words\n"})             # nothing the desktop is made from
    assert update(bench).returncode == 0 and state(bench, "docker-made") == "made\n"
    bench.release("v0.3.0", files={"desktop/panel.html": "<!doctype html><title>Jarvis</title>new\n"})
    moved = update(bench)
    assert moved.returncode == 0 and "Jarvis is now at v0.3.0" in moved.stdout, moved.stdout
    assert state(bench, "docker-made") == "made\nmade\n" and state(bench, "docker-running") != before
    assert bench.read("/opt/jarvis-desktop/app/panel.html").endswith("new\n")


def test_a_release_with_a_broken_desktop_is_undone(bench):
    bench.release("v0.1.0")
    assert desktop(bench).returncode == 0
    good = state(bench, "docker-running")
    bench.release("v0.2.0", files={"desktop/image": "lscr.io/linuxserver/webtop:latest\n"})  # not pinned
    failed = update(bench)
    assert failed.returncode == 1, failed.stdout
    assert "desktop/image does not name an image by its digest" in failed.stdout
    assert "Jarvis is back at v0.1.0 and passes its check." in failed.stdout
    assert state(bench, "docker-running") == good and state(bench, "docker-state") == "up\n"


def test_drift_is_noticed_and_repaired(bench):
    bench.release("v0.1.0")
    assert desktop(bench).returncode == 0
    assert bench.sh(CHECK, env=bench.env()).returncode == 0
    for damage, words in (
        ("echo '# open to all' >> /etc/jarvis/desktop.nft", "/etc/jarvis/desktop.nft is not what the settings ask for"),
        (f"rm {bench.state}/nft-loaded", "the firewall table jarvis_desktop is not loaded"),
        (f"echo stopped > {bench.state}/docker-state", "the desktop is not running from this release's image and files"),
        (f"sed -i 's/jarvis.files: \"/jarvis.files: \"0/' {bench.state}/docker-running", "is not running from this release"),
        (f": > {bench.state}/enabled", "the firewall is not set to load at start"),
        ("rm /etc/systemd/system/docker.service.d/jarvis-desktop-firewall.conf", "Docker is not tied to the firewall"),
        ("sed -i '/Before=/d' /etc/systemd/system/jarvis-desktop-firewall.service", "jarvis-desktop-firewall.service is not the one of this release"),
        ("sed -i '/wrapped-chromium/d' /opt/jarvis-desktop/compose.yaml", "compose.yaml is not what this release and the settings ask for"),
        ("echo '--no-sandbox' >> /opt/jarvis-desktop/app/chromium", "/opt/jarvis-desktop/app/chromium is not the one of this release"),
        ("echo '{}' > /opt/jarvis-desktop/policies/jarvis.json", "policies/jarvis.json is not the one of this release and its settings"),
    ):
        assert bench.sh(damage).returncode == 0
        broken = bench.sh(CHECK, env=bench.env())
        assert broken.returncode == 1 and words in broken.stdout, (damage, broken.stdout)
        repaired = update(bench, "--repair")
        assert repaired.returncode == 0 and "Jarvis v0.1.0: repaired" in repaired.stdout, (damage, repaired.stdout)


def test_the_check_alone_notices_a_sandbox_that_went_off(bench):
    """Say a later change to the container or the node's kernel switches it off: the check must say so."""
    bench.release("v0.1.0")
    assert desktop(bench).returncode == 0
    (bench.state / "sandbox").write_text("1")
    broken = bench.sh(CHECK, env=bench.env())
    assert broken.returncode == 1 and "sandbox layer 1, own namespaces: OFF" in broken.stdout
    assert "Chromium's sandbox is NOT fully on in this container" in broken.stdout
    assert state(bench, "docker-state") == "up\n"            # the check changes nothing
    repair = update(bench, "--repair")                        # the repair stops the desktop
    assert repair.returncode == 1 and "the desktop was stopped because of the above" in repair.stdout
    assert state(bench, "docker-state") == "stopped\n"


def test_the_node_refuses_bad_desktop_settings(bench):
    bench.release("v0.1.0")
    for name, value in (("desktop_allow", "192.0.2.300"), ("desktop_allow", "192.0.2.7/33"), ("desktop_allow", "192.0.2.7/0"),
                        ("desktop_allow", "192.0.2.7,"), ("desktop_allow", "192.0.2.7 198.51.100.9"),
                        ("desktop_allow", "proxy.example.org"), ("desktop_allow", "192.0.2.7;accept"),
                        ("desktop_allow", "192.0.2.7/24"), ("desktop_allow", "128.0.0.0/1"), ("desktop_allow", "192.0.02.7"),
                        ("desktop_origin", "http://desktop.example.org"), ("desktop_origin", "https://desktop.example.org/"),
                        ("desktop_origin", "https://desktop.example.org:0"), ("desktop_origin", "https://Desktop.example.org"),
                        ("desktop_origin", "https://desktop.example.org:443"), ("desktop_origin", "https://desktop.example.org:70000"),
                        ("desktop_origin", "*"), ("desktop_origin", 'https://x"\n      PASSWORD: "y')):
        refused = bench.install("desktop", bench.answers("desktop", **{name: value}))
        assert refused.returncode == 1 and "Nothing was changed" in refused.stdout, (name, value, refused.stdout)
    assert bench.calls("pct create") == []
    fine = bench.install("desktop", bench.answers("desktop", desktop_allow="192.0.2.7,198.51.100.16/28",
                                                  desktop_origin="https://desktop.example.org:8443"))
    assert fine.returncode == 0, fine.stdout


def test_the_container_applies_the_same_rules_to_its_settings_file(bench):
    bench.release("v0.1.0")
    assert desktop(bench).returncode == 0
    loaded = state(bench, "nft-loaded")
    for line in ("JARVIS_DESKTOP_ALLOW=192.0.2.7 } accept; ip saddr { 0.0.0.0/0", "JARVIS_DESKTOP_ALLOW=192.0.2.7/0",
                 "JARVIS_DESKTOP_ALLOW=192.0.02.7", "JARVIS_DESKTOP_ALLOW=192.0.2.7/24", "JARVIS_DESKTOP_ALLOW=0.0.0.0/1",
                 'JARVIS_DESKTOP_ORIGIN=https://x.example.org" # ', "JARVIS_DESKTOP_ORIGIN=*"):
        key = line.split("=")[0]
        bench.sh(f"cp /etc/jarvis/site.env /root/site.keep; sed -i '/^{key}=/d' /etc/jarvis/site.env; printf '%s\\n' '{line}' >> /etc/jarvis/site.env")
        refused = update(bench, "--repair")
        assert refused.returncode == 1 and key in refused.stdout and "must" in refused.stdout, (line, refused.stdout)
        assert state(bench, "nft-loaded") == loaded
        bench.sh("cp /root/site.keep /etc/jarvis/site.env")
    assert update(bench, "--repair").returncode == 0


def test_a_render_device_is_handed_to_the_desktop(bench):
    bench.release("v0.1.0")
    dri = bench.tmp / "dri"
    dri.mkdir()
    subprocess.run(["mknod", str(dri / "renderD129"), "c", "226", "129"], check=True)
    done = bench.install("desktop", bench.answers("desktop", desktop_allow=ALLOW), JARVIS_DRI_DIR=dri)
    assert done.returncode == 0, done.stdout
    compose = bench.read("/opt/jarvis-desktop/compose.yaml")
    node = f"{dri}/renderD129"
    # For drawing (DRINODE) and for encoding the picture (DRI_NODE): the image finds only renderD128 by itself.
    assert f"    devices:\n      - {node}:{node}\n" in compose
    assert f'      DRINODE: "{node}"\n      DRI_NODE: "{node}"\n' in compose


def test_the_questions_ask_who_may_open_the_desktop(bench):
    bench.release("v0.1.0")
    env = bench.env(var_ctid="189", var_repo="test/jarvis", var_ram="512", var_gpu="none")
    script = bench.src / "ct" / "jarvis-desktop.sh"
    done = bench.ns("bash", script, env=env, terminal_input=typed("default", "192.0.2.999", "192.0.2.7", ORIGIN, "", "y"))
    assert done.returncode == 0, done.stdout
    asked = re.findall(r"([A-Z][^\[\]\n]*) \[[^\]]*\]: ", done.stdout)
    assert [a.split(":")[0].split(" (")[0] for a in asked] == [
        "Choice", "Addresses that may open the desktop", "Addresses that may open the desktop",
        "Web address the desktop is opened at, such as https",
        "Container ID of the Jarvis brain on this node, whose panel this desktop shows", "Create the container with these settings?"], asked
    assert "Brain:       none yet (the panel stays a placeholder)" in done.stdout
    assert "That is not a valid value for: Addresses that may open the desktop" in done.stdout
    assert "Desktop:     may be opened by 192.0.2.7, at " + ORIGIN in done.stdout
    assert "Directory where Jarvis keeps its .env files" not in done.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_DESKTOP_ALLOW=192.0.2.7" in site and "JARVIS_DESKTOP_ORIGIN=" + ORIGIN in site


def test_answers_typed_on_a_second_run_are_applied(bench):
    """Named in the environment, the container is one that exists: what is typed must not be shown and dropped."""
    bench.release("v0.1.0")
    assert bench.install("desktop").returncode == 0
    assert "JARVIS_DESKTOP_ALLOW=\n" in bench.read("/etc/jarvis/site.env")
    env = bench.env(var_ctid="189", var_repo="test/jarvis", var_ram="512", var_gpu="none")
    again = bench.ns("bash", bench.src / "ct" / "jarvis-desktop.sh", env=env,
                     terminal_input=typed("default", "192.0.2.7", ORIGIN, "", "y", "y"))
    assert again.returncode == 0, again.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_DESKTOP_ALLOW=192.0.2.7\n" in site and f"JARVIS_DESKTOP_ORIGIN={ORIGIN}\n" in site
    assert "ip saddr { 192.0.2.7 } tcp dport 3001 counter accept" in bench.read("/etc/jarvis/desktop.nft")
    assert len(bench.calls("pct create")) == 1


def test_offers_merely_accepted_on_a_second_run_replace_nothing(bench):
    """Enter at a question shows no wish to change it: what the container has stays, for both kinds."""
    bench.release("v0.1.0")
    assert desktop(bench).returncode == 0
    env = bench.env(var_ctid="189", var_repo="test/jarvis", var_ram="512", var_gpu="none")
    again = bench.ns("bash", bench.src / "ct" / "jarvis-desktop.sh", env=env, terminal_input=typed("default", "", "", "", "y", "y"))
    assert again.returncode == 0, again.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert f"JARVIS_DESKTOP_ALLOW={ALLOW}\n" in site and f"JARVIS_DESKTOP_ORIGIN={ORIGIN}\n" in site


def test_the_same_for_the_brains_env_folder(bench):
    bench.release("v0.1.0")
    assert bench.install("brain", bench.answers(env_dir="/srv/keys", secrets="sops")).returncode == 0
    env = bench.env(var_ctid="189", var_repo="test/jarvis", var_ram="512", var_gpu="none")
    again = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=typed("default", "", "", "", "", "y", "y"))
    assert again.returncode == 0, again.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_ENV_DIR=/srv/keys\n" in site and "JARVIS_SECRETS_MODE=sops\n" in site


def test_the_example_answers_file_is_a_working_one(bench):
    from test_service_install import a_brain

    bench.release("v0.1.0")
    (bench.state / "address").write_text("192.0.2.201")
    a_brain(bench, ctid=200, address="192.0.2.200")   # the example names its brain, as the brain's example names this desktop
    env = bench.env(JARVIS_ANSWERS=REPO / "defaults" / "example-desktop.vars", var_repo="test/jarvis", var_ram="512", var_gpu="none")
    done = bench.ns("bash", bench.src / "ct" / "jarvis-desktop.sh", env=env)
    assert done.returncode == 0, done.stdout
    assert "pct create 201 " in bench.calls("pct create")[0]
    assert "ip saddr { 192.0.2.7 } tcp dport 3001 counter accept" in bench.read("/etc/jarvis/desktop.nft")
    assert 'SELKIES_ALLOWED_ORIGINS: "https://desktop.example.org"' in bench.read("/opt/jarvis-desktop/compose.yaml")
    assert "JARVIS_BRAIN_URL=https://192.0.2.200:8443\n" in bench.read("/etc/jarvis/site.env")
    brain_example = (REPO / "defaults" / "example.vars").read_text()
    assert "var_ctid=200\n" in brain_example and "var_panel_allow=192.0.2.201\n" in brain_example


def test_the_brain_gets_no_desktop_settings(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(desktop_allow="192.0.2.7"))
    assert done.returncode == 0, done.stdout
    assert "DESKTOP" not in bench.read("/etc/jarvis/site.env") and "BRAIN_URL" not in bench.read("/etc/jarvis/site.env")
    # No Docker and no desktop firewall on the brain: its own rule decides about Jarvis's port and nothing else.
    assert bench.calls("docker") == [] and not any("desktop" in call for call in bench.calls("nft"))
    assert not (bench.state / "nft-loaded").exists() and "jarvis-desktop-firewall" not in ((bench.state / "enabled").read_text())
