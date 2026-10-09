"""The real service behind real TLS, and the real panel in a real Chromium whose only reason to trust the
brain is the rule the desktop's installer writes (tests/core/live_panel.py does the work, in a network and
a /etc of its own).

Needs root, unshare, ip, openssl, a Chromium (JARVIS_TEST_BROWSER, or the one Playwright installs) and a
python3 that has Playwright. Skipped where one of those is missing. JARVIS_TEST_PICTURES names a folder the
pictures of the panel are copied to.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BROWSER = os.environ.get("JARVIS_TEST_BROWSER") or next(
    (p for p in ("/opt/pw-browsers/chromium", shutil.which("chromium") or "", shutil.which("chromium-browser") or "")
     if p and os.access(p, os.X_OK)), "")
BRAIN = "https://192.0.2.20:8443"


@pytest.fixture(scope="module")
def seen(tmp_path_factory):
    tools = all(shutil.which(tool) for tool in ("unshare", "ip", "openssl", "python3"))
    if os.geteuid() != 0 or not tools or not BROWSER:
        pytest.skip("needs root, unshare, ip, openssl and a Chromium")
    if subprocess.run(["python3", "-c", "import playwright.sync_api"], capture_output=True).returncode != 0:
        pytest.skip("needs a python3 that has Playwright")
    tmp = tmp_path_factory.mktemp("live")
    if subprocess.run(["unshare", "-n", "-m", "bash", str(REPO / "tests" / "stubs" / "namespace.sh"), str(tmp / "probe"), "true"],
                      capture_output=True).returncode != 0:
        pytest.skip("a network and file system of its own cannot be made here")
    env = {key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")}
    done = subprocess.run(["unshare", "-n", "-m", "bash", str(REPO / "tests" / "stubs" / "namespace.sh"), str(tmp / "ns"),
                           "python3", str(REPO / "tests" / "core" / "live_panel.py"), sys.executable, str(REPO), str(tmp / "work"), BROWSER],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, env=env)
    assert done.returncode == 0, done.stderr[-3000:]
    pictures = os.environ.get("JARVIS_TEST_PICTURES")
    if pictures:
        Path(pictures).mkdir(parents=True, exist_ok=True)
        for picture in (tmp / "work").glob("*.png"):
            shutil.copy(picture, pictures)
    return json.loads(done.stdout)


def test_programs_get_in_by_address_and_by_what_they_say(seen):
    programs = seen["programs"]
    assert programs["desktop_as_panel"] == 200 and programs["desktop_without_saying_so"] == 403 and programs["stranger"] == 403
    assert programs["self_naming_the_desktop"] == 403, "a header naming another address is never believed"
    assert programs["desktop_cross_site_post"] == 403 and programs["desktop_cross_site_as_json"] == 403
    assert programs["desktop_under_another_name"] == 403
    assert programs["self_health"] == [200, '{"ok":true,"release":"v0.6.0"}'] and programs["self_state"] == 403
    assert programs["page_has_title"] and programs["headers"]["x-frame-options"] == "DENY"
    assert "script-src 'self'" in programs["headers"]["content-security-policy"] and "server" not in programs["headers"]
    assert seen["startup"] == {"listen": "0.0.0.0:8443", "model": "qwen3:8b", "panel_allow": ["192.0.2.21"], "release": "v0.6.0"}


def test_the_browser_trusts_the_brain_only_through_the_installers_rule(seen):
    assert seen["untrusted"] == {"url": "panel.html", "state": "The brain at 192.0.2.20:8443 does not answer. Trying again."}
    assert seen["panel"]["url"] == f"{BRAIN}/panel/"
    # And the brain's authority is believed for the brain's address alone: not for a web site's name, not
    # for another address, though it signed certificates for both.
    assert seen["authority_elsewhere"]["a_web_sites_name"].startswith("net::ERR_CERT_"), seen["authority_elsewhere"]
    assert seen["authority_elsewhere"]["another_address"].startswith("net::ERR_CERT_"), seen["authority_elsewhere"]


def test_the_start_page_becomes_the_panel_and_the_panel_shows_how_things_stand(seen):
    panel = seen["panel"]
    assert panel["state"] == "Ready." and panel["release"] == "v0.6.0" and panel["font"] is True
    assert panel["model"] == "qwen3:8b, resting" and panel["load"].endswith(f" of {os.cpu_count()}") and panel["up"] != "--"
    assert panel["empty"] is True and panel["send_disabled"] is True
    assert panel["vault"] == "locked" and panel["cluster"] == "locked"


def test_an_unlock_on_the_brain_reaches_the_panel_without_a_restart(seen):
    assert seen["unlocked"]["vault"] == "unlocked" and seen["unlocked"]["cluster"] == "does not answer"
    assert "no Proxmox node answered" in seen["unlocked"]["why"] and "s" * 36 not in seen["unlocked"]["why"]
    # Locked when the service started, unlocked while it ran, unlocked again when it was started anew.
    vaults = [kind for kind in seen["kinds"] if kind == "vault"]
    assert seen["kinds"][0] == "vault" and len(vaults) == 3
    assert seen["problems"] == [], "the panel's own script and style ran without a complaint from the browser"


def test_a_question_is_answered_and_shown_again_after_the_panel_was_reopened(seen):
    assert seen["answer"] == {"you": "How long has this machine been up?", "jarvis": "Ready.", "box": "", "button": "Send"}
    assert seen["after_reload"] == ["How long has this machine been up?", "Ready."]


def test_stop_ends_the_answer_here_and_in_the_brain(seen):
    assert seen["while_answering"] == {"state": "Thinking.", "new_disabled": True}
    assert seen["stopped"] == {"state": "Stopped. Ready.", "note": "Stopped.", "audit": "turn_aborted"}
    assert [kind for kind in seen["kinds"] if kind != "vault"][:5] == ["startup", "request_denied", "turn", "turn_aborted", "turn"]
    assert seen["new"] == {"empty": True, "state": "Ready."}


def test_a_second_window_follows_the_first(seen):
    second = seen["second_window"]
    assert second["said"] == 4 and second["send_disabled"] is True and "answering" in second["last"]
    assert second["after"] == {"last": "said jarvis stopped", "note": "Stopped.", "send_disabled": False}


def test_a_page_from_elsewhere_in_the_same_browser_gets_nothing_from_the_brain(seen):
    elsewhere = seen["elsewhere"]
    for attempt in ("read", "readQuietly", "post", "postAsThePanelWould", "script"):
        assert elsewhere[attempt] == "blocked", attempt
    assert elsewhere["ping"] == "opaque" and elsewhere["frame_shows_panel"] is False
    # What the page saw is the browser's doing as much as the brain's; that nothing happened in the brain is
    # the brain's own record.
    assert not {"turn", "turn_aborted", "turn_failed", "tool_call"} & set(elsewhere["new_records"]) and seen["kinds"].count("turn") == 2


def test_a_brain_that_goes_away_is_said_and_found_again(seen):
    assert seen["brain_away"] == {"state": "The brain does not answer. Trying again.", "send_disabled": True}
    # A request the guard refused may land between these while the browser retries; refusals are tested above.
    assert seen["brain_back"] == "Ready." and [kind for kind in seen["kinds"] if kind not in ("vault", "request_denied")][-4:] == \
        ["turn", "shutdown", "startup", "shutdown"]
