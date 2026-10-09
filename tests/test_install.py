"""The installers, run for real against stand-ins for the Proxmox tools (see conftest.py)."""
import re
import subprocess

from conftest import ADDRESS, MAC, REPO


NO_KEYS = "# Keys allowed to sign releases.\n#\n\n"


def created(bench):
    lines = bench.calls("pct create")
    assert len(lines) == 1, lines
    return lines[0]


def test_brain_unattended_asks_nothing_and_installs_the_signed_release(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(env_dir="/srv/jarvis/env", secrets="sops", timezone="Europe/London"))
    assert done.returncode == 0, done.stdout
    # No terminal existed, so a single question would have ended the run.
    assert "Container 189 is installed: Jarvis brain at " + ADDRESS in done.stdout

    line = created(bench)
    assert line.startswith("pct create 189 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst ")
    for part in ("--hostname jarvis ", "--unprivileged 1 ", "--features nesting=1 ", "--cores 6 ", "--memory 512 ",
                 "--swap 0 ", "--rootfs local-lvm:48 ", f"--net0 name=eth0,bridge=vmbr0,tag=100,hwaddr={MAC},ip=dhcp ",
                 "--onboot 1 ", "--tags jarvis ", "--timezone Europe/London ",
                 "--description Jarvis brain. Installed by the Jarvis installer (role brain)."):
        assert part in line + " ", part
    assert "--dev0" not in line and "keyctl" not in line

    site = bench.read("/etc/jarvis/site.env")
    where = next(line for line in site.splitlines() if line.startswith("JARVIS_HOST_DESCRIPTION="))
    assert where.split("=", 1)[1].startswith(("a Linux container named ", "a computer named ", "a virtual machine named "))
    assert set(site.replace(where, "").split()) == {
        "JARVIS_ROLE=brain", "JARVIS_REPO_URL=https://github.com/test/jarvis.git", "JARVIS_RELEASE_CHANNEL=signed",
        "JARVIS_RELEASE_BRANCH=main", "JARVIS_ENV_DIR=/srv/jarvis/env", "JARVIS_SECRETS_MODE=sops",
        "JARVIS_LOCAL_MODEL=qwen3:8b", "JARVIS_EMBED_MODEL=qwen3-embedding:0.6b", "JARVIS_PANEL_ALLOW=",
        f"JARVIS_ADDRESS={ADDRESS}"}
    facts = bench.sh("""
        sed -n 's/^name=//p' /etc/jarvis/release
        cat /etc/jarvis/release.floor
        stat -c '%U:%G %a' /srv/jarvis/env /var/lib/jarvis
        cmp /opt/jarvis/bin/update /usr/bin/update && echo same-update
        cmp /etc/jarvis/allowed_signers SRC/trust/allowed_signers && echo pinned
        test -e /root/jarvis-bootstrap || echo bootstrap-removed
        test -e /etc/jarvis/site.env.install || echo handover-removed
        id -un jarvis
        /usr/bin/update --check
    """.replace("SRC", str(bench.src)))
    assert facts.stdout.split("\n")[:9] == [
        "v0.1.0", "v0.1.0", "root:jarvis 750", "root:jarvis 750", "same-update", "pinned", "bootstrap-removed",
        "handover-removed", "jarvis"], facts.stdout
    assert "Jarvis is up to date at v0.1.0." in facts.stdout
    assert bench.head() == bench.commit_of("v0.1.0")


def test_desktop_unattended(bench):
    bench.release("v0.1.0")
    done = bench.install("desktop", bench.answers("desktop", gpu="/dev/null", gpu_gid="993", swap=None))
    assert done.returncode == 0, done.stdout
    line = created(bench)
    for part in ("--hostname jarvis-desktop ", "--features nesting=1,keyctl=1 ", "--cores 4 ", "--swap 512 ",
                 "--rootfs local-lvm:32 ", "--dev0 /dev/null,gid=993"):
        assert part in line + " ", part
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_ROLE=desktop" in site and "JARVIS_ENV_DIR" not in site and "JARVIS_SECRETS_MODE" not in site
    facts = bench.sh("sed -n 's/^name=//p' /etc/jarvis/release; test -e /etc/jarvis/secrets || echo no-env-folder; id jarvis 2>&1")
    assert facts.stdout.startswith("v0.1.0\nno-env-folder\n") and "no such user" in facts.stdout
    # No render group in this pretend container, so the shared device is left as it was given.
    assert bench.calls("pct set") == [] and bench.calls("pct reboot") == []


