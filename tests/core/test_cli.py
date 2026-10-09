"""The jarvis command: one question, the doctor, and the audit check, against a scripted Ollama."""
import asyncio
import io
import json
import signal

import pytest
from fakes import FakeOllama, run, text_chunks, tool_chunks

from jarvis import cli
from jarvis.audit import verify
from jarvis.config import Settings


def settings(tmp_path, **changes):
    values = {"data_dir": tmp_path / "data", "release": "v0.5.0", "site": tmp_path / "site.env", "where": "container 201"}
    values.update(changes)
    return Settings(**values)


def records(config):
    return [json.loads(line) for line in config.audit_path.read_text().splitlines()]


def ask(config, fake, text, **kwargs):
    out, notes = io.StringIO(), io.StringIO()

    async def go():
        async with fake.client() as client:
            return await cli.turn(cli.Jarvis(config, client, cli.SURFACE), "terminal", text, out, notes, **kwargs)

    return run(go()), out.getvalue(), notes.getvalue()


def test_one_question_gets_its_answer_and_the_notes_go_elsewhere(tmp_path):
    fake = FakeOllama([tool_chunks("local_status", {}), text_chunks("Up for ", "three hours, sir.")])
    last, out, notes = ask(settings(tmp_path), fake, "How long have you been up?")
    assert last["type"] == "done" and out == "Up for three hours, sir.\n"
    assert notes.splitlines()[0].endswith(" local_status]") and "[model-" not in notes
    assert notes.splitlines()[-1] == "[qwen3:8b, audit record 2, 40.0 tokens a second]"
    system = fake.requests[0]["messages"][0]["content"]
    assert "runs on container 201" in system and "typing to you in a terminal" in system
    assert [r["kind"] for r in records(settings(tmp_path))] == ["tool_call", "turn"]
    assert "user_text" not in records(settings(tmp_path))[1]["data"]


def test_a_model_that_is_away_is_said_plainly(tmp_path):
    last, out, notes = ask(settings(tmp_path), FakeOllama(down=True), "Hello?")
    assert last["type"] == "error" and out == "" and "jarvis: local: ollama unreachable: ConnectError" in notes
    assert records(settings(tmp_path))[-1]["kind"] == "turn_failed"


def test_what_was_said_is_kept_only_when_asked_for(tmp_path):
    config = settings(tmp_path, audit_text=True)
    ask(config, FakeOllama([text_chunks("Good evening.")]), "Hello.")
    assert records(config)[0]["data"]["user_text"] == "Hello." and records(config)[0]["data"]["reply_text"] == "Good evening."


def doctor(config, fake, quick=False, release_check=False):
    out = io.StringIO()

    async def go():
        async with fake.client() as client:
            return await cli.doctor(config, quick, out, client, release_check=release_check)

    return run(go()), out.getvalue()


def test_the_doctor_asks_the_model_one_question(tmp_path):
    config = settings(tmp_path)
    code, said = doctor(config, FakeOllama([text_chunks("Ready.")]))
    assert code == 0, said
    lines = said.splitlines()
    assert lines[0] == f"   ok: Jarvis v0.5.0; settings from {tmp_path / 'site.env'}"
    assert "no records yet" in lines[1] and "local_status reads this machine" in lines[2]
    assert lines[3].startswith("   ok: qwen3:8b answered in ") and lines[3].endswith("(40.0 tokens a second); audit record 1")
    assert records(config)[0]["data"]["user"] == "doctor" and verify(config.audit_path)[0]
    # The second time the log is there and is checked.
    code, said = doctor(config, FakeOllama([text_chunks("Ready.")]))
    assert code == 0 and "audit.jsonl: 1 records, chain intact" in said
    # In the check that ends an install, the record still names the release before: it is not shown as this one.
    code, said = doctor(settings(tmp_path, installing=True), FakeOllama(), quick=True)
    assert code == 0 and "ok: Jarvis (a release is being installed); settings from" in said and "v0.5.0" not in said


