"""`update` inside a container: what it moves to, what it refuses, and how it goes back."""
from conftest import REPO

# An install script that does not put the release's update command in place (so its own check fails) and
# leaves a mess in the code folder on the way.
BROKEN = (REPO / "install" / "jarvis-install.sh").read_text().replace(
    'install_command "$JARVIS_CHECKOUT/bin/update" /usr/bin/update',
    'echo junk > "$JARVIS_CHECKOUT/leftover"; echo junk >> "$JARVIS_CHECKOUT/README.md"')


def installed(bench, version="v0.1.0"):
    bench.release(version)
    done = bench.install()
    assert done.returncode == 0, done.stdout
    return bench


def update(bench, *options):
    return bench.sh("update " + " ".join(options), env=bench.env())


def test_update_moves_to_the_next_signed_release_and_replaces_itself(bench):
    installed(bench)
    assert BROKEN != (REPO / "install" / "jarvis-install.sh").read_text()
    newer = (REPO / "bin" / "update").read_text() + "# a later version of this command\n"
    bench.release("v0.2.0", files={"bin/update": newer})

    waiting = update(bench, "--check")
    assert waiting.returncode == 0 and "Release v0.2.0 is available (installed: v0.1.0)." in waiting.stdout
    assert bench.version() == "v0.1.0"

    moved = update(bench)
    assert moved.returncode == 0, moved.stdout
    assert "Updating Jarvis from v0.1.0 to v0.2.0" in moved.stdout and "Jarvis is now at v0.2.0" in moved.stdout
    assert bench.version() == "v0.2.0"
    assert bench.read("/usr/bin/update") == newer

    again = update(bench)
    assert again.returncode == 0 and "Jarvis is up to date at v0.2.0." in again.stdout


def test_update_ignores_releases_it_cannot_trust(bench):
    installed(bench)
    stranger = bench.new_key("stranger")
    bench.release("v0.2.0", key=None)
    bench.release("v0.3.0", key=stranger)
    stayed = update(bench)
    assert stayed.returncode == 0, stayed.stdout
    assert "release v0.3.0 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert "release v0.2.0 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert "Jarvis is up to date at v0.1.0." in stayed.stdout
    assert bench.version() == "v0.1.0"


def test_a_key_added_to_the_repository_later_is_not_trusted(bench):
    installed(bench)
    stranger = bench.new_key("stranger")
    bench.release("v0.2.0", key=stranger,
                  files={"trust/allowed_signers": bench.signers(bench.owner, stranger)})
    stayed = update(bench)
    assert "release v0.2.0 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert bench.version() == "v0.1.0"


def test_a_settings_file_on_the_machine_cannot_swap_the_signature_check(bench):
    installed(bench)
    stranger = bench.new_key("stranger")
    bench.release("v0.2.0", key=stranger)
    # A stand-in for ssh-keygen that calls every signature good, named in the machine-wide git settings.
    yes = bench.tmp / "always-good"
    yes.write_text("#!/bin/sh\n"
                   "case \"$*\" in *find-principals*) echo anyone ;; "
                   "*) echo 'Good \"git\" signature for anyone with ED25519 key SHA256:" + "x" * 43 + "' ;; esac\n")
    yes.chmod(0o755)
    assert bench.sh(f"git config --system gpg.ssh.program {yes}").returncode == 0
    fooled = bench.sh("git -C /opt/jarvis -c gpg.format=ssh fetch -q --tags origin && "
                      "git -C /opt/jarvis -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=/etc/jarvis/allowed_signers "
                      "verify-tag v0.2.0", env=bench.env())
    assert fooled.returncode == 0, fooled.stdout  # plain git would be fooled
    stayed = update(bench)
    assert "release v0.2.0 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert bench.version() == "v0.1.0"


