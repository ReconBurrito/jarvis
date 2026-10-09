"""Jarvis's service on the brain (its certificates, its firewall rule, its unit) and the link from a desktop
to its brain, as the installers set them up. The service itself is a stand-in here (tests/stubs/systemctl,
tests/stubs/curl); tests/core/test_service_live.py starts the real one, and tests/test_firewall.py loads the
rules into the real nft."""
import json
import subprocess

from conftest import ADDRESS, REPO

CHECK = "bash /opt/jarvis/install/jarvis-install.sh --check"
DESKTOP_CHECK = "bash /opt/jarvis/install/jarvis-desktop-install.sh --check"
TLS = "/etc/jarvis/tls"


def state(bench, name):
    path = bench.state / name
    return path.read_text() if path.exists() else ""


def prints(bench, name):
    return bench.sh(f"openssl x509 -in {TLS}/{name}.pem -noout -fingerprint -sha256").stdout.strip()


def test_install_makes_the_certificates_the_rule_and_the_unit_and_starts_jarvis(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(panel_allow="192.0.2.90"))
    assert done.returncode == 0, done.stdout
    assert f"ok: Jarvis answers on https://{ADDRESS}:8443, for 192.0.2.90 and nobody else" in done.stdout
    assert f"give the desktop's installer var_brain=189" in done.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert f"JARVIS_ADDRESS={ADDRESS}\n" in site and "JARVIS_PANEL_ALLOW=192.0.2.90\n" in site

    # An authority of its own whose key only root reads, and a certificate for this address and nothing else.
    assert bench.sh(f"stat -c '%U:%G %a %n' {TLS} {TLS}/*").stdout.splitlines() == [
        f"root:jarvis 750 {TLS}", f"root:root 600 {TLS}/ca.key", f"root:root 644 {TLS}/ca.pem",
        f"root:jarvis 640 {TLS}/server.key", f"root:root 644 {TLS}/server.pem"]
    assert bench.sh(f"openssl verify -CAfile {TLS}/ca.pem {TLS}/server.pem").stdout == f"{TLS}/server.pem: OK\n"
    text = bench.sh(f"openssl x509 -in {TLS}/server.pem -noout -text").stdout
    assert f"IP Address:{ADDRESS}\n" in text and "DNS:" not in text and "CA:FALSE" in text and "TLS Web Server Authentication" in text
    authority = bench.sh(f"openssl x509 -in {TLS}/ca.pem -noout -text").stdout
    assert "CA:TRUE, pathlen:0" in authority and "Certificate Sign" in authority
    assert bench.sh(f"runuser -u jarvis -- cat {TLS}/ca.key").returncode != 0
    assert bench.sh(f"runuser -u jarvis -- cat {TLS}/server.key").returncode == 0
    assert bench.sh(f"runuser -u nobody -- cat {TLS}/server.key").returncode != 0

    rules = bench.read("/etc/jarvis/brain.nft")
    assert "type filter hook input priority filter; policy accept;" in rules, "nothing else about the container's network is decided"
    assert 'iifname "lo" accept\n        ip saddr { 192.0.2.90 } tcp dport 8443 counter accept\n        tcp dport 8443 counter drop\n' in rules
    assert state(bench, "nft-loaded-brain") == rules

    unit = bench.read("/etc/systemd/system/jarvis.service")
    for line in ("User=jarvis", "ExecStart=/opt/jarvis-venv/current/bin/python -I -m jarvis serve", "NoNewPrivileges=true",
                 "ProtectSystem=strict", "ReadWritePaths=/var/lib/jarvis/audit /var/lib/jarvis/notes", "Requires=jarvis-firewall.service",
                 "StartLimitIntervalSec=120", "StartLimitBurst=5", "Restart=on-failure", "LimitCORE=0",
                 "After=network-online.target ollama.service jarvis-firewall.service", "WantedBy=multi-user.target"):
        assert line + "\n" in unit, line
    assert "Before=network-pre.target jarvis.service shutdown.target\n" in bench.read("/etc/systemd/system/jarvis-firewall.service")
    assert {"jarvis-firewall.service", "jarvis.service"} <= set(state(bench, "enabled").split())
    assert state(bench, "jarvis-starts") == "started\n"
    assert bench.sh(CHECK).returncode == 0
    # The notes: a folder of Jarvis's own, and GitHub's published host keys pinned for reaching them.
    assert bench.sh("stat -c '%U:%G %a' /var/lib/jarvis/notes").stdout == "jarvis:jarvis 700\n"
    assert bench.sh("cmp /opt/jarvis/trust/github_known_hosts /etc/jarvis/trust/github_known_hosts && echo same").stdout == "same\n"
    bench.sh("echo 'github.com ssh-ed25519 swapped' > /etc/jarvis/trust/github_known_hosts")
    looked = bench.sh(CHECK)
    assert looked.returncode == 1 and "github_known_hosts is not GitHub's host keys as this release records them" in looked.stdout

    # Again: the same authority and certificate, the service started anew with the code just installed.
    before = prints(bench, "ca"), prints(bench, "server")
    again = bench.install("brain", bench.answers(panel_allow="192.0.2.90"))
    assert again.returncode == 0, again.stdout
    assert (prints(bench, "ca"), prints(bench, "server")) == before and state(bench, "jarvis-starts") == "started\nstarted\n"
    assert any(call.startswith("systemctl reset-failed jarvis.service") for call in bench.calls("systemctl"))