def test_the_quick_doctor_asks_nothing(tmp_path):
    fake = FakeOllama()
    code, said = doctor(settings(tmp_path), fake, quick=True)
    assert code == 0 and "holds qwen3:8b (not asked anything: --quick)" in said and fake.requests == []


def test_the_doctor_names_each_fault(tmp_path):
    config = settings(tmp_path)
    code, said = doctor(config, FakeOllama(down=True))
    assert code == 1 and "FAULT: Ollama does not answer at http://127.0.0.1:11434 (ConnectError)" in said
    code, said = doctor(config, FakeOllama(models=["other:latest"]))
    assert code == 1 and "FAULT: Ollama does not hold the model qwen3:8b. Run: update --repair" in said
    code, said = doctor(settings(tmp_path, model="mistral"), FakeOllama([text_chunks("Ready.")], models=["mistral:latest"]))
    assert code == 0, said
    code, said = doctor(config, FakeOllama([text_chunks("")]))
    assert code == 1 and "FAULT: qwen3:8b answered with nothing" in said
    # A log somebody edited.
    ask(config, FakeOllama([text_chunks("One.")]), "First.")
    ask(config, FakeOllama([text_chunks("Two.")]), "Second.")
    lines = config.audit_path.read_text().splitlines()
    config.audit_path.write_text("\n".join([lines[0].replace('"rounds":1', '"rounds":9'), *lines[1:]]) + "\n")
    code, said = doctor(config, FakeOllama(), quick=True)
    assert code == 1 and "FAULT: audit log" in said and "record altered" in said
    assert cli.audit(config, io.StringIO()) == 2


def test_without_a_model_the_doctor_says_so_and_chat_declines(tmp_path, capsys):
    config = settings(tmp_path, model="none")
    code, said = doctor(config, FakeOllama())
    assert code == 0 and "no local model is set (JARVIS_LOCAL_MODEL=none" in said
    assert cli.chat(config, ["Hello"], False) == 1
    assert "no local model is set" in capsys.readouterr().err


def test_a_log_that_cannot_be_kept_is_a_fault_not_a_crash(tmp_path, monkeypatch):
    (tmp_path / "data").write_text("a file where the folder should be")
    code, said = doctor(settings(tmp_path), FakeOllama())
    assert code == 1 and "FAULT: the audit log cannot be kept at" in said
    # A log that can be read but not written: nothing would notice until the first turn failed. (The tests
    # may run as root, who can write anything, so the answer to "may I write" is given here.)
    config = settings(tmp_path / "second")
    ask(config, FakeOllama([text_chunks("One.")]), "First.")
    monkeypatch.setattr(cli.os, "access", lambda path, mode: False)
    for quick, fake in ((True, FakeOllama()), (False, FakeOllama([text_chunks("Ready.")]))):
        code, said = doctor(config, fake, quick=quick)
        assert code == 1 and f"FAULT: the audit log {config.audit_path} cannot be written by the user" in said, said
    # The rest was still looked at, with a log that was thrown away.
    assert "answered in" in said and said.rstrip().endswith("not recorded") and len(records(config)) == 1


def test_a_log_whose_last_record_cannot_be_read_is_reported_and_never_continued(tmp_path, capsys, monkeypatch):
    config = settings(tmp_path)
    ask(config, FakeOllama([text_chunks("One.")]), "First.")
    whole = config.audit_path.read_bytes()
    for damage in (whole[:-40], whole + b'{"seq": 2\n', whole + b"\xff\xfe not text\n", whole + b"[1, 2]\n"):
        config.audit_path.write_bytes(damage)
        code, said = doctor(config, FakeOllama([text_chunks("Ready.")]))
        assert code == 1 and "FAULT: audit log: the last record of" in said and "cannot be read" in said, said
        assert "Jarvis will not add to it" in said and "answered in" in said and "not recorded" in said
        assert config.audit_path.read_bytes() == damage, "nothing was added to it"
        # For the installer it is a warning: the release is not at fault for a file Jarvis keeps.
        code, said = doctor(config, FakeOllama([text_chunks("Ready.")]), release_check=True)
        assert code == 0 and "WARNING: audit log: the last record of" in said and "FAULT" not in said, said
        out = io.StringIO()
        assert cli.audit(config, out) == 2 and "unreadable record" in out.getvalue()
    monkeypatch.setenv("JARVIS_ETC", str(tmp_path))
    monkeypatch.setenv("JARVIS_DATA_DIR", str(config.data_dir))
    assert cli.main(["chat", "Hello"]) == 1
    assert "Jarvis will not add to a log it cannot continue" in capsys.readouterr().err