def test_a_release_that_fails_its_check_is_undone(bench):
    installed(bench)
    bench.release("v0.2.0")
    assert update(bench).returncode == 0
    bench.release("v0.3.0", files={"install/jarvis-install.sh": BROKEN, "bin/update": "#!/bin/sh\nexit 9\n"})

    failed = update(bench)
    assert failed.returncode == 1, failed.stdout
    assert "release v0.3.0 did not install cleanly; going back to v0.2.0" in failed.stdout
    assert "the update failed. Jarvis is back at v0.2.0 and passes its check." in failed.stdout
    facts = bench.sh("""
        sed -n 's/^name=//p' /etc/jarvis/release
        cmp /opt/jarvis/bin/update /usr/bin/update && echo same-update
        git -C /opt/jarvis status --porcelain
        ls /etc/jarvis/release.pending 2>/dev/null
    """)
    assert facts.stdout == "v0.2.0\nsame-update\n", facts.stdout  # no leftover, no edited file, nothing pending
    assert bench.head() == bench.commit_of("v0.2.0")

    # The fixed release after it installs normally.
    bench.release("v0.3.1", files={"install/jarvis-install.sh": (REPO / "install" / "jarvis-install.sh").read_text(),
                                   "bin/update": (REPO / "bin" / "update").read_text()})
    fixed = update(bench)
    assert fixed.returncode == 0 and "Jarvis is now at v0.3.1" in fixed.stdout, fixed.stdout


def test_a_release_tag_that_was_moved_stops_the_update(bench):
    installed(bench)
    bench.commit("something else", {"install/jarvis-install.sh": "#!/bin/bash\necho RAN > /root/proof\n"})
    bench.git("-c", f"user.signingkey={bench.owner}", "tag", "-f", "-s", "v0.1.0", "-m", "Jarvis release v0.1.0")
    bench.git("push", "-q", "-f", "origin", "v0.1.0")
    refused = update(bench)
    assert refused.returncode == 1
    assert "release v0.1.0 on the remote is not the v0.1.0 installed here. Nothing was changed." in refused.stdout
    assert bench.version() == "v0.1.0" and bench.sh("test -e /root/proof && echo ran").stdout == ""


def test_update_never_goes_to_an_older_release(bench):
    installed(bench)
    second = bench.new_key("second")
    # v0.2.0 is signed by a second key; the container is told to trust both, then only the first again.
    bench.release("v0.2.0", key=second)
    both = bench.tmp / "both"
    both.write_text(bench.signers(bench.owner, second))
    bench.sh(f"cp {both} /etc/jarvis/allowed_signers")
    assert "Jarvis is now at v0.2.0" in update(bench).stdout
    bench.sh(f"cp {bench.src}/trust/allowed_signers /etc/jarvis/allowed_signers")
    refused = update(bench)
    assert refused.returncode == 1, refused.stdout
    assert "(v0.1.0) is older than one this container has run (v0.2.0). Nothing was changed." in refused.stdout
    assert bench.version() == "v0.2.0"


def test_code_changed_by_hand_is_not_overwritten(bench):
    installed(bench)
    bench.release("v0.2.0")
    bench.sh("echo '# edited' >> /opt/jarvis/bin/update")
    bench.sh("echo mine > /opt/jarvis/notes.txt")
    refused = update(bench)
    assert refused.returncode == 1 and "it was changed by hand" in refused.stdout and "Nothing was updated." in refused.stdout
    assert bench.version() == "v0.1.0"
    told = update(bench, "--check")
    assert told.returncode == 0 and "Release v0.2.0 is available" in told.stdout and "was changed by hand" in told.stdout
    refused = update(bench, "--repair")
    assert refused.returncode == 1 and "Nothing was repaired" in refused.stdout
    assert bench.sh("cat /opt/jarvis/notes.txt").stdout == "mine\n"
    # A commit of one's own in the code folder is a change by hand as well.
    bench.sh("cd /opt/jarvis && git add -A && git -c user.name=x -c user.email=x@example.org commit -q -m mine", env=bench.env())
    assert "it was changed by hand" in update(bench).stdout
    moved = update(bench, "--discard")
    assert moved.returncode == 0 and "Jarvis is now at v0.2.0" in moved.stdout, moved.stdout
    assert bench.sh("test -e /opt/jarvis/notes.txt && echo kept").stdout == ""