def test_settings_in_the_environment_win_over_the_answers_file(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(cpu="2"), var_cpu="3", var_hostname="friday")
    assert done.returncode == 0, done.stdout
    assert "--cores 3 " in created(bench) and "--hostname friday " in created(bench)


def test_a_fixed_address_needs_and_uses_a_gateway(bench):
    bench.release("v0.1.0")
    refused = bench.install("brain", bench.answers(net="192.0.2.89/24", vlan="", mac=""))
    assert refused.returncode == 1 and "var_gateway is needed" in refused.stdout
    assert bench.calls("pct create") == []
    done = bench.install("brain", bench.answers(net="192.0.2.89/24", gateway="192.0.2.1", vlan="", mac=""))
    assert done.returncode == 0, done.stdout
    assert "--net0 name=eth0,bridge=vmbr0,ip=192.0.2.89/24,gw=192.0.2.1 " in created(bench)


def test_bad_answers_stop_before_anything_is_changed(bench):
    bench.release("v0.1.0")
    for answers, message in [
        (bench.answers(colour="blue"), "var_colour is not a setting of this installer"),
        (bench.answers(ctid="12"), "not valid: var_ctid='12'"),
        (bench.answers(mac="zz:zz"), "var_mac='zz:zz'"),
        (bench.answers(env_dir="/run/jarvis"), "var_env_dir='/run/jarvis'"),
        (bench.answers(env_dir="/opt/jarvis/env"), "var_env_dir='/opt/jarvis/env'"),
        (bench.answers(env_dir="/etc/../root"), "var_env_dir='/etc/../root'"),
        (bench.answers(hostname="bad;name"), "var_hostname='bad;name'"),
        (bench.answers(repo="https://example.org/x"), "var_repo="),
        (bench.answers(brg="nobridge"), "bridge nobridge does not exist"),
        (bench.answers(container_storage="nowhere"), "storage nowhere is not an active storage for container disks"),
        (bench.answers(gpu="/dev/dri/renderD999"), "/dev/dri/renderD999 is not a device on this node"),
        (bench.answers(ram="999999999"), "not valid: var_ram='999999999'"),
        (bench.answers(ram="4000000"), "Set var_allow_low_memory=yes to install anyway"),
    ]:
        refused = bench.install("brain", answers)
        assert refused.returncode == 1, refused.stdout
        assert message in refused.stdout, refused.stdout
        assert "Nothing was changed" in refused.stdout
    bad = bench.tmp / "bad.vars"
    bad.write_text("var_ctid=189\nrm -rf /tmp/x\n")
    refused = bench.install("brain", bad)
    assert refused.returncode == 1 and "line 2 is not of the form var_name=value" in refused.stdout
    assert bench.calls("pct create") == [] and bench.calls("pct start") == []


def test_without_answers_and_without_a_terminal_it_refuses(bench):
    bench.release("v0.1.0")
    refused = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=bench.env())
    assert refused.returncode == 1 and "there is no terminal to ask on" in refused.stdout
    assert bench.calls("pct create") == []


def test_an_id_in_use_is_never_touched(bench):
    bench.release("v0.1.0")
    (bench.state / "ct-189").write_text("description: somebody else's container\n")
    refused = bench.install()
    assert refused.returncode == 1 and "container 189 exists and was not made by this installer" in refused.stdout
    (bench.state / "ct-189").unlink()
    (bench.state / "resources").write_text('[{"id":"lxc/189","vmid":189,"node":"other"}]')
    refused = bench.install()
    assert refused.returncode == 1 and "ID 189 is in use on another node" in refused.stdout
    # A desktop made by this installer is not taken over by the brain installer either.
    (bench.state / "resources").unlink()
    assert bench.install("desktop").returncode == 0
    refused = bench.install("brain")
    assert refused.returncode == 1 and "was not made by this installer for the brain role" in refused.stdout
    assert len(bench.calls("pct create")) == 1


def test_a_wrong_dhcp_address_stops_the_install_and_a_second_run_carries_on(bench):
    bench.release("v0.1.0")
    (bench.state / "address").write_text("192.0.2.150")
    refused = bench.install()
    assert refused.returncode == 1
    assert f"got 192.0.2.150 from DHCP, not {ADDRESS}" in refused.stdout and "fixed lease" in refused.stdout
    assert bench.sh("test -e /opt/jarvis && echo installed").stdout == ""
    (bench.state / "address").write_text(ADDRESS)
    done = bench.install()
    assert done.returncode == 0, done.stdout
    assert len(bench.calls("pct create")) == 1  # carried on with the container from the first run
    assert bench.version() == "v0.1.0"


