"""What counts as a release. Each case is a way to get code run as root in a container without the owner's
signature on exactly that code under exactly that name; none may work."""
import shutil
import subprocess

import pytest

from conftest import REPO

MARK = "#!/bin/bash\necho RAN-AS-ROOT > /root/proof\n"


def installed(bench, version="v0.1.0"):
    bench.release(version)
    done = bench.install()
    assert done.returncode == 0, done.stdout
    return bench


def update(bench, *options):
    return bench.sh("update " + " ".join(options), env=bench.env())


def nothing_ran(bench):
    return bench.sh("test -e /root/proof && echo ran").stdout == ""


def test_an_old_signed_tag_republished_under_a_newer_name_is_not_a_release(bench):
    installed(bench)
    bench.release("v0.2.0", files={"NOTES": "fixed\n"})
    assert "Jarvis is now at v0.2.0" in update(bench).stdout
    # Needs push access only, no key: the old, validly signed tag object under a new name.
    bench.git("push", "-q", "origin", "refs/tags/v0.1.0:refs/tags/v9.9.9")
    stayed = update(bench)
    assert stayed.returncode == 0, stayed.stdout
    assert "release v9.9.9 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert "Jarvis is up to date at v0.2.0." in stayed.stdout
    assert bench.version() == "v0.2.0" and bench.sh("test -e /opt/jarvis/NOTES && echo kept").stdout == "kept\n"


def test_another_tag_signed_by_the_owner_is_not_a_release(bench):
    installed(bench)
    bench.git("checkout", "-q", "-b", "side")
    bench.commit("experiment", {"install/jarvis-install.sh": MARK}, push=False)
    bench.tag("experiment-1", message="not a release")
    bench.git("push", "-q", "origin", "refs/tags/experiment-1:refs/tags/v7.0.0")
    stayed = update(bench)
    assert "release v7.0.0 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert bench.version() == "v0.1.0" and nothing_ran(bench)


def test_a_signed_tag_whose_own_name_differs_is_not_a_release_even_with_the_release_line(bench):
    installed(bench)
    bench.commit("draft", {"install/jarvis-install.sh": MARK})
    bench.tag("draft", message="Jarvis release v7.0.0")
    bench.git("push", "-q", "origin", "refs/tags/draft:refs/tags/v7.0.0")
    stayed = update(bench)
    assert "release v7.0.0 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert bench.version() == "v0.1.0" and nothing_ran(bench)


def test_a_signed_tag_of_the_right_name_without_the_release_line_is_not_a_release(bench):
    installed(bench)
    # What a tag of the same name signed for some other project with the same key would look like.
    bench.release("v0.2.0", files={"install/jarvis-install.sh": MARK}, message="Other project 0.2.0")
    stayed = update(bench)
    assert "release v0.2.0 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert bench.version() == "v0.1.0" and nothing_ran(bench)


def test_unsigned_and_lightweight_tags_and_a_strangers_signature(bench):
    installed(bench)
    stranger = bench.new_key("stranger")
    bench.release("v0.2.0", key=None, files={"install/jarvis-install.sh": MARK})
    bench.release("v0.3.0", key=stranger)
    bench.commit("more")
    bench.git("tag", "v0.4.0")
    bench.git("push", "-q", "origin", "v0.4.0")
    stayed = update(bench)
    for name in ("v0.2.0", "v0.3.0", "v0.4.0"):
        assert f"release {name} is not a release signed by a pinned key; ignored." in stayed.stdout
    assert bench.version() == "v0.1.0" and nothing_ran(bench)


def test_only_plain_version_names_are_releases(bench):
    bench.release("v1.0.0-rc1", files={"install/jarvis-install.sh": MARK})
    bench.release("v1.0.0", files={"install/jarvis-install.sh": (REPO / "install" / "jarvis-install.sh").read_text()})
    done = bench.install()
    assert done.returncode == 0, done.stdout
    assert bench.version() == "v1.0.0"
    for name in ("v1.0.0-rc2", "v1.0.1a", "v01.0.9", "v1.1", "v2"):
        bench.release(name, files={"install/jarvis-install.sh": MARK})
    stayed = update(bench)
    assert "Jarvis is up to date at v1.0.0." in stayed.stdout, stayed.stdout
    assert nothing_ran(bench)