def test_repair_puts_back_what_was_removed(bench):
    installed(bench)
    bench.sh("rm -rf /etc/jarvis/secrets; chmod 0777 /var/lib/jarvis")
    broken = bench.sh("bash /opt/jarvis/install/jarvis-install.sh --check")
    assert broken.returncode == 1 and "the .env folder /etc/jarvis/secrets is missing" in broken.stdout
    repaired = update(bench, "--repair")
    assert repaired.returncode == 0 and "Jarvis v0.1.0: repaired" in repaired.stdout, repaired.stdout
    assert bench.sh("stat -c '%U:%G %a' /etc/jarvis/secrets /var/lib/jarvis").stdout == "root:jarvis 750\nroot:jarvis 750\n"


def test_options_and_a_missing_install(bench):
    installed(bench)
    assert "unknown option '--force'" in update(bench, "--force").stdout
    assert "use one of --check and --repair" in update(bench, "--check", "--repair").stdout
    busy = bench.sh("exec 8>/run/jarvis-update.lock; flock 8; update", env=bench.env())
    assert busy.returncode == 1 and "another update is running" in busy.stdout
    bench.sh("mv /opt/jarvis /opt/gone")
    gone = update(bench)
    assert gone.returncode == 1 and "Jarvis is not installed in /opt/jarvis" in gone.stdout


def test_the_env_folder_keeps_what_the_owner_put_there(bench):
    installed(bench)
    bench.sh("printf 'TOKEN=example\\n' > /etc/jarvis/secrets/jarvis.env; chmod 0640 /etc/jarvis/secrets/jarvis.env")
    bench.release("v0.2.0")
    assert update(bench).returncode == 0
    assert bench.read("/etc/jarvis/secrets/jarvis.env") == "TOKEN=example\n"


# ---------------------------------------------------------------- found by review

GOOD = (REPO / "install" / "jarvis-install.sh").read_text()


def test_an_update_that_was_killed_is_finished_by_the_next_one(bench):
    installed(bench)
    killed = GOOD.replace('say "Commands"', 'say "Commands"\n    [ -e /root/second-try ] || { touch /root/second-try; kill -KILL $PPID; exit 1; }')
    newer = (REPO / "bin" / "update").read_text() + "# later\n"
    bench.release("v0.2.0", files={"install/jarvis-install.sh": killed, "bin/update": newer})
    cut = update(bench)
    assert cut.returncode != 0 and "Jarvis is now at" not in cut.stdout
    assert bench.version() == "v0.1.0"  # nothing is recorded as installed before its check passed

    told = update(bench, "--check")
    assert "Release v0.2.0 is available (installed: v0.1.0)." in told.stdout
    done = update(bench)
    assert done.returncode == 0 and "Jarvis is now at v0.2.0" in done.stdout, done.stdout
    assert bench.version() == "v0.2.0" and bench.read("/usr/bin/update") == newer
    assert bench.sh("bash /opt/jarvis/install/jarvis-install.sh --check").returncode == 0


def test_a_revert_that_was_killed_is_put_in_order_by_the_next_update(bench):
    installed(bench)
    # v0.2.0 fails its check; while going back to v0.1.0 the machine loses power (the code is left at v0.2.0).
    bench.release("v0.2.0", files={"install/jarvis-install.sh": BROKEN, "bin/update": "#!/bin/sh\nexit 9\n"})
    bench.sh("cd /opt/jarvis && git fetch -q origin '+refs/tags/v*:refs/jarvis/tags/v*' && git checkout -q -f refs/jarvis/tags/v0.2.0"
             " && echo junk > leftover && git rev-parse HEAD > /etc/jarvis/release.pending", env=bench.env())
    bench.git("push", "-q", "origin", ":refs/tags/v0.2.0")  # meanwhile the owner withdrew the broken release
    told = update(bench, "--check")
    assert told.returncode == 0 and "An earlier update or repair was cut short." in told.stdout, told.stdout
    back = update(bench)
    assert back.returncode == 0 and "Putting Jarvis v0.1.0 back in order" in back.stdout, back.stdout
    assert bench.sh("git -C /opt/jarvis status --porcelain; ls /etc/jarvis/release.pending 2>/dev/null").stdout == ""
    assert bench.head() == bench.commit_of("v0.1.0")
    assert "Jarvis is up to date at v0.1.0." in update(bench).stdout