def test_an_altered_log_is_a_warning_for_the_installer_and_a_fault_for_the_owner(tmp_path):
    config = settings(tmp_path)
    ask(config, FakeOllama([text_chunks("One.")]), "First.")
    ask(config, FakeOllama([text_chunks("Two.")]), "Second.")
    lines = config.audit_path.read_text().splitlines()
    config.audit_path.write_text("\n".join([lines[0].replace('"rounds":1', '"rounds":9'), *lines[1:]]) + "\n")
    code, said = doctor(config, FakeOllama([text_chunks("Ready.")]), release_check=True)
    assert code == 0 and "WARNING: audit log" in said and "record altered" in said and "audit record 3" in said
    code, said = doctor(config, FakeOllama([text_chunks("Ready.")]))
    assert code == 1 and "FAULT: audit log" in said
    # What the installer does count: a model that does not answer.
    code, said = doctor(config, FakeOllama(down=True), release_check=True)
    assert code == 1 and "FAULT: Ollama does not answer" in said


def test_the_audit_command(tmp_path):
    config, out = settings(tmp_path), io.StringIO()
    assert cli.audit(config, out) == 0 and out.getvalue().endswith("no records yet\n")
    ask(config, FakeOllama([text_chunks("One.")]), "First.")
    out = io.StringIO()
    assert cli.audit(config, out) == 0 and out.getvalue() == f"{config.audit_path}: 1 records, chain intact\n"


def test_the_command_line(tmp_path, monkeypatch, capsys):
    (tmp_path / "site.env").write_text("JARVIS_LOCAL_MODEL=none\n")
    (tmp_path / "release").write_text("name=v0.5.0\ncommit=0\n")
    monkeypatch.setenv("JARVIS_ETC", str(tmp_path))
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["--version"]) == 0 and capsys.readouterr().out == "v0.5.0\n"
    assert cli.main(["audit"]) == 0 and "no records yet" in capsys.readouterr().out
    assert cli.main(["chat", "Hello"]) == 1 and "no local model is set" in capsys.readouterr().err
    assert cli.main(["doctor", "--quick"]) == 0 and "ok: Jarvis v0.5.0" in capsys.readouterr().out
    monkeypatch.setenv("JARVIS_AUDIT_TEXT", "perhaps")
    assert cli.main(["audit"]) == 1 and "JARVIS_AUDIT_TEXT must be on or off" in capsys.readouterr().err


def test_the_chat_command_asks_one_question_and_ends_cleanly(tmp_path, capsys, caplog):
    config = settings(tmp_path)
    assert cli.chat(config, ["Good", "evening"], False, FakeOllama([text_chunks("Good evening, sir.")]).client()) == 0
    said = capsys.readouterr()
    assert said.out == "Good evening, sir.\n" and said.err.startswith("[qwen3:8b, audit record 1")
    assert cli.chat(config, ["Hello"], False, FakeOllama(down=True).client()) == 1
    assert not caplog.records, [record.getMessage() for record in caplog.records]


