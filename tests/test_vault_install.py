"""The vault on the brain: sops taken by version and checksum, the identity sealed with a passphrase and
unlocked after a restart (jarvis-unlock, run on a terminal here, with the real age), the vault files made
readable for Jarvis alone, and the cluster's certificate authority handed over by the installer on the node."""
import json
import subprocess

from conftest import REPO

CHECK = "bash /opt/jarvis/install/jarvis-install.sh --check"
PASS = "correct horse battery staple"


def state(bench, name):
    path = bench.state / name
    return path.read_text() if path.exists() else ""


def sops_brain(bench, **answers):
    if not bench.git("tag", "-l", "v0.1.0").stdout.strip():
        bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(secrets="sops", **answers))
    assert done.returncode == 0, done.stdout
    return done


def typed(*lines):
    return "".join(line + "\n" for line in lines)


def unlock(bench, *args, passphrase=PASS, times=1):
    return bench.ns("jarvis-unlock", *args, terminal_input=typed(*([passphrase] * times)))


def test_sops_is_taken_by_version_and_checksum_and_age_from_the_system(bench):
    done = sops_brain(bench)
    fetched = [call for call in bench.calls("curl") if "getsops" in call]
    assert len(fetched) == 1 and fetched[0].endswith("https://github.com/getsops/sops/releases/download/v9.9.8/sops-v9.9.8.linux.amd64")
    assert bench.sh("cmp /usr/local/bin/sops " + str(bench.state / "sops-download") + " && stat -c '%U %a' /usr/local/bin/sops").stdout == "root 755\n"
    assert bench.sh("stat -c '%U:%G %a' /var/lib/jarvis-key /etc/jarvis/trust").stdout == "root:root 700\nroot:root 755\n"
    assert "WARNING: this brain has no vault identity yet. As root on the brain (pct enter), run: jarvis-unlock --setup" in done.stdout
    assert bench.sh("cmp /opt/jarvis/bin/jarvis-unlock /usr/sbin/jarvis-unlock && echo same").stdout == "same\n"
    assert "vault: no *.enc.env file in /etc/jarvis/secrets; Jarvis has no tools for the lab until one is there" in done.stdout
    # Again: nothing fetched a second time. Changed by hand: noticed, and put back by a repair.
    assert sops_brain(bench) and len([c for c in bench.calls("curl") if "getsops" in c]) == 1
    bench.sh("echo '# changed' >> /usr/local/bin/sops")
    looked = bench.sh(CHECK)
    assert looked.returncode == 1 and "/usr/local/bin/sops is not sops v9.9.8 as this release records it" in looked.stdout
    assert bench.sh("update --repair").returncode == 0 and bench.sh(CHECK).returncode == 0


def test_a_sops_that_does_not_match_its_checksum_is_not_installed(bench):
    bench.release("v0.1.0")
    with open(bench.state / "sops-download", "a") as extra:
        extra.write("# one line more\n")
    failed = bench.install("brain", bench.answers(secrets="sops"))
    assert failed.returncode == 1
    assert "the sops that arrived does not match the checksum this release records. Nothing of it was installed." in failed.stdout
    assert bench.sh("test -e /usr/local/bin/sops || echo absent").stdout == "absent\n"
    assert bench.sh("ls -A /var/lib/jarvis-install").stdout == ""