def test_ctrl_c_during_an_update_goes_back(bench):
    installed(bench)
    stopped = GOOD.replace('say "Commands"', 'kill -INT $PPID; exit 130')
    bench.release("v0.2.0", files={"install/jarvis-install.sh": stopped, "bin/update": "#!/bin/sh\nexit 9\n"})
    out = update(bench)
    assert out.returncode == 1 and "Jarvis is back at v0.1.0 and passes its check." in out.stdout, out.stdout
    assert bench.version() == "v0.1.0"
    assert bench.sh("cmp /opt/jarvis/bin/update /usr/bin/update").returncode == 0 and bench.head() == bench.commit_of("v0.1.0")


def test_an_update_does_not_need_the_package_servers_when_nothing_is_missing(bench):
    installed(bench)
    bench.sh("printf '#!/bin/sh\\nexit 100\\n' > /usr/local/sbin/apt-get")
    bench.release("v0.2.0")
    moved = update(bench)
    assert moved.returncode == 0 and "Jarvis is now at v0.2.0" in moved.stdout, moved.stdout


def test_a_missing_package_that_cannot_be_fetched_says_why(bench):
    installed(bench)
    bench.sh("printf '#!/bin/sh\\necho E: Temporary failure resolving the mirror >&2\\nexit 100\\n' > /usr/local/sbin/apt-get")
    bench.release("v0.2.0", files={"install/jarvis-install.sh": GOOD.replace("tzdata", "tzdata no-such-package-for-the-test")})
    failed = bench.sh("update", env=bench.env(JARVIS_APT_PAUSE="0"))
    assert failed.returncode == 1
    assert "E: Temporary failure resolving the mirror" in failed.stdout
    assert "could not install: no-such-package-for-the-test" in failed.stdout
    assert "Jarvis is back at v0.1.0 and passes its check." in failed.stdout


def test_the_floor_is_the_newest_release_ever_run(bench):
    installed(bench)
    bench.release("v0.2.0")
    assert update(bench).returncode == 0
    assert bench.read("/etc/jarvis/release.floor") == "v0.2.0\n"
    # v0.3.0 fails and is undone: the floor stays where the last good install put it.
    bench.release("v0.3.0", files={"install/jarvis-install.sh": BROKEN, "bin/update": "#!/bin/sh\nexit 9\n"})
    assert update(bench).returncode == 1
    assert bench.read("/etc/jarvis/release.floor") == "v0.2.0\n" and bench.version() == "v0.2.0"


def test_repair_without_an_install_says_so(bench):
    installed(bench)
    bench.sh("rm /etc/jarvis/release")
    refused = update(bench, "--repair")
    assert refused.returncode == 1 and "no release is recorded as installed here" in refused.stdout
    again = update(bench)
    assert again.returncode == 0 and "Updating Jarvis from nothing to v0.1.0" in again.stdout, again.stdout


def test_an_install_script_that_leaves_files_in_the_code_folder_has_failed(bench):
    installed(bench)
    littering = GOOD.replace('say "Commands"', 'echo cache > "$JARVIS_CHECKOUT/build.cache"\n    say "Commands"')
    bench.release("v0.2.0", files={"install/jarvis-install.sh": littering})
    failed = update(bench)
    assert failed.returncode == 1 and "left or changed files in /opt/jarvis" in failed.stdout
    assert "Jarvis is back at v0.1.0 and passes its check." in failed.stdout
    bench.release("v0.2.1", files={"install/jarvis-install.sh": GOOD})
    fixed = update(bench)
    assert fixed.returncode == 0 and "Jarvis is now at v0.2.1" in fixed.stdout, fixed.stdout