def test_no_address_at_all(bench):
    bench.release("v0.1.0")
    (bench.state / "address").write_text("")
    refused = bench.install()
    assert refused.returncode == 1 and "container 189 got no address" in refused.stdout


def test_the_render_group_of_the_container_decides_the_device_group(bench):
    bench.release("v0.1.0")
    assert bench.sh("groupadd -g 4105 render").returncode == 0
    done = bench.install("brain", bench.answers(gpu="/dev/null"))
    assert done.returncode == 0, done.stdout
    assert "--dev0 /dev/null,gid=993" in created(bench)
    assert bench.calls("pct set") == ["pct set 189 --dev0 /dev/null,gid=4105"]
    assert bench.calls("pct reboot") == ["pct reboot 189"]


def test_a_missing_template_is_downloaded(bench):
    bench.release("v0.1.0")
    (bench.state / "no-template").write_text("")
    done = bench.install()
    assert done.returncode == 0, done.stdout
    assert bench.calls("pveam download") == ["pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst"]
    assert created(bench).startswith("pct create 189 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst ")


def test_only_a_release_signed_by_the_pinned_key_is_installed(bench):
    stranger = bench.new_key("stranger")
    bench.release("v0.1.0")
    bench.release("v0.2.0", key=None)        # newer, not signed
    bench.release("v0.3.0", key=stranger)    # newer, signed by a key that is not pinned
    done = bench.install()
    assert done.returncode == 0, done.stdout
    assert "release v0.3.0 is not a release signed by a pinned key; ignored." in done.stdout
    assert "release v0.2.0 is not a release signed by a pinned key; ignored." in done.stdout
    assert bench.version() == "v0.1.0"


def test_no_pinned_key_or_no_signed_release_installs_nothing(bench):
    bench.release("v0.1.0", key=None)
    refused = bench.install()
    assert refused.returncode == 1 and "the remote has no release signed by a pinned key" in refused.stdout
    assert bench.sh("ls /opt/jarvis; test -e /usr/bin/update && echo has-update").stdout == ""


def test_an_empty_signers_file_trusts_nothing(bench):
    bench.release("v0.1.0")
    (bench.src / "trust" / "allowed_signers").write_text(NO_KEYS)
    refused = bench.install()
    assert refused.returncode == 1 and "no release signing key is pinned" in refused.stdout


def test_a_second_run_does_not_replace_the_pinned_keys(bench):
    bench.release("v0.1.0")
    assert bench.install().returncode == 0
    pinned = bench.read("/etc/jarvis/allowed_signers")
    (bench.src / "trust" / "allowed_signers").write_text(bench.signers(bench.new_key("intruder")))
    again = bench.install()
    assert again.returncode == 0, again.stdout
    assert "already pinned in the container, left as they are" in again.stdout
    assert bench.read("/etc/jarvis/allowed_signers") == pinned


def test_the_branch_channel_follows_a_branch(bench):
    bench.commit("work in progress")
    done = bench.install("brain", bench.answers(release="branch"))
    assert done.returncode == 0, done.stdout
    assert bench.version() == "main@" + bench.git("rev-parse", "--short=12", "HEAD").stdout.strip()
    bench.commit("more work", {"NOTES": "x\n"})
    moved = bench.sh("update", env=bench.env())
    assert moved.returncode == 0, moved.stdout
    assert bench.version() == "main@" + bench.git("rev-parse", "--short=12", "HEAD").stdout.strip()
    assert bench.sh("cat /etc/jarvis/release.floor 2>&1").returncode != 0  # a branch sets no release floor


def typed(*lines):
    return "".join(line + "\n" for line in lines)


def test_default_mode_asks_only_where_the_env_files_live(bench):
    bench.release("v0.1.0")
    env = bench.env(var_ctid="189", var_repo="test/jarvis", var_ram="512", var_gpu="none")
    done = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env,
                    terminal_input=typed("default", "/srv/keys", "sops", "no model", "llama3.2:3b", "192.0.2.90", "y"))
    assert done.returncode == 0, done.stdout
    asked = re.findall(r"([A-Z][^\[\]\n]*) \[[^\]]*\]: ", done.stdout)
    model = "Local model Jarvis thinks with, as Ollama names it (none for no local model)"
    assert asked == ["Choice", "Directory where Jarvis keeps its .env files", "Choice", model, model,
                     "Addresses that may reach Jarvis's panel port: the desktop container (commas between; empty for nobody)",
                     "Create the container with these settings?"], done.stdout
    assert f"That is not a valid value for: {model}" in done.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_ENV_DIR=/srv/keys" in site and "JARVIS_SECRETS_MODE=sops" in site
    assert "JARVIS_LOCAL_MODEL=llama3.2:3b\n" in site and "JARVIS_EMBED_MODEL=qwen3-embedding:0.6b\n" in site
    assert "JARVIS_PANEL_ALLOW=192.0.2.90\n" in site and "Panel:       Jarvis may be reached by 192.0.2.90" in done.stdout
    assert bench.sh("stat -c '%U:%G %a' /srv/keys").stdout == "root:jarvis 750\n"