def test_releases_are_ordered_by_number_not_by_text(bench):
    for name in ("v0.9.0", "v0.10.0", "v0.2.0"):
        bench.release(name)
    assert bench.install().returncode == 0
    assert bench.version() == "v0.10.0"


def test_a_tag_put_on_the_installed_code_changes_nothing(bench):
    installed(bench)
    bench.release("v0.2.0")
    assert "now at v0.2.0" in update(bench).stdout
    bench.tag("v99.0.0", key=None, at="v0.2.0^{commit}")
    bench.git("push", "-q", "origin", "v99.0.0")
    bench.release("v0.3.0")
    moved = update(bench)
    assert moved.returncode == 0 and "Jarvis is now at v0.3.0" in moved.stdout, moved.stdout


def test_a_local_branch_named_like_the_release_does_not_win(bench):
    installed(bench)
    old = bench.sh("git -C /opt/jarvis rev-parse HEAD").stdout.strip()
    bench.release("v0.2.0", files={"NOTES": "new\n"})
    bench.sh(f"git -C /opt/jarvis branch v0.2.0 {old}")
    assert "Jarvis is now at v0.2.0" in update(bench).stdout
    assert bench.head() == bench.commit_of("v0.2.0")


@pytest.mark.skipif(not shutil.which("gpg"), reason="needs gpg")
def test_an_openpgp_signature_never_counts(bench, tmp_path):
    installed(bench)
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch", "--quiet"]
    subprocess.run([*gpg, "--passphrase", "", "--quick-generate-key", "Stranger <stranger@example.org>", "ed25519", "sign", "never"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    public = tmp_path / "stranger.asc"
    public.write_bytes(subprocess.run([*gpg, "--armor", "--export"], check=True, stdout=subprocess.PIPE).stdout)
    bench.commit("release v0.2.0", {"install/jarvis-install.sh": MARK})
    wrapper = tmp_path / "gpg-home"
    wrapper.write_text(f'#!/bin/sh\nexec gpg --homedir {home} "$@"\n')
    wrapper.chmod(0o755)
    bench.git("-c", "gpg.format=openpgp", "-c", f"gpg.program={wrapper}", "-c", "user.signingkey=stranger@example.org",
              "tag", "-s", "v0.2.0", "-m", "Jarvis release v0.2.0")
    bench.git("push", "-q", "origin", "v0.2.0")
    # The stranger's key sits in root's keyring in the container, and plain git accepts the tag.
    assert bench.sh(f"gpg --batch --quiet --import {public} 2>&1").returncode == 0
    plain = bench.sh("git -C /opt/jarvis fetch -q --tags origin && git -C /opt/jarvis verify-tag v0.2.0", env=bench.env())
    assert plain.returncode == 0, plain.stdout
    stayed = update(bench)
    assert "release v0.2.0 is not a release signed by a pinned key; ignored." in stayed.stdout
    assert bench.version() == "v0.1.0" and nothing_ran(bench)


def test_the_first_release_of_a_changed_name_is_refused(bench):
    """A release name can never stand for other code than was installed under it."""
    installed(bench)
    bench.commit("other code", {"install/jarvis-install.sh": MARK})
    bench.git("tag", "-d", "v0.1.0")
    bench.tag("v0.1.0")                                    # the remote now offers other, validly signed code as v0.1.0
    bench.git("push", "-q", "-f", "origin", "v0.1.0")
    refused = update(bench)
    assert refused.returncode == 1
    assert "release v0.1.0 on the remote is not the v0.1.0 installed here. Nothing was changed." in refused.stdout
    assert nothing_ran(bench)