def test_the_identity_is_made_sealed_and_unlocked_after_every_restart(bench):
    sops_brain(bench)
    # No identity yet.
    assert "No identity yet. Run: jarvis-unlock --setup" in bench.sh("jarvis-unlock --status").stdout
    refused = unlock(bench)
    assert refused.returncode == 1 and "this brain has no identity yet. Run: jarvis-unlock --setup" in refused.stdout
    # Empty, short or mismatched answers are refused, never handed to age (which would make one up and seal with it).
    tried = bench.ns("jarvis-unlock", "--setup", terminal_input=typed("", "short", PASS, "other", "", ""))
    assert tried.returncode == 1, tried.stdout
    for said in ("An empty passphrase is not accepted. Try again.", "That is shorter than 8 characters. Try again.",
                 "The two did not match. Try again.", "no passphrase was chosen. Nothing was changed."):
        assert said in tried.stdout, tried.stdout
    # (The test terminal shows what is typed ahead of a prompt, since it all arrives at once. Nothing beyond that.)
    assert "autogenerate" not in tried.stdout and tried.stdout.count(PASS) <= 1
    assert bench.sh("ls -A /var/lib/jarvis-key; ls -A /run/jarvis").stdout == "", "nothing left behind"
    no_terminal = bench.sh("jarvis-unlock --setup")
    assert no_terminal.returncode == 1 and "the passphrase is asked on a terminal, and there is none here" in no_terminal.stdout
    # Made: the passphrase is asked for and confirmed; age is handed it and proves the sealed file opens.
    made = unlock(bench, "--setup", times=2)
    assert made.returncode == 0, made.stdout
    public = bench.read("/var/lib/jarvis-key/recipient").strip()
    assert public.startswith("age1") and len(public) == 62 and public in made.stdout
    assert "AGE-SECRET-KEY" not in made.stdout and made.stdout.count(PASS) <= 2, "neither the identity nor the passphrase is shown"
    assert bench.sh("ls -A /run/jarvis").stdout.split() == ["age.key", "recipient"], "no working copy left behind"
    assert "no vault file in /etc/jarvis/secrets yet: copy lab.enc.env there" in made.stdout
    assert bench.sh("stat -c '%U:%G %a %n' /var/lib/jarvis-key/identity.age /run/jarvis /run/jarvis/age.key").stdout.splitlines() == [
        "root:root 600 /var/lib/jarvis-key/identity.age", "root:jarvis 750 /run/jarvis", "root:jarvis 440 /run/jarvis/age.key"]
    assert bench.sh("grep -c AGE-SECRET-KEY /var/lib/jarvis-key/identity.age").stdout == "0\n", "sealed, not in clear"
    assert bench.sh("runuser -u jarvis -- cat /run/jarvis/age.key").stdout.startswith("AGE-SECRET-KEY-1")
    assert bench.sh("runuser -u jarvis -- cat /var/lib/jarvis-key/identity.age").returncode != 0
    assert bench.sh("age-keygen -y /run/jarvis/age.key").stdout.strip() == public
    # A second setup does not replace it unasked.
    again = unlock(bench, "--setup", times=2)
    assert again.returncode == 1 and "this brain has an identity already" in again.stdout
    assert bench.read("/var/lib/jarvis-key/recipient").strip() == public

    # A restart empties /run: locked. The wrong passphrase changes nothing; the right one unlocks.
    assert bench.sh("jarvis-unlock --lock").returncode == 0 and bench.sh("test -e /run/jarvis/age.key || echo gone").stdout == "gone\n"
    assert "Locked. Public key: " + public in bench.sh("jarvis-unlock --status").stdout
    wrong = unlock(bench, passphrase="not it")
    assert wrong.returncode == 1 and "that passphrase does not open this brain's sealed identity. Nothing was changed." in wrong.stdout
    assert wrong.stdout.count("not it") <= 1
    alone = bench.sh("jarvis-unlock")   # no terminal at all
    assert alone.returncode == 1 and "the passphrase is asked on a terminal, and there is none here" in alone.stdout
    assert bench.sh("ls -A /run/jarvis").stdout.split() == ["recipient"], "no working copy left behind"
    assert bench.sh("test -e /run/jarvis/age.key || echo still-locked").stdout == "still-locked\n"
    right = unlock(bench)
    assert right.returncode == 0 and "Unlocked." in right.stdout and bench.sh("age-keygen -y /run/jarvis/age.key").stdout.strip() == public
    # A sealed file that is not this identity's: refused.
    bench.sh("age-keygen -o /root/other 2>/dev/null")
    swap = bench.ns("bash", "-c", "grep AGE-SECRET /root/other | age -p -o /var/lib/jarvis-key/identity.age", terminal_input=typed(PASS, PASS))
    assert swap.returncode == 0, swap.stdout
    mismatch = unlock(bench)
    assert mismatch.returncode == 1 and "does not match its public key" in mismatch.stdout