def test_a_failed_repair_is_not_called_up_to_date(bench):
    installed(bench)
    bench.sh("userdel -r jarvis; mv /usr/sbin/useradd /usr/sbin/useradd.real; printf '#!/bin/sh\\nexit 1\\n' > /usr/sbin/useradd; "
             "chmod +x /usr/sbin/useradd")
    failed = update(bench, "--repair")
    assert failed.returncode == 1 and "repairing v0.1.0 failed" in failed.stdout
    told = update(bench)
    assert "up to date" not in told.stdout and told.returncode == 1
    bench.sh("mv /usr/sbin/useradd.real /usr/sbin/useradd")
    mended = update(bench)
    assert mended.returncode == 0 and "Putting Jarvis v0.1.0 back in order" in mended.stdout, mended.stdout
    assert "Jarvis is up to date at v0.1.0." in update(bench).stdout


def test_what_is_recorded_survives_a_full_disk_and_is_never_guessed(bench):
    installed(bench)
    bench.release("v0.2.0")
    # The folder of the record has no room left: the update must fail and say so, not report success.
    full = bench.sh("cp -a /etc/jarvis /root/keep && mount -t tmpfs -o size=64k tmpfs /etc/jarvis && cp -a /root/keep/. /etc/jarvis/ "
                    "&& { dd if=/dev/zero of=/etc/jarvis/fill bs=1k count=128 2>/dev/null; update; echo status=$?; }", env=bench.env())
    assert "status=1" in full.stdout and "Jarvis is now at" not in full.stdout, full.stdout
    assert bench.version() == "v0.1.0"
    assert update(bench).returncode == 0 and bench.version() == "v0.2.0"

    for damage in ("echo rubbish > /etc/jarvis/release", "printf 'v0.2.0 \\n' > /etc/jarvis/release.floor",
                   ": > /etc/jarvis/release"):
        keep = "cp -a /etc/jarvis/release /root/r; cp -a /etc/jarvis/release.floor /root/f; "
        back = "; status=$?; cp -a /root/r /etc/jarvis/release; cp -a /root/f /etc/jarvis/release.floor; exit $status"
        refused = bench.sh(keep + damage + "; update" + back, env=bench.env())
        assert refused.returncode == 1 and "is damaged" in refused.stdout and "Nothing was changed" in refused.stdout, damage


def test_the_floor_is_kept_twice(bench):
    installed(bench)
    second = bench.new_key("second")
    bench.release("v0.2.0", key=second)
    both = bench.tmp / "both"
    both.write_text(bench.signers(bench.owner, second))
    bench.sh(f"cp {both} /etc/jarvis/allowed_signers")
    assert "Jarvis is now at v0.2.0" in update(bench).stdout
    bench.sh(f"cp {bench.src}/trust/allowed_signers /etc/jarvis/allowed_signers; rm /etc/jarvis/release.floor")
    refused = update(bench)
    assert refused.returncode == 1 and "is older than one this container has run (v0.2.0)" in refused.stdout


def test_what_the_remote_once_held_cannot_block_a_later_update(bench):
    installed(bench)
    # Somebody with push access plants names that collide with future releases; the container fetches them.
    bench.git("push", "-q", "origin", "HEAD:refs/tags/v0.2.0/x", "HEAD:refs/heads/fix")
    assert update(bench).returncode == 0
    # The owner removes them and releases; a branch is renamed into a folder of branches on the way.
    bench.git("push", "-q", "origin", ":refs/tags/v0.2.0/x", ":refs/heads/fix")
    bench.git("push", "-q", "origin", "HEAD:refs/heads/fix/login")
    bench.release("v0.2.0")
    moved = update(bench)
    assert moved.returncode == 0 and "Jarvis is now at v0.2.0" in moved.stdout, moved.stdout
    # Only release tags are kept from the remote, and exactly those it has now.
    refs = bench.sh("git -C /opt/jarvis for-each-ref --format='%(refname)'").stdout.split()
    assert refs == ["refs/jarvis/installed", "refs/jarvis/tags/v0.1.0", "refs/jarvis/tags/v0.2.0"], refs