def test_default_mode_with_enter_keeps_the_defaults_and_no_means_no(bench):
    bench.release("v0.1.0")
    env = bench.env(var_ctid="189", var_repo="test/jarvis", var_ram="512", var_gpu="none")
    stopped = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=typed("", "", "", "", "", "n"))
    assert stopped.returncode == 1 and "stopped. Nothing was changed." in stopped.stdout
    assert bench.calls("pct create") == []
    done = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=typed("", "", "", "", "", "y"))
    assert done.returncode == 0, done.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_ENV_DIR=/etc/jarvis/secrets" in site and "JARVIS_SECRETS_MODE=plain" in site
    assert "JARVIS_LOCAL_MODEL=qwen3:8b\n" in site and "JARVIS_PANEL_ALLOW=\n" in site
    assert "Panel:       Jarvis may be reached by nobody yet" in done.stdout


def test_advanced_mode_asks_every_setting_and_asks_again_after_a_bad_answer(bench):
    bench.release("v0.1.0")
    env = bench.env(var_repo="test/jarvis")
    answers = typed(
        "advanced", "12", "189", "friday", "tank", "local", "64", "2", "512", "0", "vmbr0", "300", MAC,
        "192.0.2.89/24", "192.0.2.1", "none", "Europe/London", "jarvis;lab", "no", "/srv/env", "plain", "none", "192.0.2.90", "branch",
        "main", "y")
    done = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=answers)
    assert done.returncode == 0, done.stdout
    assert "That is not a valid value for: Container ID" in done.stdout
    line = created(bench)
    for part in ("pct create 189 ", "--hostname friday ", "--cores 2 ", "--rootfs tank:64 ", "--onboot 0 ",
                 f"--net0 name=eth0,bridge=vmbr0,tag=300,hwaddr={MAC},ip=192.0.2.89/24,gw=192.0.2.1 ",
                 "--tags jarvis;lab ", "--timezone Europe/London "):
        assert part in line + " ", part
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_RELEASE_CHANNEL=branch" in site and "JARVIS_ENV_DIR=/srv/env" in site
    # No local model was asked for: none is fetched, and the model for searching notes is not asked about.
    assert "JARVIS_LOCAL_MODEL=none\n" in site and "Local model for searching notes" not in done.stdout
    assert bench.calls("curl") == [] or not any("ollama" in call for call in bench.calls("curl"))
    # Not asked: the repository (given in the environment), and settings that do not apply to these answers.
    assert "GitHub repository (owner/name) [" not in done.stdout
    assert "Address DHCP must hand out" not in done.stdout and "Group ID of the render group" not in done.stdout
    assert "Container ID of the Jarvis brain" not in done.stdout and "Addresses that may open the desktop" not in done.stdout
    assert "JARVIS_PANEL_ALLOW=192.0.2.90\n" in site


def test_leaving_the_menu_changes_nothing(bench):
    bench.release("v0.1.0")
    left = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=bench.env(), terminal_input=typed("quit"))
    assert left.returncode == 1 and "stopped. Nothing was changed." in left.stdout
    assert bench.calls("pct create") == []


# ---------------------------------------------------------------- found by review


def test_answers_from_a_file_must_name_the_container(bench):
    bench.release("v0.1.0")
    refused = bench.install("brain", bench.answers(ctid=None))
    assert refused.returncode == 1 and "var_ctid is needed when the answers come from a file" in refused.stdout
    assert bench.calls("pct create") == []


def test_a_second_interactive_run_offers_the_container_of_the_first(bench):
    bench.release("v0.1.0")
    (bench.state / "address").write_text("192.0.2.150")
    env = bench.env(var_repo="test/jarvis", var_ram="512", var_gpu="none", var_expect_ip=ADDRESS, STUB_NEXTID="100")
    first = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=typed("", "", "", "", "", "y"))
    assert first.returncode == 1 and "run this again with var_ctid=100" in first.stdout
    (bench.state / "address").write_text(ADDRESS)
    env["STUB_NEXTID"] = "101"  # what a node answers once 100 exists
    second = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=typed("", "", "", ""))
    assert second.returncode == 0, second.stdout
    assert "Container 100 was made by this installer for the brain role." in second.stdout
    assert [line.split()[2] for line in bench.calls("pct create")] == ["100"]
    # Asked for, another one is made.
    third = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=typed("", "new", "", "", "", "", "y"))
    assert third.returncode == 0, third.stdout
    assert [line.split()[2] for line in bench.calls("pct create")] == ["100", "101"]


