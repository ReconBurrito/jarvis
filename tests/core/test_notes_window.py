"""The notes window: what it is given, what it may save, and who may ask. The notes are invented."""
import base64
import json

from fakes import run
from test_notes import SEED, git, origin  # noqa: F401  (origin is a fixture)
from test_server import ORIGIN, PANEL, Bench

from jarvis.notes.paths import version


def bench(tmp_path, origin):  # noqa: F811
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key = base64.b64encode(("-----" + "BEGIN OPENSSH PRIVATE " + "KEY-----\nmade up\n").encode()).decode()
    (secrets / "lab.env").write_text(f"JARVIS_NOTES_REPO={origin}\nJARVIS_NOTES_DEPLOY_KEY={key}\n")
    made = Bench(tmp_path, env_dir=secrets, secrets_mode="plain", embed_model="none")
    assert made.jarvis.lab == {"notes": ""}
    return made


def put(made, body):
    return made.ask("PUT", "/api/notes/note", headers=dict(PANEL, origin=ORIGIN), json=body)


def test_the_window_lists_and_opens_the_notes(tmp_path, origin):  # noqa: F811
    made = bench(tmp_path, origin)
    listed = made.ask("GET", "/api/notes").json()
    assert [n["path"] for n in listed["notes"]] == sorted(SEED) and listed["detail"].startswith("main at ")
    flags = {n["path"]: (n["protected"], n["read_only"]) for n in listed["notes"]}
    assert flags["USER.md"] == (True, False) and flags["raw/2025/2025-01-01-filed.md"] == (False, True) and flags["wiki/garden.md"] == (False, False)
    opened = made.ask("GET", "/api/notes/note", params={"path": "wiki/garden.md"}).json()
    assert opened == {"path": "wiki/garden.md", "text": SEED["wiki/garden.md"], "version": version(SEED["wiki/garden.md"]),
                      "protected": False, "read_only": False, "lossy": False}
    for bad in ("../origin.git/config", ".git/config", "wiki/missing.md", "notes.txt"):
        assert made.ask("GET", "/api/notes/note", params={"path": bad}).status_code == 404, bad


def test_the_owner_saves_in_their_own_name_and_a_change_made_meanwhile_is_never_overwritten(tmp_path, origin):  # noqa: F811
    made = bench(tmp_path, origin)
    made.ask("GET", "/api/notes")
    base = version(SEED["wiki/garden.md"])
    saved = put(made, {"path": "wiki/garden.md", "text": "# Garden\n\nThe tomatoes are watered every evening now.", "base": base})
    assert saved.status_code == 200, saved.text
    assert git("log", "-1", "--format=%an|%cn|%s", "main", cwd=origin) == "Owner|Jarvis|edit: wiki/garden.md"
    assert git("show", "main:wiki/garden.md", cwd=origin).endswith("every evening now.")
    again = put(made, {"path": "wiki/garden.md", "text": "an edit from an old copy", "base": base})
    assert again.status_code == 409 and again.json()["conflict"] is True and "changed since you opened it" in again.json()["error"]
    # The owner may change a standing file directly; a new note is made with an empty base; sources stay as filed.
    assert put(made, {"path": "USER.md", "text": "# Owner\n\n- Wants short answers.\n- Reads in the morning.\n",
                      "base": version(SEED["USER.md"])}).status_code == 200
    assert put(made, {"path": "wiki/new.md", "text": "# New\n", "base": ""}).status_code == 200
    assert put(made, {"path": "wiki/new.md", "text": "# Again\n", "base": ""}).status_code == 409, "it exists now"
    raw = put(made, {"path": "raw/2025/2025-01-01-filed.md", "text": "changed", "base": version(SEED["raw/2025/2025-01-01-filed.md"])})
    assert raw.status_code == 400 and "never edited" in raw.json()["error"]
    secret = put(made, {"path": "wiki/garden.md", "text": "password: abcdefghijklmnopqrstu", "base": ""})
    assert secret.status_code in (400, 409)
    kinds = [r["kind"] for r in made.records() if r["kind"].startswith("note_")]
    assert kinds.count("note_saved") == 3 and "note_save_failed" in kinds
    log = made.settings.audit_path.read_text()
    assert "evening" not in log and "Reads in the morning" not in log, "the audit keeps the size and fingerprint, never the text"


def test_only_the_panel_may_save(tmp_path, origin):  # noqa: F811
    made = bench(tmp_path, origin)
    body = {"path": "wiki/garden.md", "text": "x", "base": version(SEED["wiki/garden.md"])}
    from_elsewhere = made.ask("PUT", "/api/notes/note", headers={"sec-fetch-site": "cross-site", "host": PANEL["host"],
                                                                "origin": "https://example.org"}, json=body)
    no_origin = made.ask("PUT", "/api/notes/note", headers=PANEL, json=body)
    as_form = made.ask("PUT", "/api/notes/note", headers=dict(PANEL, origin=ORIGIN), content="path=wiki/garden.md")
    assert from_elsewhere.status_code == no_origin.status_code == as_form.status_code == 403
    assert made.ask("PUT", "/api/notes/note", headers=dict(PANEL, origin=ORIGIN), json={"path": 3, "text": "x"}).status_code == 400
    assert git("log", "--format=%s", "main", cwd=origin) == "seed"


