"""Age identities and vault files for the tests, made with the real age (tests/stubs/sops opens them)."""
import os
import shutil
import subprocess
from pathlib import Path

STUBS = Path(__file__).resolve().parents[1] / "stubs"
SOPS = str(STUBS / "sops")


def identity(folder: Path, name: str = "age.key") -> tuple[Path, str]:
    """A new identity in FOLDER, and its public key written beside it as jarvis-unlock does."""
    folder.mkdir(parents=True, exist_ok=True)
    key = folder / name
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    public = subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout.strip()
    (folder / "recipient").write_text(public + "\n")
    return key, public


def vault_file(path: Path, values: dict[str, str], *recipients: str) -> Path:
    plain = "".join(f"{key}={value}\n" for key, value in values.items())
    args = ["age", "-a"]
    for recipient in recipients:
        args += ["-r", recipient]
    armoured = subprocess.run(args, input=plain, check=True, capture_output=True, text=True).stdout
    head = "".join(f"sops_age__list_{n}__map_recipient={recipient}\n" for n, recipient in enumerate(recipients))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + "sops_version=3.13.3\n" + armoured)
    return path


# The real sops, where the machine running the tests has it (the one this release pins, ideally).
REAL_SOPS = next((path for path in (os.environ.get("JARVIS_TEST_SOPS", ""), "/usr/local/bin/sops", shutil.which("sops") or "")
                  if path and os.access(path, os.X_OK) and "stubs" not in path), "")


def real_vault_file(path: Path, values: dict[str, str], *recipients: str) -> Path:
    """A vault file as the owner's sops makes it."""
    plain = path.parent / (path.name + ".plain")
    path.parent.mkdir(parents=True, exist_ok=True)
    plain.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    done = subprocess.run([REAL_SOPS, "encrypt", "--age", ",".join(recipients), "--input-type", "dotenv", "--output-type", "dotenv",
                           str(plain)], check=True, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(path.parent)})
    plain.unlink()
    path.write_text(done.stdout)
    return path