def test_signers_without_a_key_are_replaced_by_a_second_run(bench):
    bench.release("v0.1.0")
    good = (bench.src / "trust" / "allowed_signers").read_text()
    (bench.src / "trust" / "allowed_signers").write_text(NO_KEYS)
    assert "no release signing key is pinned" in bench.install().stdout
    (bench.src / "trust" / "allowed_signers").write_text(good)
    done = bench.install()
    assert done.returncode == 0, done.stdout
    assert bench.version() == "v0.1.0"


def test_a_second_run_still_corrects_the_device_group(bench):
    bench.release("v0.1.0")
    assert bench.sh("groupadd -g 4105 render").returncode == 0
    (bench.state / "address").write_text("192.0.2.150")
    answers = bench.answers(gpu="/dev/null")
    assert bench.install("brain", answers).returncode == 1
    (bench.state / "address").write_text(ADDRESS)
    assert bench.install("brain", answers).returncode == 0
    assert bench.calls("pct set") == ["pct set 189 --dev0 /dev/null,gid=4105"]
    assert bench.install("brain", answers).returncode == 0  # and a third run has nothing left to correct
    assert len(bench.calls("pct set")) == 1 and len(bench.calls("pct reboot")) == 1


def test_a_second_run_changes_nothing_nobody_asked_for(bench):
    bench.release("v0.1.0")
    assert bench.sh("groupadd -g 4105 render").returncode == 0
    made = bench.install("brain", bench.answers(env_dir="/srv/keys", secrets="sops"))  # no GPU
    assert made.returncode == 0, made.stdout
    # Again, now naming a GPU and nothing about the .env files: no device is added, nothing is restarted.
    again = bench.install("brain", bench.answers(gpu="/dev/null"))
    assert again.returncode == 0, again.stdout
    assert bench.calls("pct set") == [] and bench.calls("pct reboot") == []
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_ENV_DIR=/srv/keys" in site and "JARVIS_SECRETS_MODE=sops" in site
    # And through the questions, carrying on: the same.
    env = bench.env(var_repo="test/jarvis", var_ram="512")
    carried = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=typed("", ""))
    assert carried.returncode == 0, carried.stdout
    assert "Directory where Jarvis keeps its .env files [" not in carried.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_ENV_DIR=/srv/keys" in site and "JARVIS_SECRETS_MODE=sops" in site
    assert bench.sh("test -e /etc/jarvis/secrets && echo made").stdout == ""


def test_a_container_that_only_quotes_the_installers_words_is_not_taken_for_its_own(bench):
    bench.release("v0.1.0")
    (bench.state / "ct-189").write_text("description: Mail. Not Jarvis brain. Installed by the Jarvis installer (role brain).%0A")
    refused = bench.install()
    assert refused.returncode == 1 and "container 189 exists and was not made by this installer" in refused.stdout
    assert bench.calls("pct push") == []


def test_pinned_keys_are_left_alone_when_the_container_cannot_be_asked(bench):
    bench.release("v0.1.0")
    assert bench.install().returncode == 0
    pinned = bench.read("/etc/jarvis/allowed_signers")
    (bench.src / "trust" / "allowed_signers").write_text(bench.signers(bench.new_key("intruder")))
    (bench.state / "fail-exec").write_text("grep -Eqv")
    refused = bench.install()
    assert refused.returncode == 1 and "could not be asked about its release signing keys" in refused.stdout
    (bench.state / "fail-exec").write_text("")
    assert bench.read("/etc/jarvis/allowed_signers") == pinned


def test_the_installer_does_not_throw_away_code_changed_by_hand(bench):
    bench.release("v0.1.0")
    assert bench.install().returncode == 0
    bench.sh("echo '# mine' >> /opt/jarvis/README.md; echo mine > /opt/jarvis/notes.txt")
    refused = bench.install()
    assert refused.returncode == 1 and "was changed by hand since it was installed" in refused.stdout
    assert "update --discard" in refused.stdout
    assert bench.sh("tail -n 1 /opt/jarvis/README.md; cat /opt/jarvis/notes.txt").stdout == "# mine\nmine\n"


def test_a_container_without_a_render_group_gets_a_warning(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(gpu="/dev/null"))
    assert done.returncode == 0 and "the container has no render group" in done.stdout


