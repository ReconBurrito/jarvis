import json

from fakes import FakeOllama, run, text_chunks, tool_chunks, audit  # noqa: F401 (a fixture)

from jarvis.about import describe
from jarvis.claims import Claims, claims, worked
from jarvis.llm import OllamaBackend
from jarvis.router import Router
from jarvis.tools import TIER_READ_ONLY, Tool, ToolRegistry

CLAIMED = {
    'I have added a line at the bottom saying "test" and saved the document.': "write",
    'The line "test" has been added.': "write",
    "The README document is now open and scrolled to the bottom.": "hands",
    "I have noted your instruction and will not ask if there is anything else.": "promise",
    "I will not ask again.": "promise",
    "Understood, I won't use that phrase again.": "promise",
    "From now on, I will keep my answers short.": "promise",
    "I've restarted the backup server.": "lab",
    "I will ignore that error from now on.": "lab",
    "I have closed the pulse window.": "desktop",
    "The pulse window has been closed.": "desktop",
    "The note has been updated with the new port count.": "write",
    "I scrolled down to the section.": "hands",
    "The change is now saved.": "write",
    "I opened the README and added the line.": "write",
    "It has been saved.": "write",
}
NOT_CLAIMED = (
    "A new guest has been added to the cluster.", "The guest web was stopped at 02:00.", "The backup was created last night.",
    "The firewall has been updated to version 25.1.", "I have not saved anything.", "I cannot add lines to a note.",
    "The README note is open.", "Nothing was changed.", "pihole-2 was restarted three hours ago.",
    "The snapshot was removed by the prune job.", "I found two nodes.", "I have checked the backup server.", "I will check the switch.",
    "The switch firmware was updated in June.", "I could not close the window because the desktop is not open.",
    "The port was disabled by the switch.", "The README file was created on 3 October.", "The line has not been added.",
    "That waits on the branch proposal/note-user and takes effect when you merge it.", "I will not be able to see that.",
    "The note says the switch was installed in the rack.", "The backup server has been running for 3 days.",
    "Port 7 has been down since noon.", "A firewall update has been released.", "It has been up for 12 days.",
    "The last backup of web has been verified.", 'The log says "the file was saved by root."',
    "The task log ends with 'I have saved the configuration'.",
)


def test_claims_about_jarviss_own_actions_are_told_from_reports_about_the_lab():
    for sentence, kind in CLAIMED.items():
        assert kind in claims(sentence), sentence
    for sentence in NOT_CLAIMED:
        assert claims(sentence) == [], sentence


def test_a_claim_may_be_made_once_a_tool_stands_behind_it():
    made = Claims(["note_add", "desktop_window"])
    assert made.unbacked("I have added the line.") == "write" and made.unbacked("I have closed the window.") == "desktop"
    made.tool("note_add", {"error": "there is no note called 'ups'", "done": False})
    made.tool("desktop_window", {"done": False, "note": "it was not open"})
    assert made.unbacked("I have added the line.") == "write" and made.unbacked("I have closed the window.") == "desktop", "a failed call backs nothing"
    made.tool("note_add", {"done": True, "path": "wiki/switch.md"})
    made.tool("desktop_window", {"done": True, "windows": []})
    assert made.unbacked("I have added the line.") is None and made.unbacked("I have closed the window.") is None
    assert made.unbacked("I have added the line and scrolled down to it.") == "hands", "nothing ever stands behind scrolling"
    assert made.unbacked("I have restarted the node.") == "lab"
    proposed = Claims(["note_add"])
    proposed.tool("note_add", {"done": False, "proposed": True, "branch": "proposal/note-user"})
    assert proposed.unbacked("I have noted that; it waits for you to merge it.") is None
    assert worked("x", {"done": True}) == "done" and worked("x", {"done": False, "proposed": True}) == "proposed"
    for result in ({"guests": []}, {"error": "no"}, "text", {"done": False}, {"started": True}):
        assert worked("x", result) is None, result


