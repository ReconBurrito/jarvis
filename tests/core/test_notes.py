"""Jarvis's notes: the repository, search by words and by meaning, and the two tools that write.

Every note here is invented. A bare repository in a temporary folder stands in for the remote.
"""
import json
import os
import subprocess

import httpx
import pytest
from fakes import run

from jarvis.audit import AuditLog
from jarvis.notes.repo import BrainError, BrainRepo
from jarvis.notes.search import search
from jarvis.notes.semantic import Embeddings, hybrid_search
from jarvis.notes.tools import make_notes_tools
from jarvis.notes.write import add_text, find_exact, make_note_write_tools, proposal_name, replace_text
from jarvis.tools import ToolRegistry

SEED = {
    "README.md": "# Notes\n\nWhat Jarvis knows, written down.\n",
    "USER.md": "# Owner\n\n- Wants short answers.\n",
    "MEMORY.md": "# Memory\n\n- The printer is on the second floor.\n",
    "SOUL.md": "# Soul\n\nDry, calm, exact.\n",
    "wiki/printer.md": "# Printer\n\nThe printer has two paper trays.\n\n## Toner\n\nBlack only.\n",
    "wiki/backups.md": "# Backups\n\nThe backup job runs every night and keeps a week of copies.\n",
    "wiki/garden.md": "# Garden\n\nThe tomatoes are watered every morning.\n",
    "raw/2025/2025-01-01-filed.md": "source: typed\n\nThe first note ever filed.\n",
}