ENV_DIRS = {
    "/etc/jarvis/secrets": True, "/srv/jarvis": True, "/srv/jarvis/env.d": True, "/mnt/keys/jarvis": True,
    "/opt/secrets": True, "/media/usb/jarvis": True, "/var/lib/jarvis-env": False, "/var/lib/apt": False,
    "/etc": False, "/etc/jarvis": False, "/root": False, "/root/env": False, "/usr/bin": False, "/srv": False,
    "/var/lib": False, "/var/lib/jarvis": False, "/var/lib/jarvis/env": False, "/opt/jarvis": False,
    "/opt/jarvis/env": False, "/var/run/jarvis": False, "/var/tmp/jarvis": False, "/var/lock/jarvis": False,
    "/run/jarvis": False, "/tmp/jarvis": False, "/home/jarvis/env": False, "/srv/../etc": False,
    "/srv/.hidden": False, "/srv/a/./b": False, "/srv/jarvis/": False, "srv/jarvis": False, "/srv/a b": False,
    "/srv/a;b": False, "/srv/$(id)": False, "/srv/" + "x" * 200: False,
}


def test_where_the_env_folder_may_be(bench):
    """The node and the container apply the same rule."""
    for library in ("misc/build.func", "misc/install.func"):
        script = f'set -u; . {REPO}/{library}; while IFS= read -r path; do env_dir_ok "$path" && echo "yes $path" || echo "no $path"; done'
        out = subprocess.run(["bash", "-c", script], input="\n".join(ENV_DIRS) + "\n", text=True, stdout=subprocess.PIPE, check=True)
        got = {line.split(" ", 1)[1]: line.startswith("yes ") for line in out.stdout.splitlines()}
        assert got == ENV_DIRS, library
    bench.release("v0.1.0")
    refused = bench.install("brain", bench.answers(env_dir="/etc"))
    assert refused.returncode == 1 and "var_env_dir='/etc'" in refused.stdout and bench.calls("pct create") == []


def test_the_container_refuses_a_folder_that_is_a_link_or_somebody_elses(bench):
    bench.release("v0.1.0")
    bench.sh("mkdir -p /srv; ln -s /run /srv/keys; useradd -m other; install -d -o other /srv/theirs; "
             "install -d /srv/data; echo kept > /srv/data/report")
    linked = bench.install("brain", bench.answers(env_dir="/srv/keys/jarvis"))
    assert linked.returncode == 1 and "leads elsewhere through a link" in linked.stdout
    theirs = bench.install("brain", bench.answers(env_dir="/srv/theirs"))
    assert theirs.returncode == 1 and "exists and is not an empty folder owned by root; it was left as it is" in theirs.stdout
    assert bench.sh("stat -c '%U' /srv/theirs").stdout == "other\n"
    in_use = bench.install("brain", bench.answers(env_dir="/srv/data"))
    assert in_use.returncode == 1 and "exists and is not an empty folder owned by root" in in_use.stdout
    assert bench.sh("stat -c '%U:%G %a' /srv/data; ls /srv/data").stdout == "root:root 755\nreport\n"
    assert bench.version() == ""  # neither run was recorded as an install


def test_strict_settings(bench):
    bench.release("v0.1.0")
    for answers, message in [
        (bench.answers(ctid="0189"), "var_ctid='0189'"),
        (bench.answers(cpu="007"), "var_cpu='007'"),
        (bench.answers(vlan="020"), "var_vlan='020'"),
        (bench.answers(expect_ip="192.0.2.089"), "var_expect_ip='192.0.2.089'"),
        (bench.answers(mac="01" + MAC[2:]), "var_mac='01" + MAC[2:] + "'"),  # a group address, not a card's own
        (bench.answers(timezone="--force"), "var_timezone='--force'"),
        (bench.answers(timezone="Mars/Phobos"), "var_timezone='Mars/Phobos'"),
        (bench.answers(timezone="Etc/UTC"), "var_timezone='Etc/UTC'"),
        (bench.answers(container_storage="cold"), "storage cold is not an active storage for container disks"),
        (bench.answers(container_storage="local"), "storage local is not an active storage for container disks"),
        (bench.answers(template_storage="local-lvm"), "storage local-lvm is not an active storage for container templates"),
    ]:
        refused = bench.install("brain", answers)
        assert refused.returncode == 1 and message in refused.stdout, refused.stdout
        assert "Nothing was changed" in refused.stdout
    assert bench.calls("pct create") == [] and bench.calls("pveam download") == []