def test_many_worthless_tags_make_little_noise(bench):
    installed(bench)
    for number in range(8):
        bench.tag(f"v9.{number}.0", key=None)
    bench.git("push", "-q", "origin", "--tags")
    stayed = update(bench)
    assert stayed.returncode == 0 and "Jarvis is up to date at v0.1.0." in stayed.stdout
    assert stayed.stdout.count("is not a release signed by a pinned key; ignored.") == 5
    assert stayed.stdout.count("more tags of that kind follow") == 1


def test_git_variables_of_the_shell_do_not_redirect_update(bench):
    installed(bench)
    bench.release("v0.2.0")
    moved = bench.sh("update", env=bench.env(GIT_DIR="/root/elsewhere/.git", GIT_WORK_TREE="/root/elsewhere"))
    assert moved.returncode == 0 and "Jarvis is now at v0.2.0" in moved.stdout, moved.stdout


def test_what_differs_from_the_release_is_put_aside_never_dropped(bench):
    """Also where update does not refuse: after an install or repair that was cut short."""
    installed(bench)
    bench.sh("userdel -r jarvis; mv /usr/sbin/useradd /usr/sbin/useradd.real; printf '#!/bin/sh\\nexit 1\\n' > /usr/sbin/useradd; "
             "chmod +x /usr/sbin/useradd")
    assert update(bench, "--repair").returncode == 1          # leaves its mark: cut short
    bench.sh("mv /usr/sbin/useradd.real /usr/sbin/useradd; echo '# my fix' >> /opt/jarvis/install/jarvis-install.sh; "
             "echo mine > /opt/jarvis/notes.txt")
    mended = update(bench)
    assert mended.returncode == 0 and "were put aside, not dropped" in mended.stdout, mended.stdout
    kept = bench.sh("cd /opt/jarvis && git stash list && git stash show -p --include-untracked stash@{0}", env=bench.env())
    assert "put aside before installing v0.1.0" in kept.stdout and "+# my fix" in kept.stdout and "+mine" in kept.stdout
    assert "Jarvis is up to date at v0.1.0." in update(bench).stdout

    # With --discard the same holds: asked to go ahead, it still drops nothing.
    bench.sh("echo again > /opt/jarvis/notes.txt")
    bench.release("v0.2.0")
    moved = update(bench, "--discard")
    assert moved.returncode == 0 and "were put aside, not dropped" in moved.stdout
    assert bench.sh("git -C /opt/jarvis stash list | wc -l").stdout.strip() == "2"


def test_the_installed_code_keeps_a_name_of_its_own(bench):
    """So git never clears it away, even when its tag is gone from the remote and it has no descendants."""
    installed(bench)
    first = bench.head()
    bench.git("push", "-q", "origin", ":refs/tags/v0.1.0")     # withdrawn
    bench.git("checkout", "-q", "--orphan", "fresh")
    bench.commit("unrelated history", push=False)
    bench.tag("v0.2.0")
    bench.git("push", "-q", "origin", "v0.2.0")
    assert bench.sh("git -C /opt/jarvis rev-parse refs/jarvis/installed").stdout.strip() == first
    assert update(bench).returncode == 0 and bench.version() == "v0.2.0"
    gone = bench.sh("cd /opt/jarvis && git reflog expire --expire=now --all && git gc -q --prune=now && git cat-file -t %s" % first,
                    env=bench.env())
    assert gone.returncode != 0                                 # the old code is no longer needed, and is cleared
    assert bench.sh("git -C /opt/jarvis rev-parse refs/jarvis/installed").stdout.strip() == bench.head()
    assert update(bench, "--repair").returncode == 0