def test_jarvis_reads_its_vault_once_unlocked_and_the_files_are_kept_for_it(bench):
    sops_brain(bench)
    assert unlock(bench, "--setup", times=2).returncode == 0
    public = bench.read("/var/lib/jarvis-key/recipient").strip()
    plain = "JARVIS_PVE_TOKEN_ID=jarvis@pve!ro\nJARVIS_PVE_TOKEN_SECRET=" + "s" * 36 + "\nJARVIS_PVE_HOSTS=127.0.0.1=pve1\n"
    armoured = subprocess.run(["age", "-a", "-r", public], input=plain, check=True, capture_output=True, text=True).stdout
    vault = bench.tmp / "lab.enc.env"
    vault.write_text(f"sops_age__list_0__map_recipient={public}\nsops_version=3.13.3\n" + armoured)
    # Copied in by hand, as anybody might: readable by everybody.
    bench.sh(f"install -m 0644 {vault} /etc/jarvis/secrets/lab.enc.env")
    looked = bench.sh(CHECK)
    assert looked.returncode == 1 and "/etc/jarvis/secrets/lab.enc.env must belong to root, group jarvis, mode 0640" in looked.stdout
    mended = bench.sh("update --repair")
    assert mended.returncode == 0, mended.stdout
    assert bench.sh("stat -c '%U:%G %a' /etc/jarvis/secrets/lab.enc.env").stdout == "root:jarvis 640\n"
    assert "ok: vault: unlocked, 3 values from 1 file" in mended.stdout
    # The cluster's certificate authority is not there yet: said, and no Proxmox tools.
    assert "WARNING: proxmox: no tools (/etc/jarvis/trust/proxmox-ca.pem is missing (the installer on the Proxmox node puts it there))" in mended.stdout
    status = bench.sh("jarvis-unlock --status").stdout
    assert "Unlocked. Public key: " + public in status and "lab.enc.env: encrypted for this brain" in status
    # Locked: the doctor says how to unlock, and the install check does not hold an update back for it.
    bench.sh("jarvis-unlock --lock")
    locked = bench.sh("jarvis doctor --quick")
    assert locked.returncode == 0 and "WARNING: vault: locked since the last restart; Jarvis cannot read the lab. As root on the brain, run: jarvis-unlock" in locked.stdout
    assert bench.sh(CHECK).returncode == 0
    # A file encrypted for another identity only.
    other = subprocess.run(["age-keygen"], check=True, capture_output=True, text=True).stdout
    other_public = next(line.split(": ")[1] for line in other.splitlines() if line.startswith("# public key: "))
    armoured = subprocess.run(["age", "-a", "-r", other_public], input=plain, check=True, capture_output=True, text=True).stdout
    vault.write_text(f"sops_age__list_0__map_recipient={other_public}\n" + armoured)
    bench.sh(f"install -m 0640 -g jarvis {vault} /etc/jarvis/secrets/lab.enc.env")
    assert unlock(bench).returncode == 0
    said = bench.sh("jarvis doctor --quick").stdout
    assert "FAULT: vault: lab.enc.env: is not encrypted for this brain's identity" in said
    assert "NOT encrypted for this brain: add the public key to .sops.yaml and run sops updatekeys on it" in bench.sh("jarvis-unlock --status").stdout
    assert "s" * 36 not in said + bench.read("/var/lib/jarvis/audit/audit.jsonl")


def test_the_node_installer_hands_the_cluster_authority_to_the_brain(bench):
    ca = bench.tmp / "pve-root-ca.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
                    "-subj", "/CN=Proxmox Virtual Environment", "-keyout", "/dev/null", "-out", str(ca)], check=True, capture_output=True)
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(secrets="sops"), JARVIS_PVE_CA=ca)
    assert done.returncode == 0, done.stdout
    assert "the cluster's certificate authority is handed to the brain" in done.stdout
    assert bench.read("/etc/jarvis/trust/proxmox-ca.pem") == ca.read_text()
    assert bench.sh("stat -c '%U %a' /etc/jarvis/trust/proxmox-ca.pem").stdout == "root 644\n"
    # Not one certificate: said, and nothing is handed over.
    ca.write_text(ca.read_text() * 2)
    again = bench.install("brain", bench.answers(secrets="sops"), JARVIS_PVE_CA=ca)
    assert again.returncode == 0 and "is not one certificate on this node, so Jarvis cannot check the Proxmox API's certificates" in again.stdout
    assert bench.read("/etc/jarvis/trust/proxmox-ca.pem").count("BEGIN CERTIFICATE") == 1
    # A desktop gets none.
    desktop = bench.install("desktop", JARVIS_PVE_CA=ca)
    assert "certificate authority is handed" not in desktop.stdout