def test_with_nobody_named_the_service_runs_and_the_install_says_who_is_missing(bench):
    bench.release("v0.1.0")
    done = bench.install("brain")
    assert done.returncode == 0, done.stdout
    assert "WARNING: nobody may reach the panel yet: name the desktop container's address (var_panel_allow)" in done.stdout
    rules = bench.read("/etc/jarvis/brain.nft")
    assert "ip saddr" not in rules and "tcp dport 8443 counter drop" in rules
    # Named later, on a second run: the rule follows, the certificates stay.
    before = prints(bench, "ca")
    named = bench.install("brain", bench.answers(panel_allow="192.0.2.90,198.51.100.0/28"))
    assert named.returncode == 0, named.stdout
    assert "ip saddr { 192.0.2.90, 198.51.100.0/28 } tcp dport 8443 counter accept" in bench.read("/etc/jarvis/brain.nft")
    assert prints(bench, "ca") == before
    for bad in ("192.0.2.300", "192.0.2.5/24", "example.org", "0.0.0.0/0", "192.0.2.1; reboot"):
        refused = bench.install("brain", bench.answers(panel_allow=bad))
        assert refused.returncode == 1 and "these settings are missing or not valid" in refused.stdout, bad
    bench.sh("sed -i 's/^JARVIS_PANEL_ALLOW=.*/JARVIS_PANEL_ALLOW=0.0.0.0\\/0/' /etc/jarvis/site.env")
    refused = bench.sh("bash /opt/jarvis/install/jarvis-install.sh")
    assert refused.returncode == 1 and "JARVIS_PANEL_ALLOW in /etc/jarvis/site.env must be addresses" in refused.stdout