def test_the_self_description_follows_the_tools_that_are_there():
    full = describe(["proxmox_guests", "pbs_backups", "note_add", "note_replace", "notes_read", "desktop_window", "code_propose"], "qwen3:8b")
    assert "qwen3:8b" in full and "read the state of the Proxmox cluster" in full and "add lines to a note (note_add)" in full
    assert "only when a tool has reported it in this turn" in full
    assert "scroll, type, click or select inside a window" in full and "change anything in the lab" in full
    assert "write to or change a note;" not in full and "edit a note in any other way than note_add and note_replace" in full
    bare = describe(["local_status"])
    assert "write to or change a note" in bare and "Proxmox" not in bare and "look anything up on the internet" in bare
    assert "You have no tools at the moment" in describe([])
    assert "runs on a computer in the owner's lab" in describe([], where="")
    assert "What lasts is an audit log of your turns and tool calls. The owner talks to you by typing." in bare
    assert "What lasts is your notes and an audit log" in full and "open, close or move a window, or see" in bare
    assert "in a terminal on that machine" in describe([], surface="The owner is typing to you in a terminal on that machine.")
    assert "runs on Proxmox container 201 on the node pve2" in describe([], where="Proxmox container 201 on the node pve2")


async def fake_status(_):
    return {"host": "jarvis"}


async def note_add(arguments):
    return {"done": True, "path": "README.md", "commit": "abc1234"}


def build(audit, fake, with_note_tool=True):
    backend = OllamaBackend("http://ollama", "qwen3:8b", fake.client())
    backend.name = "local"
    tools = ToolRegistry(audit)
    tools.register(Tool("local_status", "d", {"type": "object", "properties": {}}, TIER_READ_ONLY, fake_status))
    if with_note_tool:
        tools.register(Tool("note_add", "d", {"type": "object", "properties": {}}, TIER_READ_ONLY, note_add))
    return Router([backend], tools, audit, audit_text=True)


def turn(router, text):
    async def go():
        return [event async for event in router.turn("s1", text)]

    events = run(go())
    return events, "".join(e["text"] for e in events if e["type"] == "token")


def records(audit):
    return [json.loads(line)["data"] for line in audit.path.read_text().splitlines() if json.loads(line)["kind"] == "turn"]


def test_a_false_claim_is_not_shown_and_the_model_does_it_properly_on_the_second_try(audit):
    fake = FakeOllama([
        text_chunks('The README is open. I have added a line saying "test" and saved the document. Would you like to see it?'),
        tool_chunks("note_add", {"note": "README", "text": "test"}),
        text_chunks('I have added the line "test" to the README.'),
    ])
    router = build(audit, fake)
    events, shown = turn(router, "Add a line saying test to the README.")
    assert shown == 'The README is open. I have added the line "test" to the README.'
    assert events[-1]["claims_refused"] == ["write"] and "saved the document" not in shown
    sent_back = fake.requests[1]["messages"]
    assert sent_back[-2]["role"] == "assistant" and "saved the document" in sent_back[-2]["content"]
    assert sent_back[-1]["role"] == "user" and "No tool reported that in this turn" in sent_back[-1]["content"]
    assert "call note_add or note_replace" in sent_back[-1]["content"]
    history = router._sessions["s1"]
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"], "the draft and the note about it are not kept"
    assert history[-1]["content"] == shown
    record = records(audit)[-1]
    assert record["claims_refused"] == ["write"] and record["reply_text"] == shown and record["tools"] == ["note_add"]


def test_a_second_false_claim_is_replaced_by_the_plain_truth(audit):
    fake = FakeOllama([text_chunks("I have saved the document."), text_chunks("The document has been saved.")])
    router = build(audit, fake, with_note_tool=False)
    events, shown = turn(router, "Save the document.")
    assert shown == "I have not written or changed anything: none of my tools reported it."
    assert events[-1]["claims_refused"] == ["write", "write"]
    assert "You have no tool that writes to a note." in fake.requests[1]["messages"][-1]["content"]
    assert router._sessions["s1"][-1] == {"role": "assistant", "content": shown} and len(router._sessions["s1"]) == 2
    assert len(fake.requests) == 2, "the model is sent back once, not for ever"