def test_settings_mean_the_same_in_every_language_of_the_node(bench):
    bench.release("v0.1.0")
    for answers, message in [(bench.answers(hostname="jarvisé"), "var_hostname="),
                             (bench.answers(vlan="٣"), "var_vlan="),
                             (bench.answers(env_dir="/srv/sécrets"), "var_env_dir=")]:
        refused = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=bench.env(JARVIS_ANSWERS=answers, LC_ALL="C.utf8", LANG="C.utf8"))
        assert refused.returncode == 1 and message in refused.stdout and "Nothing was changed" in refused.stdout, refused.stdout
    assert bench.calls("pct create") == []


def test_an_older_proxmox_is_refused(bench):
    bench.release("v0.1.0")
    for version in ("8.1.4", "7.4-3"):
        refused = bench.install("brain", STUB_PVE_VERSION=version)
        assert refused.returncode == 1 and "Proxmox VE 8.2 or newer is needed. Nothing was changed." in refused.stdout
    assert bench.install("brain", STUB_PVE_VERSION="8.2.2").returncode == 0


def test_a_second_run_with_another_repository_fetches_from_it(bench):
    bench.release("v0.1.0")
    assert bench.install().returncode == 0
    env = bench.env(JARVIS_ANSWERS=bench.answers(repo="other/jarvis"), GIT_CONFIG_VALUE_0="https://github.com/other/jarvis.git")
    again = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env)
    assert again.returncode == 0, again.stdout
    assert bench.sh("git -C /opt/jarvis remote get-url origin").stdout == "https://github.com/other/jarvis.git\n"


def test_the_installer_waits_for_no_update_and_keeps_no_lock_open(bench):
    bench.release("v0.1.0", files={"install/jarvis-install.sh": (REPO / "install" / "jarvis-install.sh").read_text().replace(
        'say "Commands"', 'ls /proc/$$/fd > /root/open-files\n    say "Commands"')})
    busy = bench.sh(f"exec 8>/run/jarvis-update.lock; flock 8; JARVIS_ANSWERS={bench.answers()} bash {bench.src}/ct/jarvis.sh",
                    env=bench.env())
    assert busy.returncode == 1 and "an update is running in this container" in busy.stdout
    assert bench.install().returncode == 0
    assert "9" not in bench.read("/root/open-files").split()


def test_started_straight_from_the_web(bench):
    """bash -c "$(curl ...)": the script has no file of its own, fetches the repository at one ref and cleans up."""
    bench.release("v0.1.0")
    script = (bench.src / "ct" / "jarvis.sh").read_text()
    env = bench.env(JARVIS_ANSWERS=bench.answers(), JARVIS_REPO="test/jarvis", JARVIS_REF="main", TMPDIR=bench.tmp / "tmp")
    (bench.tmp / "tmp").mkdir()
    done = bench.ns("bash", "-c", script, env=env)
    assert done.returncode == 0, done.stdout
    assert bench.calls("curl")[0] == "curl -fsSL https://codeload.github.com/test/jarvis/tar.gz/main"
    assert not any("codeload" in call for call in bench.calls("curl")[1:])
    assert bench.version() == "v0.1.0" and list((bench.tmp / "tmp").iterdir()) == []

    (bench.state / "no-download").write_text("")
    failed = bench.ns("bash", "-c", script, env=bench.env(JARVIS_ANSWERS=bench.answers(ctid="190"), JARVIS_REPO="test/jarvis",
                                                         TMPDIR=bench.tmp / "tmp"))
    assert failed.returncode == 1 and "could not fetch test/jarvis at main. Nothing was changed." in failed.stdout
    assert len(bench.calls("pct create")) == 1 and list((bench.tmp / "tmp").iterdir()) == []


def test_the_example_answers_file_is_a_working_one(bench):
    bench.release("v0.1.0")
    (bench.state / "address").write_text("192.0.2.200")
    env = bench.env(JARVIS_ANSWERS=REPO / "defaults" / "example.vars", var_repo="test/jarvis", var_ram="512", var_gpu="/dev/null")
    done = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env)
    assert done.returncode == 0, done.stdout
    line = created(bench)
    for part in ("pct create 200 ", "--hostname jarvis ", "tag=100,hwaddr=00:00:5E:00:53:C8,ip=dhcp ", "--timezone Europe/London ",
                 "--dev0 /dev/null,gid=993"):
        assert part in line + " ", part