def test_a_new_address_gets_a_new_certificate_from_the_same_authority(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    authority, old = prints(bench, "ca"), prints(bench, "server")
    (bench.state / "address").write_text("192.0.2.77")
    looked = bench.sh(CHECK)
    assert looked.returncode == 1 and "the service's certificate is not for this machine's address (192.0.2.77)" in looked.stdout
    mended = bench.sh("update --repair")
    assert mended.returncode == 0, mended.stdout
    assert f"WARNING: this brain's address changed from {ADDRESS} to 192.0.2.77. Run the desktop's installer again" in mended.stdout
    assert prints(bench, "ca") == authority and prints(bench, "server") != old
    assert "IP Address:192.0.2.77\n" in bench.sh(f"openssl x509 -in {TLS}/server.pem -noout -text").stdout
    assert "JARVIS_ADDRESS=192.0.2.77\n" in bench.read("/etc/jarvis/site.env") and bench.sh(CHECK).returncode == 0


def test_certificates_are_renewed_in_time_and_what_is_wrong_with_them_is_noticed(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    authority = prints(bench, "ca")
    short = (f"openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days DAYS -subj '/CN=Jarvis brain' "
             f"-keyout {TLS}/server.key -out {TLS}/server.pem -CA {TLS}/ca.pem -CAkey {TLS}/ca.key "
             f"-addext subjectAltName=IP:{ADDRESS} 2>/dev/null && chown root:jarvis {TLS}/server.key && chmod 640 {TLS}/server.key")
    # Ten days left: the check says so, and the repair makes a new one from the same authority.
    assert bench.sh(short.replace("DAYS", "10")).returncode == 0
    looked = bench.sh(CHECK)
    assert looked.returncode == 1 and "the service's certificate runs out within two weeks" in looked.stdout
    assert bench.sh("update --repair").returncode == 0 and prints(bench, "ca") == authority
    assert bench.sh(f"openssl x509 -in {TLS}/server.pem -noout -checkend {700 * 86400}").returncode == 0
    # Forty days left: no fault yet, and every install or update renews it all the same.
    assert bench.sh(short.replace("DAYS", "40")).returncode == 0
    forty = prints(bench, "server")
    assert bench.sh(CHECK).returncode == 0
    assert bench.sh("update --repair").returncode == 0 and prints(bench, "server") != forty

    for break_it, words in (
        (f"chmod 644 {TLS}/server.key", f"{TLS}/server.key must belong to root, group jarvis, mode 0640"),
        (f"chmod 640 {TLS}/ca.key", f"{TLS}/ca.key must belong to root alone, mode 0600"),
        (f"openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 30 -subj /CN=x -keyout /dev/null "
         f"-out {TLS}/server.pem -addext subjectAltName=IP:{ADDRESS} 2>/dev/null", "is not signed by this brain's own authority"),
        (f"openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out {TLS}/server.key 2>/dev/null && chown root:jarvis "
         f"{TLS}/server.key && chmod 640 {TLS}/server.key", "the service's key does not belong to its certificate"),
        (f"rm {TLS}/server.pem", f"{TLS}/server.pem is missing"),
        ("echo '# changed' >> /etc/systemd/system/jarvis.service", "/etc/systemd/system/jarvis.service is not the one of this release"),
        ("echo '# changed' >> /etc/jarvis/brain.nft", "/etc/jarvis/brain.nft is not what the settings ask for"),
        (f"rm {bench.state}/nft-loaded-brain", "the firewall table jarvis_brain is not loaded"),
        (f"rm {bench.state}/jarvis-running", "Jarvis's service is not running"),
        (f"touch {bench.state}/jarvis-silent", f"Jarvis's service does not answer on https://{ADDRESS}:8443"),
    ):
        assert bench.sh(break_it).returncode == 0, break_it
        looked = bench.sh(CHECK)
        assert looked.returncode == 1 and words in looked.stdout, (break_it, looked.stdout)
        bench.sh(f"rm -f {bench.state}/jarvis-silent")
        mended = bench.sh("update --repair")
        assert mended.returncode == 0 and bench.sh(CHECK).returncode == 0, (break_it, mended.stdout)
    assert prints(bench, "ca") == authority, "the authority outlived all of it: no desktop has to be told again"

    # Only a lost or foreign authority key makes a new authority, and that is said.
    bench.sh(f"rm {TLS}/ca.key")
    again = bench.sh("update --repair")
    assert again.returncode == 0 and prints(bench, "ca") != authority
    assert "WARNING: this brain has a new certificate authority. Run the desktop's installer again (with var_brain)" in again.stdout


def test_a_service_that_fails_stops_the_install_with_the_reason(bench):
    bench.release("v0.1.0")
    for flag, words in (("jarvis-wont-start", "Jarvis's service does not start. See: journalctl -u jarvis"),
                        ("jarvis-silent", f"Jarvis's service does not answer on https://{ADDRESS}:8443. See: journalctl -u jarvis"),
                        ("fail-nft", "the firewall rules could not be loaded")):
        (bench.state / flag).touch()
        failed = bench.install("brain")
        assert failed.returncode == 1 and words in failed.stdout, (flag, failed.stdout)
        # A service that did not come up is not left switched on, trying again and again.
        assert "jarvis.service" not in state(bench, "enabled").split() and not (bench.state / "jarvis-running").exists(), flag
        (bench.state / flag).unlink()
    done = bench.install("brain")
    assert done.returncode == 0, done.stdout
    # A container without an address, or without the network card at all: said, not a silent end.
    for how in ("address", "no-eth0"):
        (bench.state / how).write_text("")
        for command in ("bash /opt/jarvis/install/jarvis-install.sh", CHECK):
            failed = bench.sh(command)
            assert failed.returncode == 1 and ("has no address on eth0" in failed.stdout or "is not this container's address ()" in failed.stdout), (how, failed.stdout)
        (bench.state / "address").write_text(ADDRESS)
        (bench.state / "no-eth0").unlink(missing_ok=True)
    assert bench.sh("update --repair").returncode == 0


def test_a_failed_release_leaves_no_service_behind_when_the_release_before_has_none(bench):
    """Going back to a release from before there was a service: nothing of that release would stop it."""
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    running = lambda: "jarvis.service" in state(bench, "enabled").split() and (bench.state / "jarvis-running").exists()
    (bench.state / "chat-fails").touch()
    # A check asked for by hand only reports.
    assert bench.sh(CHECK).returncode == 1 and running()
    # The check that ends an install, when the release to go back to has a service too: left alone, since
    # that release starts its own.
    bench.sh("cp /etc/jarvis/release /etc/jarvis/release.pending")
    assert bench.sh(CHECK).returncode == 1 and running()
    # When the release before has none (here: a commit without the service's code), it is switched off.
    bench.git("rm", "-q", "src/jarvis/server.py")
    bench.commit("a release from before the service")
    old = bench.commit_of("HEAD")
    bench.sh(f"git -C /opt/jarvis fetch -q origin main && sed -i 's/^commit=.*/commit={old}/' /etc/jarvis/release")
    failed = bench.sh(CHECK)
    assert failed.returncode == 1 and "Jarvis's service was switched off: this release did not pass its check" in failed.stdout
    assert not running() and not (bench.state / "jarvis-running").exists()
    assert "ExecCondition=/usr/bin/test -f /opt/jarvis/src/jarvis/server.py\n" in bench.read("/etc/systemd/system/jarvis.service")


def test_an_update_that_fails_goes_back_to_a_running_service(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    broken = (REPO / "src" / "jarvis" / "cli.py").read_text().replace("    faults = 0\n", "    faults = 1\n", 1)
    assert broken != (REPO / "src" / "jarvis" / "cli.py").read_text()
    bench.release("v0.2.0", files={"src/jarvis/cli.py": broken})   # a release whose doctor always finds a fault
    failed = bench.sh("update")
    assert failed.returncode == 1 and bench.version() == "v0.1.0", failed.stdout
    assert "jarvis.service" in state(bench, "enabled").split() and (bench.state / "jarvis-running").exists()
    assert state(bench, "jarvis-starts").count("started") == 3 and bench.sh(CHECK).returncode == 0


# ------------------------------------------------------------ a desktop and its brain

def a_brain(bench, ctid=190, address="192.0.2.20", role="brain"):
    """Another container on the node: a brain as the desktop's installer finds it. Returns its authority's text."""
    files = bench.state / f"files-{ctid}" / "etc" / "jarvis"
    (files / "tls").mkdir(parents=True, exist_ok=True)
    (bench.state / f"ct-{ctid}").write_text("description: a brain\n")
    (bench.state / f"run-{ctid}").write_text("running\n")
    lines = [f"JARVIS_ROLE={role}"] + ([f"JARVIS_ADDRESS={address}"] if address else [])
    (files / "site.env").write_text("\n".join(lines) + "\n")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "30",
                    "-subj", "/CN=Jarvis brain authority (test)", "-keyout", "/dev/null", "-out", str(files / "tls" / "ca.pem"),
                    "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0"], check=True, capture_output=True)
    return (files / "tls" / "ca.pem").read_text()


def body_of(pem):
    return "".join(line for line in pem.splitlines() if not line.startswith("-----"))


def test_a_desktop_is_told_where_its_brain_is_and_trusts_it_for_that_address_only(bench):
    bench.release("v0.1.0")
    authority = a_brain(bench)
    (bench.state / "brain-answer").write_text("204")
    done = bench.install("desktop", bench.answers("desktop", brain="190"))
    assert done.returncode == 0, done.stdout
    assert "brain: container 190 at https://192.0.2.20:8443" in done.stdout
    assert "ok: the brain at https://192.0.2.20:8443 answers this desktop" in done.stdout
    assert f"The brain lets it in once its installer was run with var_panel_allow={ADDRESS}" in done.stdout
    assert "JARVIS_BRAIN_URL=https://192.0.2.20:8443\n" in bench.read("/etc/jarvis/site.env")
    assert bench.read("/etc/jarvis/brain-ca.pem") == authority
    assert not any("pct exec 190" in call and " cat " not in call for call in bench.calls("pct")), "the brain was only read from"

    # The browser's rules: this release's, plus the brain's authority for the brain's address and no other.
    policy = json.loads(bench.read("/opt/jarvis-desktop/policies/jarvis.json"))
    release = json.loads((REPO / "desktop" / "policy.json").read_text())
    assert {key: value for key, value in policy.items() if key != "CACertificatesWithConstraints"} == release
    # Both kinds of name are limited: an unnamed kind would be allowed in full, and the brain's authority
    # could then sign for any web site. (tests/core/test_service_live.py tries exactly that in a browser.)
    assert policy["CACertificatesWithConstraints"] == [{"certificate": body_of(authority), "constraints": {
        "permitted_cidrs": ["192.0.2.20/32"], "permitted_dns_names": ["jarvis-brain.invalid"]}}]
    assert "CACertificates" not in policy, "never trusted without limits"
    assert bench.read("/opt/jarvis-desktop/app/brain.js").splitlines()[-1] == 'window.JARVIS_BRAIN = "https://192.0.2.20:8443";'
    assert bench.sh(DESKTOP_CHECK).returncode == 0

    # A second run that does not name the brain keeps it.
    label = json.loads(json.dumps(state(bench, "docker-running"))).split('jarvis.files: "')[1][:16]
    again = bench.install("desktop", bench.answers("desktop"))
    assert again.returncode == 0 and "JARVIS_BRAIN_URL=https://192.0.2.20:8443\n" in bench.read("/etc/jarvis/site.env")
    assert state(bench, "docker-running").split('jarvis.files: "')[1][:16] == label
    # The brain made a new authority: named again, the desktop takes the new one and is made anew.
    renewed = a_brain(bench)
    assert renewed != authority
    again = bench.install("desktop", bench.answers("desktop", brain="190"))
    assert again.returncode == 0, again.stdout
    assert json.loads(bench.read("/opt/jarvis-desktop/policies/jarvis.json"))["CACertificatesWithConstraints"][0]["certificate"] == body_of(renewed)
    assert state(bench, "docker-running").split('jarvis.files: "')[1][:16] != label
    # Named as none: the desktop is as it was before it had a brain.
    unlinked = bench.install("desktop", bench.answers("desktop", brain=""))
    assert unlinked.returncode == 0, unlinked.stdout
    assert json.loads(bench.read("/opt/jarvis-desktop/policies/jarvis.json")) == release
    assert bench.read("/opt/jarvis-desktop/app/brain.js").splitlines()[-1] == 'window.JARVIS_BRAIN = "";'
    assert "this desktop has no brain yet" in unlinked.stdout and bench.sh(DESKTOP_CHECK).returncode == 0


def test_a_brain_that_is_away_or_does_not_let_the_desktop_in_is_said_and_is_no_fault(bench):
    bench.release("v0.1.0")
    a_brain(bench)
    done = bench.install("desktop", bench.answers("desktop", brain="190"))
    assert done.returncode == 0, done.stdout
    assert "WARNING: the brain at https://192.0.2.20:8443 does not answer this desktop." in done.stdout
    (bench.state / "brain-answer").write_text("403")
    looked = bench.sh(DESKTOP_CHECK)
    assert looked.returncode == 0 and f"does not let this desktop in. Run the brain's installer with var_panel_allow={ADDRESS}" in looked.stdout


def test_a_brain_that_is_not_one_stops_the_desktop_install_before_anything_is_changed(bench):
    bench.release("v0.1.0")
    for prepare, words in (
        (lambda: None, "container 190 (var_brain) is not a running container on this node"),
        (lambda: a_brain(bench, role="desktop"), "container 190 (var_brain) is not a Jarvis brain"),
        (lambda: a_brain(bench, address=""), "the brain in container 190 has no panel service yet. Update it first (pct exec 190 -- update)"),
        (lambda: (a_brain(bench), (bench.state / "files-190/etc/jarvis/tls/ca.pem").write_text("not a certificate\n")),
         "handed over as its certificate authority is not one certificate"),
        (lambda: (a_brain(bench), (bench.state / "files-190/etc/jarvis/tls/ca.pem").write_text(a_brain(bench) * 2)),
         "handed over as its certificate authority is not one certificate"),
        (lambda: (bench.state / "run-190").write_text("stopped\n"), "is not a running container on this node"),
    ):
        prepare()
        refused = bench.install("desktop", bench.answers("desktop", brain="190"))
        assert refused.returncode == 1 and words in refused.stdout and "Nothing was changed" in refused.stdout, (words, refused.stdout)
    assert bench.calls("pct create") == []
    own = bench.install("desktop", bench.answers("desktop", brain="189"))
    assert own.returncode == 1 and "var_brain names the desktop's own container" in own.stdout
    for bad in ("jarvis", "13 7", "99", "190; reboot"):
        refused = bench.install("desktop", bench.answers("desktop", brain=bad))
        assert refused.returncode == 1 and "these settings are missing or not valid" in refused.stdout, bad


def test_the_desktop_refuses_a_link_somebody_wrote_into_its_settings_by_hand(bench):
    bench.release("v0.1.0")
    authority = a_brain(bench)
    assert bench.install("desktop", bench.answers("desktop", brain="190")).returncode == 0
    script = "bash /opt/jarvis/install/jarvis-desktop-install.sh"
    for value in ("http://192.0.2.20:8443", "https://example.org:8443", "https://192.0.2.20", 'https://192.0.2.20:8443";alert(1);"'):
        bench.sh(f"sed -i 's|^JARVIS_BRAIN_URL=.*|JARVIS_BRAIN_URL={value}|' /etc/jarvis/site.env")
        refused = bench.sh(script)
        assert refused.returncode == 1 and "JARVIS_BRAIN_URL in /etc/jarvis/site.env must look like https://192.0.2.20:8443" in refused.stdout, value
    bench.sh("sed -i 's|^JARVIS_BRAIN_URL=.*|JARVIS_BRAIN_URL=https://192.0.2.20:8443|' /etc/jarvis/site.env")
    for damage in ("echo extra >> /etc/jarvis/brain-ca.pem", f"cat /etc/jarvis/brain-ca.pem /etc/jarvis/brain-ca.pem > /root/two && cp /root/two /etc/jarvis/brain-ca.pem",
                   "sed -i '2s/^/\"}], \"x\": [{\"/' /etc/jarvis/brain-ca.pem", "rm /etc/jarvis/brain-ca.pem"):
        bench.sh(f"printf %s '{authority}' > /etc/jarvis/brain-ca.pem")
        bench.sh(damage)
        refused = bench.sh(script)
        assert refused.returncode == 1 and "is missing or is not one certificate" in refused.stdout, damage