def test_a_conversation_keeps_its_thread_and_ctrl_c_stops_only_the_answer(tmp_path, capsys, monkeypatch):
    config = settings(tmp_path)

    class Interrupted(FakeOllama):
        def handler(self, request):
            if request.url.path == "/api/chat" and len(self.requests) == 1:
                self.requests.append(json.loads(request.content))
                raise KeyboardInterrupt
            return super().handler(request)

    fake = Interrupted([text_chunks("Both nodes are up."), text_chunks("Still up.")])
    typed = iter(["How are the nodes?", "Tell me a very long story.", "And now?", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(typed))
    assert cli.chat(config, [], False, fake.client()) == 0
    said = capsys.readouterr()
    assert said.out == "Both nodes are up.\nStill up.\n" and "[stopped]" in said.err
    # The stopped question left nothing behind: the third question follows the first answer.
    third = [m["content"] for m in fake.requests[2]["messages"][1:]]
    assert third == ["How are the nodes?", "Both nodes are up.", "And now?"]
    assert [r["kind"] for r in records(config)] == ["turn", "turn_aborted", "turn"]


def test_ctrl_c_while_the_model_is_thinking_cancels_the_turn(tmp_path, capsys, monkeypatch):
    config = settings(tmp_path)

    class Slow(FakeOllama):
        async def handler(self, request):
            if request.url.path == "/api/chat" and not self.requests:
                self.requests.append(json.loads(request.content))
                asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signal.SIGINT)
                await asyncio.sleep(30)
            return FakeOllama.handler(self, request)

    fake = Slow([text_chunks("Here I am.")])
    typed = iter(["Think for a long time.", "Are you there?", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(typed))
    assert cli.chat(config, [], False, fake.client()) == 0
    said = capsys.readouterr()
    assert said.out == "Here I am.\n" and "[stopped]" in said.err
    assert [m["content"] for m in fake.requests[1]["messages"][1:]] == ["Are you there?"]
    assert [r["kind"] for r in records(config)] == ["turn_aborted", "turn"]


def test_ctrl_c_in_the_middle_of_writing_an_answer_leaves_no_half_turn(tmp_path):
    """The interrupt lands in this program's own code, not while it waits: the turn is over at once, and must
    still be taken out of the conversation then, not whenever the half-read answer is cleared away."""
    config = settings(tmp_path)
    fake = FakeOllama([text_chunks("One. ", "Two."), text_chunks("Still here."), text_chunks("Yes.")])

    class Out(io.StringIO):
        def write(self, text):
            if text.startswith("One"):
                raise KeyboardInterrupt
            return super().write(text)

    loop = asyncio.new_event_loop()
    jarvis = cli.Jarvis(config, fake.client(), cli.SURFACE)
    out, notes = Out(), io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        cli._run(loop, cli.turn(jarvis, "terminal", "Tell me a story.", out, notes))
    assert jarvis.router._sessions["terminal"] == []
    assert cli._run(loop, cli.turn(jarvis, "terminal", "And now?", out, notes))["type"] == "done"
    assert cli._run(loop, cli.turn(jarvis, "terminal", "Sure?", out, notes))["type"] == "done"
    cli._finish(loop, jarvis)
    assert [m["content"] for m in fake.requests[1]["messages"][1:]] == ["And now?"]
    assert [m["content"] for m in fake.requests[2]["messages"][1:]] == ["And now?", "Still here.", "Sure?"]
    assert [r["kind"] for r in records(config)] == ["turn_aborted", "turn", "turn"] and out.getvalue() == "Still here.\nYes.\n"


def test_the_doctor_says_what_each_system_answered():
    from jarvis.cli import LAB_CHECKS
    assert set(LAB_CHECKS) == {"proxmox", "pbs", "opnsense", "dns", "switch"}
    assert LAB_CHECKS["switch"][1]({"system": {"model": "TL-SG2008P 3.0"}, "ports_up": 6}) == "TL-SG2008P 3.0 answers, 6 ports up"
    assert LAB_CHECKS["pbs"][1]({"datastores": [{"used_percent": 30.0}, {"used_percent": 71.5}]}) == "2 datastores, the fullest 71.5 percent used"
    assert LAB_CHECKS["opnsense"][1]({"name": "firewall.lab.example", "uptime": "3 days"}) == "firewall.lab.example answers, up 3 days"
    assert LAB_CHECKS["dns"][1]({"resolvers": [{"healthy": True}, {"healthy": False}]}) == \
        "1 of 2 resolvers healthy (ask Jarvis which and why)"
    assert LAB_CHECKS["dns"][1]({"resolvers": [{"healthy": True}]}) == "1 of 1 resolvers healthy"
