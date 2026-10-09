"""Settings come from site.env; the environment wins; a value that cannot be used is refused by name."""
from pathlib import Path

import pytest

from jarvis.config import Settings, SettingsError, read_site


def etc(tmp_path, site="", release=""):
    (tmp_path / "site.env").write_text(site)
    if release:
        (tmp_path / "release").write_text(release)
    return {"JARVIS_ETC": str(tmp_path)}


def test_defaults_when_nothing_is_set(tmp_path):
    settings = Settings.load({"JARVIS_ETC": str(tmp_path / "absent")})
    assert settings.ollama_url == "http://127.0.0.1:11434" and settings.model == "qwen3:8b" and settings.has_model
    assert settings.audit_path == Path("/var/lib/jarvis/audit/audit.jsonl") and settings.audit_text is False
    assert settings.release == "" and settings.where == ""


def test_site_env_is_read_as_the_installer_writes_it(tmp_path):
    site = ("# written by the installer\nJARVIS_ROLE=brain\nJARVIS_LOCAL_MODEL=llama3.2:3b\nJARVIS_LOCAL_MODEL=mistral\n"
            "JARVIS_HOST_DESCRIPTION=container 201 on the node pve2\nNOT_OURS=1\nJARVIS_AUDIT_TEXT=on\n")
    settings = Settings.load(etc(tmp_path, site, "name=v0.5.0\ncommit=abc\n"))
    assert settings.model == "mistral" and settings.where == "container 201 on the node pve2"
    assert settings.audit_text is True and settings.release == "v0.5.0" and settings.site == tmp_path / "site.env"
    assert settings.installing is False
    (tmp_path / "release.pending").write_text("commit=def\n")
    assert Settings.load({"JARVIS_ETC": str(tmp_path)}).installing is True
    assert read_site(tmp_path / "site.env")["JARVIS_ROLE"] == "brain" and "NOT_OURS" not in read_site(tmp_path / "site.env")


def test_the_environment_wins_over_the_file(tmp_path):
    env = etc(tmp_path, "JARVIS_LOCAL_MODEL=mistral\nJARVIS_OLLAMA_URL=http://127.0.0.1:11434\n")
    env.update(JARVIS_LOCAL_MODEL="none", JARVIS_OLLAMA_URL="http://127.0.0.1:9/", JARVIS_DATA_DIR=str(tmp_path / "data"))
    settings = Settings.load(env)
    assert settings.model == "none" and not settings.has_model and settings.ollama_url == "http://127.0.0.1:9"
    assert settings.audit_path == tmp_path / "data" / "audit" / "audit.jsonl"


def test_values_that_cannot_be_used_are_refused_by_name(tmp_path):
    for key, value in (("JARVIS_OLLAMA_URL", "ftp://x"), ("JARVIS_OLLAMA_URL", "http://a b"), ("JARVIS_DATA_DIR", "data"),
                       ("JARVIS_AUDIT_TEXT", "maybe")):
        with pytest.raises(SettingsError, match=key):
            Settings.load({"JARVIS_ETC": str(tmp_path), key: value})