def test_a_promise_about_its_own_behaviour_needs_a_note_behind_it(audit):
    fake = FakeOllama([text_chunks("Understood. I will not ask that again."), text_chunks("I cannot change that myself in a conversation.")])
    router = build(audit, fake)
    _, shown = turn(router, "Never ask me that again.")
    assert shown == "Understood. I cannot change that myself in a conversation."
    assert "call note_add with the note USER" in fake.requests[1]["messages"][-1]["content"]


def test_a_hint_never_names_a_tool_that_is_not_there():
    bare = Claims(["local_status"])
    for kind in ("write", "action", "desktop", "hands", "browse", "lab", "promise"):
        note = bare.note(kind, "I have done it.")
        assert not any(tool in note for tool in ("note_add", "note_replace", "desktop_", "web_")), (kind, note)
    assert "call desktop_window or desktop_arrange" in Claims(["desktop_window"]).note("desktop", "I closed it.")
    assert "call web_browse with the whole task" in Claims(["web_browse"]).note("hands", "I scrolled down.")


def test_honest_replies_and_lab_reports_pass_untouched(audit):
    text = "The guest web was stopped at 02:00. A new guest has been added to the cluster. I cannot restart anything."
    fake = FakeOllama([text_chunks(text)])
    router = build(audit, fake)
    events, shown = turn(router, "What changed?")
    assert shown == text and "claims_refused" not in events[-1] and len(fake.requests) == 1


def test_the_local_model_is_told_what_it_is(audit):
    fake = FakeOllama([text_chunks("A program on a laptop in the lab.")])
    router = build(audit, fake)
    turn(router, "What are you?")
    system = fake.requests[0]["messages"][0]["content"]
    assert "What you are and how you work:" in system and "You are a program called Jarvis" in system and "local_status" not in system
    assert system.startswith("You are Jarvis, the assistant of this home lab") and "read the state of the machine you run on" in system


def test_opening_a_page_is_backed_by_browser_open_and_working_inside_one_never_is():
    made = Claims(["browser_open", "browser_read", "browser_tabs"])
    assert made.unbacked("I opened the page on example.com.") == "browse"
    assert made.unbacked("I signed in to the site.") == "form"
    made.tool("browser_open", {"error": "localhost is not an address on the internet"})
    assert made.unbacked("I opened the page on example.com.") == "browse", "a refused open backs nothing"
    made.tool("browser_open", {"done": True, "tab": "T1", "title": "Example"})
    assert made.unbacked("I opened the page on example.com.") is None
    assert made.unbacked("I signed in to the site.") == "form", "browser_open does not sign in"
    assert made.unbacked("I filled in the form on the page.") == "form"
    assert made.unbacked("I clicked the sign-in button on the page.") in ("hands", "form"), "nor click"
    assert "browser_open opens a page and browser_read reads it" in made.note("form", "I clicked it.")
    assert "call browser_open with its address" in made.note("browse", "I opened it.")
    read = Claims(["browser_read"])
    read.tool("browser_read", {"done": True, "tab": "T1", "text": "Hello"})
    assert read.unbacked("I looked up the page you have open in the browser.") is None


def test_a_hint_never_names_a_browser_tool_that_is_not_there():
    bare = Claims(["local_status"])
    for kind in ("browse", "form"):
        assert "browser_" not in bare.note(kind, "I did it."), kind


def test_jarvis_is_told_what_the_browser_tools_can_and_cannot_do():
    said = describe(["local_status", "browser_open", "browser_read", "browser_tabs"])
    assert "open a web page there (browser_open)" in said and "click, type, scroll or sign in on a web page" in said
    assert "look anything up on the internet" not in said.split("What you cannot do:")[1]
    assert "look anything up on the internet" in describe(["local_status"])