def test_without_notes_the_window_is_told_why(tmp_path):
    made = Bench(tmp_path)
    answer = made.ask("GET", "/api/notes")
    assert answer.status_code == 503 and "Jarvis has no notes yet" in answer.json()["error"]


def test_the_window_is_served_like_the_panel(tmp_path):
    made = Bench(tmp_path)
    page = made.ask("GET", "/panel/notes.html")
    assert page.status_code == 200 and '<script src="markdown.js"></script>' in page.text
    for name in ("notes.js", "notes.css", "markdown.js"):
        assert made.ask("GET", f"/panel/{name}").status_code == 200, name
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


# ---------------------------------------------------------------- found by review

def test_a_change_merged_in_from_elsewhere_is_returned_so_the_next_save_cannot_drop_it(tmp_path, origin):  # noqa: F811
    made = bench(tmp_path, origin)
    made.ask("GET", "/api/notes")
    other = tmp_path / "other"
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    (other / "wiki" / "printer.md").write_text(SEED["wiki/printer.md"] + "\nOrdered in spring.\n")
    git("commit", "-q", "-am", "elsewhere", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    # The change lands on the remote just after the save looked (its pull happened before): the save rebases onto it.
    async def looked_already():
        return None
    made.jarvis.notes.start = looked_already
    text = SEED["wiki/printer.md"].replace("two paper trays", "three paper trays")
    saved = put(made, {"path": "wiki/printer.md", "text": text, "base": version(SEED["wiki/printer.md"])})
    assert saved.status_code == 200, saved.text
    now = saved.json()
    assert "three paper trays" in now["text"] and "Ordered in spring." in now["text"] and now["version"] == version(now["text"])
    assert git("show", "main:wiki/printer.md", cwd=origin) + "\n" == now["text"]


def test_a_save_that_cannot_reach_the_remote_changes_nothing(tmp_path, origin):  # noqa: F811
    import os
    made = bench(tmp_path, origin)
    made.ask("GET", "/api/notes")
    os.rename(origin, tmp_path / "away.git")
    failed = put(made, {"path": "wiki/garden.md", "text": "# Garden\n\nLost?\n", "base": version(SEED["wiki/garden.md"])})
    assert failed.status_code == 400 and failed.json()["error"].startswith("nothing was saved")
    repo = made.jarvis.notes
    assert repo.read("wiki/garden.md") == SEED["wiki/garden.md"] and git("log", "--format=%s", cwd=repo.path) == "seed"
    os.rename(tmp_path / "away.git", origin)
    assert put(made, {"path": "wiki/garden.md", "text": "# Garden\n\nSaved.\n", "base": version(SEED["wiki/garden.md"])}).status_code == 200


def test_a_note_that_is_not_plain_text_opens_read_only_and_errors_name_no_paths(tmp_path, origin):  # noqa: F811
    made = bench(tmp_path, origin)
    made.ask("GET", "/api/notes")
    (made.jarvis.notes.path / "wiki" / "odd.md").write_bytes(b"# Odd\n\xff\xfe\n")
    opened = made.ask("GET", "/api/notes/note", params={"path": "wiki/odd.md"}).json()
    assert opened["lossy"] is True and opened["read_only"] is True
    refused = put(made, {"path": "wiki/odd.md", "text": "x", "base": opened["version"]})
    assert refused.status_code == 400 and "not plain UTF-8" in refused.json()["error"]
    blocked = put(made, {"path": "README.md/inside.md", "text": "x", "base": ""})
    assert blocked.status_code == 400 and str(tmp_path) not in blocked.text
    assert str(tmp_path) not in made.settings.audit_path.read_text()


def test_a_failed_send_after_a_merge_leaves_a_clean_copy_that_jarvis_can_still_write_to(tmp_path, origin):  # noqa: F811
    made = bench(tmp_path, origin)
    made.ask("GET", "/api/notes")
    other = tmp_path / "other"
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    (other / "wiki" / "backups.md").write_text(SEED["wiki/backups.md"] + "\nChecked weekly.\n")
    git("commit", "-q", "-am", "elsewhere", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused here >&2\nexit 1\n")
    hook.chmod(0o755)

    async def looked_already():
        return None
    repo = made.jarvis.notes
    start, repo.start = repo.start, looked_already
    failed = put(made, {"path": "wiki/garden.md", "text": "# Garden\n\nMine.\n", "base": version(SEED["wiki/garden.md"])})
    assert failed.status_code == 400 and failed.json()["error"] == (
        "nothing was saved: the notes could not be brought up to date with the remote or sent to it")
    assert str(origin) not in made.settings.audit_path.read_text()
    assert git("status", "--porcelain", cwd=repo.path) == "", "nothing half done is left in the copy"
    assert repo.read("wiki/garden.md") == SEED["wiki/garden.md"] and "Checked weekly." in repo.read("wiki/backups.md")
    hook.unlink()
    repo.start = start
    run(repo.edit("wiki/backups.md", lambda text: text + "And monthly.\n", "jarvis: add"))
    assert "And monthly." in git("show", "main:wiki/backups.md", cwd=origin)
