"""The brain's local model: Ollama taken by version and checksum, the models fetched, and the proof that
the model sits on the GPU. Ollama itself is a stand-in here (tests/stubs/curl, tests/stubs/systemctl)."""
import subprocess

from conftest import REPO

UNIT = "/etc/systemd/system/ollama.service"


def state(bench, name):
    path = bench.state / name
    return path.read_text() if path.exists() else ""


def render_device(bench):
    """A folder with a render device in it, as a container that was given a GPU has."""
    dri = bench.tmp / "dri"
    dri.mkdir(exist_ok=True)
    if not (dri / "renderD128").exists():
        subprocess.run(["mknod", str(dri / "renderD128"), "c", "226", "128"], check=True)
    return dri


def test_the_real_release_is_named_by_version_and_checksum():
    words = dict(line.split("=", 1) for line in (REPO / "brain" / "ollama").read_text().split() if "=" in line)
    assert words["version"].startswith("v") and len(words["sha256"]) == 64 and int(words["sha256"], 16)


def test_install_fetches_ollama_and_the_models_and_proves_the_gpu(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", JARVIS_DRI_DIR=render_device(bench))
    assert done.returncode == 0, done.stdout
    assert "qwen3:8b is on the GPU (100 percent)" in done.stdout
    assert "ok: local model qwen3:8b served by Ollama v9.9.9" in done.stdout
    fetched = [call for call in bench.calls("curl") if "releases/download" in call]
    assert len(fetched) == 1 and fetched[0].endswith("https://github.com/ollama/ollama/releases/download/v9.9.9/ollama-linux-amd64.tar.zst")
    assert bench.sh("/usr/local/bin/ollama").stdout == "ollama version is 9.9.9\n"
    assert bench.sh("test -f /usr/local/lib/ollama/libggml-base.so && ls /var/lib/jarvis-install").stdout == ""   # nothing left of the download
    assert bench.sh("stat -c '%U %a' /var/lib/jarvis-install").stdout == "root 700\n"
    unit = bench.read(UNIT)
    for line in ("ExecStart=/usr/local/bin/ollama serve", "User=ollama", 'Environment="OLLAMA_HOST=127.0.0.1:11434"',
                 'Environment="OLLAMA_CONTEXT_LENGTH=16384"', 'Environment="OLLAMA_VULKAN=1"', "WantedBy=multi-user.target"):
        assert line + "\n" in unit, line
    assert "0.0.0.0" not in unit
    assert "ollama\n" in state(bench, "enabled") and state(bench, "ollama-starts") == "started\n"
    assert state(bench, "ollama-models") == "qwen3:8b\nqwen3-embedding:0.6b\n"
    assert bench.sh("id -nG ollama; getent passwd ollama | cut -d: -f6").stdout.split()[-1] == "/var/lib/ollama"

    # Again: nothing is fetched or restarted a second time, and the check passes.
    again = bench.install("brain", JARVIS_DRI_DIR=render_device(bench))
    assert again.returncode == 0, again.stdout
    assert len([call for call in bench.calls("curl") if "releases/download" in call]) == 1
    assert len([call for call in bench.calls("curl") if "/api/pull" in call]) == 2
    assert state(bench, "ollama-starts") == "started\n"
    check = bench.sh("bash /opt/jarvis/install/jarvis-install.sh --check", env=bench.env(JARVIS_DRI_DIR=render_device(bench)))
    assert check.returncode == 0 and "Jarvis brain: as it should be" in check.stdout, check.stdout


def test_without_a_gpu_the_model_runs_on_the_processor_and_that_is_said(bench):
    bench.release("v0.1.0")
    (bench.state / "ollama-gpu-share").write_text("0")
    done = bench.install("brain", JARVIS_DRI_DIR=bench.tmp / "no-such-folder")
    assert done.returncode == 0, done.stdout
    assert "no GPU was given to this container: qwen3:8b runs on the processor" in done.stdout
    assert "OLLAMA_VULKAN" not in bench.read(UNIT)


def test_a_model_on_the_processor_although_there_is_a_gpu_is_a_fault(bench):
    bench.release("v0.1.0")
    (bench.state / "ollama-gpu-share").write_text("0")
    failed = bench.install("brain", JARVIS_DRI_DIR=render_device(bench))
    assert failed.returncode == 1
    assert "the model qwen3:8b runs on the processor although this container has a GPU" in failed.stdout
    assert "the local model is not as it should be" in failed.stdout
    # Mended (the device's group, say), the installer run again carries on and passes.
    (bench.state / "ollama-gpu-share").write_text("100")
    done = bench.install("brain", JARVIS_DRI_DIR=render_device(bench))
    assert done.returncode == 0, done.stdout
    assert len([call for call in bench.calls("curl") if "releases/download" in call]) == 1


def test_a_model_that_only_partly_fits_is_a_warning(bench):
    bench.release("v0.1.0")
    (bench.state / "ollama-gpu-share").write_text("62")
    done = bench.install("brain", JARVIS_DRI_DIR=render_device(bench))
    assert done.returncode == 0, done.stdout
    assert "WARNING: only 62 percent of qwen3:8b fits on the GPU" in done.stdout


def test_an_archive_that_does_not_match_its_checksum_is_not_installed(bench):
    bench.release("v0.1.0")
    with open(bench.state / "ollama.tar.zst", "ab") as archive:
        archive.write(b"one byte more")
    failed = bench.install("brain")
    assert failed.returncode == 1
    assert "the Ollama archive that arrived does not match the checksum this release records. Nothing of it was installed." in failed.stdout
    assert bench.sh("ls /usr/local/bin/ollama /usr/local/lib/ollama 2>&1 | grep -c 'No such file'").stdout == "2\n"
    assert bench.sh("ls /var/lib/jarvis-install").stdout == "" and state(bench, "ollama-starts") == ""


def test_an_archive_with_files_that_do_not_belong_is_not_installed(bench):
    pack = bench.tmp / "bad-pack"
    (pack / "bin").mkdir(parents=True)
    (pack / "etc").mkdir()
    (pack / "bin" / "ollama").write_text("#!/bin/sh\n")
    (pack / "etc" / "passwd").write_text("root::0:0::/:/bin/sh\n")
    archive = bench.state / "ollama.tar.zst"
    archive.unlink()
    subprocess.run(f"tar -c -C '{pack}' . | zstd -q -o '{archive}'", shell=True, check=True)
    digest = subprocess.run(["sha256sum", str(archive)], check=True, text=True, stdout=subprocess.PIPE).stdout.split()[0]
    bench.release("v0.1.0", files={"brain/ollama": f"version=v9.9.9\nsha256={digest}\n"})
    failed = bench.install("brain")
    assert failed.returncode == 1 and "the Ollama archive holds paths that do not belong in it (./etc/" in failed.stdout
    assert bench.sh("test -e /usr/local/bin/ollama || echo absent").stdout == "absent\n"


def test_failures_say_what_failed(bench):
    bench.release("v0.1.0")
    for flag, value, words in (
        ("no-ollama-download", "", "Ollama v9.9.9 could not be fetched from https://github.com/ollama/ollama/releases/download/v9.9.9/"),
        ("ollama-wont-start", "", "Ollama does not start. See: journalctl -u ollama"),
        ("ollama-down", "", "Ollama does not answer on http://127.0.0.1:11434"),
        ("fail-model-pull", "qwen3:8b", "the model qwen3:8b could not be fetched. Check the name"),
        ("model-wont-load", "", "the model qwen3:8b does not load"),
    ):
        (bench.state / flag).write_text(value)
        failed = bench.install("brain")
        assert failed.returncode == 1 and words in failed.stdout, (flag, failed.stdout)
        (bench.state / flag).unlink()
    done = bench.install("brain")
    assert done.returncode == 0, done.stdout


def test_no_local_model_means_no_ollama(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(model="none"))
    assert done.returncode == 0, done.stdout
    assert "no local model was asked for" in done.stdout
    assert not any("ollama" in call for call in bench.calls("curl")) and state(bench, "ollama-starts") == ""
    assert bench.sh("bash /opt/jarvis/install/jarvis-install.sh --check").returncode == 0


def test_the_models_are_the_ones_named(bench):
    bench.release("v0.1.0")
    done = bench.install("brain", bench.answers(model="llama3.2:3b", embed_model="none"))
    assert done.returncode == 0, done.stdout
    assert state(bench, "ollama-models") == "llama3.2:3b\n" and state(bench, "ollama-loaded") == "llama3.2:3b"
    assert "JARVIS_LOCAL_MODEL=llama3.2:3b\n" in bench.read("/etc/jarvis/site.env")
    # A model without a tag is the one Ollama calls latest.
    named = bench.install("brain", bench.answers(model="mistral"))
    assert named.returncode == 0, named.stdout
    assert state(bench, "ollama-models") == "llama3.2:3b\nmistral:latest\n"


def test_names_that_are_not_model_names_are_refused(bench):
    bench.release("v0.1.0")
    for bad in ("qwen3:8b; rm -rf /", "Qwen3", "../x", 'a"b', "x:", ""):
        refused = bench.install("brain", bench.answers(model=bad))
        assert refused.returncode == 1 and "these settings are missing or not valid" in refused.stdout, bad
    assert bench.calls("pct create") == []
    # Written into the settings file by hand, the container refuses it as well.
    assert bench.install("brain").returncode == 0
    bench.sh("sed -i 's/^JARVIS_LOCAL_MODEL=.*/JARVIS_LOCAL_MODEL=x$(id)/' /etc/jarvis/site.env")
    refused = bench.sh("bash /opt/jarvis/install/jarvis-install.sh")
    assert refused.returncode == 1 and "must be a model as Ollama names it" in refused.stdout


def test_the_check_notices_what_went_missing(bench):
    bench.release("v0.1.0")
    assert bench.install("brain").returncode == 0
    check = "bash /opt/jarvis/install/jarvis-install.sh --check"
    for break_it, words in (
        ("echo '# changed' >> /etc/systemd/system/ollama.service", "/etc/systemd/system/ollama.service is not the one of this release"),
        ("rm /usr/local/lib/ollama/.jarvis-release", "Ollama is not the release this version of Jarvis names (v9.9.9)"),
    ):
        bench.sh(break_it)
        broken = bench.sh(check)
        assert broken.returncode == 1 and words in broken.stdout, broken.stdout
        assert bench.sh("update --repair").returncode == 0
        assert bench.sh(check).returncode == 0
    (bench.state / "ollama-models").write_text("qwen3:8b\n")
    broken = bench.sh(check)
    assert broken.returncode == 1 and "the model qwen3-embedding:0.6b is not there" in broken.stdout
    (bench.state / "ollama-down").write_text("")
    broken = bench.sh(check)
    assert broken.returncode == 1 and "Ollama does not answer on http://127.0.0.1:11434" in broken.stdout