def test_a_first_run_that_stopped_early_has_still_handed_over_its_settings(bench):
    """The second run names only the container; what the first run chose must not be lost."""
    bench.release("v0.1.0")
    (bench.state / "address").write_text("192.0.2.150")
    first = bench.install("brain", bench.answers(env_dir="/srv/keys", secrets="sops"))
    assert first.returncode == 1 and "run this again with var_ctid=189" in first.stdout
    (bench.state / "address").write_text(ADDRESS)
    second = bench.install("brain", bench.answers(repo=None, mac=None, vlan=None))  # ctid, ram, gpu and the address check only
    assert second.returncode == 0, second.stdout
    assert {line for line in bench.read("/etc/jarvis/site.env").splitlines() if not line.startswith("JARVIS_HOST_DESCRIPTION=")} == {
        "JARVIS_ROLE=brain", "JARVIS_REPO_URL=https://github.com/test/jarvis.git", "JARVIS_RELEASE_CHANNEL=signed",
        "JARVIS_RELEASE_BRANCH=main", "JARVIS_ENV_DIR=/srv/keys", "JARVIS_SECRETS_MODE=sops",
        "JARVIS_LOCAL_MODEL=qwen3:8b", "JARVIS_EMBED_MODEL=qwen3-embedding:0.6b", "JARVIS_PANEL_ALLOW=",
        f"JARVIS_ADDRESS={ADDRESS}"}
    assert bench.version() == "v0.1.0" and bench.sh("stat -c '%U:%G %a' /srv/keys").stdout == "root:jarvis 750\n"


def test_the_same_through_the_questions(bench):
    bench.release("v0.1.0")
    (bench.state / "address").write_text("192.0.2.150")
    env = bench.env(var_repo="test/jarvis", var_ram="512", var_gpu="none", var_expect_ip=ADDRESS)
    first = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=env, terminal_input=typed("", "/srv/keys", "sops", "", "", "y"))
    assert first.returncode == 1 and "from DHCP" in first.stdout
    (bench.state / "address").write_text(ADDRESS)
    del env["var_repo"]
    second = bench.ns("bash", bench.src / "ct" / "jarvis.sh", env=bench.env(var_ram="512"), terminal_input=typed("", ""))
    assert second.returncode == 0, second.stdout
    site = bench.read("/etc/jarvis/site.env")
    assert "JARVIS_ENV_DIR=/srv/keys" in site and "JARVIS_SECRETS_MODE=sops" in site and "test/jarvis" in site


def test_a_stop_nobody_foresaw_names_the_command_and_its_code(bench):
    """On one node a plain command into the container came back failed, now and then and without a word, and
    the installer ended as silently. Whatever the reason, it says where it stopped."""
    bench.release("v0.1.0")
    # A stop the installer explains itself, here from inside a piece it runs to collect an answer: said
    # once, not reported a second time as unforeseen, and ending with 1 as before.
    (bench.state / "no-template").touch()
    (bench.state / "no-template-offered").touch()
    refused = bench.install()
    assert refused.returncode == 1 and refused.stdout.count("no Ubuntu 24.04 template") == 1, refused.stdout
    assert "stopped where it should not have" not in refused.stdout
    (bench.state / "no-template").unlink()
    (bench.state / "no-template-offered").unlink()
    assert bench.install().returncode == 0
    for command in ("install -d -m 0755 /etc/jarvis", "install -d -m 0700 /root/jarvis-bootstrap"):
        (bench.state / "fail-exec").write_text(command)
        stopped = bench.install()
        assert stopped.returncode == 255, stopped.stdout
        assert "jarvis-install: stopped where it should not have. This command ended with code 255:" in stopped.stdout
        assert f"(inside container 189: {command})" in stopped.stdout
        assert "Running the installer again carries on from where it stopped." in stopped.stdout
        assert stopped.stdout.count("stopped where it should not have") == 1
    (bench.state / "fail-exec").write_text("")
    again = bench.install()
    assert again.returncode == 0 and "stopped where it should not have" not in again.stdout, again.stdout
    # A stop the installer explains itself is not reported a second time, and still ends with 1.
    refused = bench.install("brain", bench.answers(ram="12"))
    assert refused.returncode == 1 and "these settings are missing or not valid" in refused.stdout
    assert "stopped where it should not have" not in refused.stdout


def test_settings_are_not_rewritten_when_the_container_cannot_be_asked(bench):
    bench.release("v0.1.0")
    assert bench.install("brain", bench.answers(env_dir="/srv/keys")).returncode == 0
    (bench.state / "fail-exec").write_text("test -s /etc/jarvis/site.env")
    refused = bench.install()
    assert refused.returncode == 1 and "could not be asked for its settings" in refused.stdout
    (bench.state / "fail-exec").write_text("")
    assert "JARVIS_ENV_DIR=/srv/keys" in bench.read("/etc/jarvis/site.env")