def git(*args, cwd):
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"}
    done = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare repository standing in for the remote, holding the invented notes above."""
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
    for rel, text in SEED.items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text)
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    git("push", "-q", str(bare), "main", cwd=seed)
    return bare


def make_repo(tmp_path, origin, **kwargs):
    repo = BrainRepo(tmp_path / "state" / "notes" / "repo", str(origin), **kwargs)
    run(repo.start())
    assert repo.ready, repo.detail
    return repo


def make(tmp_path, origin, **kwargs):
    repo = make_repo(tmp_path, origin, **kwargs)
    audit = AuditLog(tmp_path / "audit.jsonl")
    tools = ToolRegistry(audit)
    for tool in make_notes_tools(repo) + make_note_write_tools(repo, audit):
        tools.register(tool)
    return repo, tools


def call(tools, name, **arguments):
    return run(tools.dispatch("owner:s1", name, arguments))


def records(tmp_path, kind):
    return [r["data"] for r in map(json.loads, (tmp_path / "audit.jsonl").read_text().splitlines()) if r["kind"] == kind]


def refused(function, *args):
    try:
        function(*args)
    except Exception as exc:
        return str(exc)
    raise AssertionError("accepted")


# ---------------------------------------------------------------- the repository

def test_the_notes_are_cloned_read_and_kept_inside_their_folder(tmp_path, origin):
    repo = make_repo(tmp_path, origin)
    assert repo.detail.startswith("main at ")
    assert repo.read("wiki/printer.md") == SEED["wiki/printer.md"]
    assert repo.notes() == sorted(SEED)
    for bad in ("../origin.git/HEAD", "/etc/passwd", ".git/config", "wiki/../../seed/README.md", "a\\b.md", ""):
        assert "not a note path" in refused(repo.resolve, bad), bad


def test_a_second_start_brings_what_changed_elsewhere_and_a_lost_remote_leaves_a_readable_copy(tmp_path, origin):
    repo = make_repo(tmp_path, origin)
    other = tmp_path / "other"
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    (other / "wiki" / "garden.md").write_text("# Garden\n\nThe beans need a stake.\n")
    git("commit", "-q", "-am", "beans", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    run(repo.start())
    assert "beans" in repo.read("wiki/garden.md")
    os.rename(origin, tmp_path / "gone.git")
    run(repo.start())
    assert repo.ready and repo.detail.startswith("local copy only:") and "beans" in repo.read("wiki/garden.md")


def test_no_remote_at_all_is_said(tmp_path):
    repo = BrainRepo(tmp_path / "repo", "")
    run(repo.start())
    assert not repo.ready and repo.detail == "no notes repository is set up"


# ---------------------------------------------------------------- searching and reading

def test_keyword_search_finds_the_right_section_and_names_its_note():
    found = search(SEED, "how many paper trays does the printer have")
    assert found[0]["path"] == "wiki/printer.md" and found[0]["heading"] == "Printer" and "two paper trays" in found[0]["text"]
    assert search(SEED, "the of and") == []


TOPICS = {"copies": (1, 0, 0), "backup": (1, 0, 0), "saved": (1, 0, 0), "tomato": (0, 1, 0), "plants": (0, 1, 0),
          "toner": (0, 0, 1), "ink": (0, 0, 1)}


def meaning(text):
    vector = [0.01, 0.01, 0.01]
    for word, topic in TOPICS.items():
        if word in text.lower():
            vector = [v + t for v, t in zip(vector, topic)]
    return vector


class FakeEmbed:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(body["input"])
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "model not found"})
        return httpx.Response(200, json={"embeddings": [meaning(t) for t in body["input"]]})


def embeddings(tmp_path, fake):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return Embeddings("http://ollama.invalid", "an-embedding-model", tmp_path / "vectors.json", client=client)


def test_a_question_in_other_words_is_found_by_meaning_and_vectors_are_kept(tmp_path):
    fake = FakeEmbed()
    found, how = run(hybrid_search(SEED, "when do the plants get water", embeddings(tmp_path, fake)))
    assert how == "words and meaning" and found[0]["path"] == "wiki/garden.md" and "meaning" in found[0]["matched"]
    first = sum(len(batch) for batch in fake.calls)
    again = FakeEmbed()
    run(hybrid_search(SEED, "is anything saved", embeddings(tmp_path, again)))
    assert sum(len(batch) for batch in again.calls) == 1, "only the question is embedded again; the notes' vectors were kept"
    assert first > 1 and (tmp_path / "vectors.json").exists()


def test_without_the_model_search_falls_back_to_words_and_says_so(tmp_path):
    found, how = run(hybrid_search(SEED, "printer trays", embeddings(tmp_path, FakeEmbed(status=404))))
    assert how.startswith("words only: meaning search unavailable") and found[0]["path"] == "wiki/printer.md"


def test_the_read_tools_name_their_notes(tmp_path, origin):
    _, tools = make(tmp_path, origin)
    found = call(tools, "notes_search", query="paper trays")
    assert found["results"][0]["path"] == "wiki/printer.md" and found["search"] == "words"
    assert call(tools, "notes_read", path="wiki/garden.md")["text"] == SEED["wiki/garden.md"]
    assert "not a note path" in call(tools, "notes_read", path="../origin.git/config")["error"]


# ---------------------------------------------------------------- writing

def test_adding_and_replacing_place_the_text_exactly():
    note = SEED["wiki/printer.md"]
    assert add_text(note, "It is serviced in spring.") == note + "\nIt is serviced in spring.\n"
    assert add_text(note, "Colour later, maybe.", "toner") == note + "\nColour later, maybe.\n"
    assert add_text(note, "Paper is A4.", "Printer") == note.replace("trays.\n\n## Toner", "trays.\n\nPaper is A4.\n\n## Toner")
    assert add_text("# List\n\n- one\n", "- two") == "# List\n\n- one\n- two\n"
    assert "the headings are: Printer, Toner" in refused(add_text, note, "x", "Cables")
    assert replace_text("- a: on\n- b: on\n", "- a: on", "- a: off") == "- a: off\n- b: on\n"
    assert "2 times" in refused(replace_text, "- a: on\n- b: on\n", ": on", "x")
    assert "not in the note" in refused(replace_text, note, "four trays", "x")


def test_a_note_is_named_by_its_path_or_its_file_name_and_nothing_looser():
    notes = sorted(SEED)
    for wanted, path in (("printer", "wiki/printer.md"), ("the printer note", "wiki/printer.md"), ("wiki/printer", "wiki/printer.md"),
                         ("USER", "USER.md"), ("user", "USER.md"), ("the readme", "README.md")):
        assert find_exact(wanted, notes) == (path, []), wanted
    for wanted in ("print", "", "the note", "garde"):
        assert find_exact(wanted, notes)[0] is None, wanted


def test_an_ordinary_note_is_changed_on_main_in_jarviss_name_and_the_text_stays_out_of_the_audit(tmp_path, origin):
    repo, tools = make(tmp_path, origin)
    done = call(tools, "note_add", note="the printer note", text="It is serviced in spring.")
    assert done["done"] is True and done["path"] == "wiki/printer.md"
    assert git("log", "-1", "--format=%an <%ae>|%s", "main", cwd=origin) == "Jarvis <jarvis@localhost>|jarvis: add in wiki/printer.md"
    replaced = call(tools, "note_replace", note="wiki/printer.md", find="two paper trays", replace="three paper trays")
    assert replaced["done"] is True and "three paper trays" in git("show", "main:wiki/printer.md", cwd=origin)
    log = (tmp_path / "audit.jsonl").read_text()
    assert "serviced" not in log and "three paper" not in log
    assert [c["how"] for c in records(tmp_path, "note_changed")] == ["add", "replace"]


def test_a_standing_file_is_only_ever_proposed(tmp_path, origin):
    repo, tools = make(tmp_path, origin)
    first = call(tools, "note_add", note="user", text="- Prefers the morning.")
    assert first["done"] is False and first["proposed"] is True and first["branch"] == "proposal/note-user"
    assert git("show", "main:USER.md", cwd=origin) + "\n" == SEED["USER.md"]
    assert "- Prefers the morning." in git("show", "proposal/note-user:USER.md", cwd=origin)
    assert proposal_name("skills/a-b.md") != proposal_name("skills/a/b.md")


def test_what_the_note_tools_refuse(tmp_path, origin):
    repo, tools = make(tmp_path, origin, has_secret=lambda text: "vault-value-0123456789" in text)
    assert "raw source" in call(tools, "note_add", note="raw/2025/2025-01-01-filed.md", text="x")["error"]
    assert "looks as if it holds a secret" in call(tools, "note_add", note="printer", text="password: abcdefghijklmnopqrst")["error"]
    assert "looks as if it holds a secret" in call(tools, "note_add", note="printer", text="it is vault-value-0123456789")["error"]
    assert "there is no note called" in call(tools, "note_add", note="nothing-like-it", text="x")["error"]
    assert "that would leave the note empty" in call(tools, "note_replace", note="garden", find=SEED["wiki/garden.md"], replace="")["error"]
    assert git("log", "--format=%s", "main", cwd=origin) == "seed", "nothing was written"
    os.symlink(repo.path / "USER.md", repo.path / "wiki" / "link.md")
    assert "link" in call(tools, "note_add", note="wiki/link.md", text="x")["error"]


# ---------------------------------------------------------------- found by review

def test_a_link_waiting_on_a_proposal_branch_is_never_written_through(tmp_path, origin):
    repo, tools = make(tmp_path, origin)
    target = tmp_path / "outside.txt"
    target.write_text("untouched\n")
    other = tmp_path / "other"
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    git("checkout", "-q", "-b", "proposal/note-user", cwd=other)
    os.remove(other / "USER.md")
    os.symlink(target, other / "USER.md")
    git("add", "-A", cwd=other)
    git("commit", "-q", "-m", "a link where USER.md was", cwd=other)
    git("push", "-q", "origin", "proposal/note-user", cwd=other)
    result = call(tools, "note_add", note="user", text="- Something.")
    assert result["done"] is False and "error" in result
    assert target.read_text() == "untouched\n"
    assert not list(repo.path.parent.glob(".proposal-*")), "no scratch left behind"


def test_a_proposal_is_built_without_checking_anything_out(tmp_path, origin):
    repo, tools = make(tmp_path, origin)
    call(tools, "note_add", note="soul", text="Patient.")
    assert git("show", "proposal/note-soul:SOUL.md", cwd=origin).endswith("Patient.")
    assert git("show", "proposal/note-soul:wiki/garden.md", cwd=origin) + "\n" == SEED["wiki/garden.md"], "the rest of main came along"
    assert git("log", "-1", "--format=%an|%P", "proposal/note-soul", cwd=origin).split("|")[1] == git("rev-parse", "main", cwd=origin)
    assert git("status", "--porcelain", cwd=repo.path) == "" and git("worktree", "list", "--porcelain", cwd=repo.path).count("worktree ") == 1


def test_a_clone_of_another_repository_is_neither_read_nor_written(tmp_path, origin):
    make_repo(tmp_path, origin)
    elsewhere = tmp_path / "elsewhere.git"
    git("clone", "-q", "--bare", str(origin), str(elsewhere), cwd=tmp_path)
    moved = BrainRepo(tmp_path / "state" / "notes" / "repo", str(elsewhere))
    run(moved.start())
    assert not moved.ready and "holds a clone of another repository" in moved.detail


def test_a_path_that_climbs_back_is_judged_as_where_it_lands(tmp_path, origin):
    repo = make_repo(tmp_path, origin)
    for sneaky in ("wiki/../SOUL.md", "wiki/../raw/2025/2025-01-01-filed.md", "./README.md"):
        with pytest.raises(BrainError, match="not a note path"):
            run(repo.save({sneaky: "changed"}, "sneaky"))
    assert git("log", "--format=%s", "main", cwd=origin) == "seed"


def test_once_closed_the_notes_never_start_again(tmp_path, origin):
    repo = make_repo(tmp_path, origin)
    run(repo.close())
    run(repo.start())
    assert not repo.ready and repo.detail == "closed" and repo._key == b""


def test_only_a_repository_over_ssh_or_a_full_folder_path_is_taken(tmp_path):
    for remote in ("notes.git", "--upload-pack=touch x", "https://example.org/notes.git", "github.com:you/notes.git"):
        repo = BrainRepo(tmp_path / "r", remote)
        run(repo.start())
        assert not repo.ready and "must be a repository over SSH" in repo.detail, remote
    ssh = BrainRepo(tmp_path / "r", "git@example.org:you/notes.git")
    run(ssh.start())
    assert not ssh.ready and "the deploy key or the pinned host keys are missing" in ssh.detail


def test_any_user_at_host_remote_is_reached_over_ssh(tmp_path):
    from jarvis.notes.repo import is_ssh_remote
    for remote in ("git@github.com:you/notes.git", "deploy@example.org:notes.git", "ssh://git@example.org/notes.git"):
        assert is_ssh_remote(remote), remote
    for remote in ("/srv/notes.git", "notes.git", "github.com:you/notes.git", "ext::sh -c id", "https://example.org/n.git"):
        assert not is_ssh_remote(remote), remote


# ---------------------------------------------------------------- notes out of the box

def test_notes_kept_on_this_machine_begin_by_themselves_and_are_written_without_a_remote(tmp_path):
    repo = BrainRepo(tmp_path / "state" / "notes" / "repo", "", local=True)
    run(repo.start())
    assert repo.ready and repo.detail.endswith("(kept on this machine only)"), repo.detail
    assert repo.notes() == ["README.md"] and "Jarvis's notes" in repo.read("README.md")
    commit = run(repo.save({"wiki/printer.md": "# Printer\n\nTwo trays.\n"}, "printer"))
    assert commit and "Two trays" in repo.read("wiki/printer.md") and not repo.can_push
    assert not list((tmp_path / "state" / "notes").glob(".begin-*")), "nothing half made is left beside it"
    run(repo.start())   # a second start leaves them as they are
    assert repo.ready and "Two trays" in repo.read("wiki/printer.md")


def test_an_empty_remote_gets_the_first_note_and_is_used(tmp_path):
    bare = tmp_path / "empty.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    repo = BrainRepo(tmp_path / "state" / "notes" / "repo", str(bare))
    run(repo.start())
    assert repo.ready and repo.detail.startswith("main at") and "this machine only" not in repo.detail, repo.detail
    assert "Jarvis's notes" in git("show", "main:README.md", cwd=bare)


def test_a_remote_without_main_is_said(tmp_path):
    bare, seed = tmp_path / "other.git", tmp_path / "seed"
    git("init", "-q", "--bare", "-b", "trunk", str(bare), cwd=tmp_path)
    git("init", "-q", "-b", "trunk", str(seed), cwd=tmp_path)
    (seed / "a.md").write_text("a\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "a", cwd=seed)
    git("push", "-q", str(bare), "trunk", cwd=seed)
    repo = BrainRepo(tmp_path / "state" / "notes" / "repo", str(bare))
    run(repo.start())
    assert not repo.ready and "has no branch main" in repo.detail


def test_notes_kept_on_this_machine_move_to_a_remote_named_later_when_it_is_empty(tmp_path, origin):
    place = tmp_path / "state" / "notes" / "repo"
    local = BrainRepo(place, "", local=True)
    run(local.start())
    run(local.save({"wiki/printer.md": "# Printer\n\nTwo trays.\n"}, "printer"))
    head = git("rev-parse", "main", cwd=place)
    # A remote with notes of its own: nothing is mixed, and the owner is told what to do.
    clash = BrainRepo(place, str(origin))
    run(clash.start())
    assert not clash.ready and "already has notes of its own" in clash.detail
    assert git("rev-parse", "main", cwd=place) == head
    # An empty one: the notes go there, history and all, and are kept in step from then on.
    empty = tmp_path / "empty.git"
    git("init", "-q", "--bare", "-b", "main", str(empty), cwd=tmp_path)
    moved = BrainRepo(place, str(empty))
    run(moved.start())
    assert moved.ready and git("rev-parse", "main", cwd=empty) == head
    run(moved.save({"wiki/garden.md": "# Garden\n\nBeans.\n"}, "garden"))
    assert "Beans" in git("show", "main:wiki/garden.md", cwd=empty)


def test_jarvis_keeps_its_notes_on_its_own_machine_when_the_vault_names_none(tmp_path):
    from fakes import FakeOllama

    from jarvis.config import Settings
    from jarvis.core import Jarvis

    def jarvis(local):
        config = Settings(data_dir=tmp_path / f"data-{local}", site=tmp_path / "site.env", env_dir=tmp_path / "secrets",
                          trust_dir=tmp_path / "trust", notes_local=local)
        return Jarvis(config, FakeOllama().client())

    off = jarvis(False)
    assert off.lab.get("notes") != "" and not any(n.startswith("notes_") for n in off.tools.names())
    run(off.close())
    on = jarvis(True)
    assert on.lab.get("notes") == "" and {"notes_search", "notes_read", "note_add"} <= set(on.tools.names())

    async def started():
        await on.notes.start()
        return on.notes.ready, on.notes.detail

    ready, detail = run(started())
    assert ready and detail.endswith("(kept on this machine only)"), detail
    run(on.close())


def test_a_move_to_a_remote_that_fails_is_tried_again_and_never_mixes_two_histories(tmp_path):
    place = tmp_path / "state" / "notes" / "repo"
    local = BrainRepo(place, "", local=True)
    run(local.start())
    run(local.save({"wiki/a.md": "# A\n\nmine\n"}, "a"))
    empty = tmp_path / "empty.git"
    git("init", "-q", "--bare", "-b", "main", str(empty), cwd=tmp_path)
    hook = empty / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    refused = BrainRepo(place, str(empty))
    run(refused.start())
    assert not refused.ready or refused.detail.startswith("local copy only")
    assert subprocess.run(["git", "-C", str(place), "config", "--get", "remote.origin.url"], capture_output=True).returncode != 0, \
        "a push that failed sets no origin"
    # Someone else's notes land there meanwhile: they are not mixed with these.
    other = tmp_path / "other"
    git("init", "-q", "-b", "main", str(other), cwd=tmp_path)
    (other / "theirs.md").write_text("theirs\n")
    git("add", "-A", cwd=other)
    git("commit", "-q", "-m", "theirs", cwd=other)
    hook.unlink()
    git("push", "-q", str(empty), "main", cwd=other)
    again = BrainRepo(place, str(empty))
    run(again.start())
    assert not again.ready and "already has notes of its own" in again.detail
    assert "mine" not in git("log", "--all", "--format=%s", cwd=empty) and git("log", "--format=%s", cwd=empty) == "theirs"


def test_notes_from_another_history_are_never_pulled_into_a_clone(tmp_path, origin):
    repo = make_repo(tmp_path, origin)
    stranger = tmp_path / "stranger"
    git("init", "-q", "-b", "main", str(stranger), cwd=tmp_path)
    (stranger / "x.md").write_text("x\n")
    git("add", "-A", cwd=stranger)
    git("commit", "-q", "-m", "x", cwd=stranger)
    git("push", "-q", "--force", str(origin), "main", cwd=stranger)   # the remote's main replaced by an unrelated one
    with pytest.raises(BrainError, match="no history in common"):
        run(repo.save({"wiki/b.md": "# B\n\nb\n"}, "b"))
    assert git("log", "--format=%s", cwd=origin) == "x", "nothing was pushed onto it"


def test_a_clone_the_vault_no_longer_names_is_not_taken_for_notes_kept_here(tmp_path, origin):
    make_repo(tmp_path, origin)
    alone = BrainRepo(tmp_path / "state" / "notes" / "repo", "", local=True)
    run(alone.start())
    assert not alone.ready and "no longer names" in alone.detail
