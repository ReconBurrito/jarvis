"""Jarvis does not say a bare no: a refusal needs a reason and a way forward, or it goes back to the model."""
import json

from fakes import FakeOllama, run, text_chunks, tool_chunks, audit  # noqa: F401 (a fixture)

from jarvis.llm import OllamaBackend
from jarvis.personality import FILLERS, filler, personality
from jarvis.refusal import HOLD_AT_MOST, Opening, missing, opens_with_refusal
from jarvis.router import SYSTEM_PROMPT, Router
from jarvis.tools import TIER_READ_ONLY, Tool, ToolRegistry

BARE = (
    "I'm sorry, I can't do that.",
    "I'm sorry. I can't do that.",
    "I apologize, but I can't do that.",
    "My apologies, I cannot help with that.",
    "I cannot do that.",
    "I'm afraid I can't help with that.",
    "Sorry, but I am unable to restart the node.",
    "That is not possible.",
    "Unfortunately, I won't be doing that.",
    "I’m sorry, sir, I cannot comply.",
    "No, I can't do that.",
    "I must decline.",
    "That can't be done.",
    "I'm sorry, but I can't do that. I can't access external systems without approval.",  # a reason, and no way forward
    "I cannot restart it. I couldn't even if you asked twice.",
    "I cannot tell stories.",                   # seen on the lab, 2026-10-08
    "I can't tell you a joke.",
    "I cannot say that.",
)
WHOLE = (
    "I cannot restart the node: none of my tools changes anything in the lab. You can restart it from the Proxmox page, and I can tell you its state before and after.",
    "I can't show the camera feed because no tool of mine reads it. I can report whether the recorder's guest is running instead.",
    "That is not possible yet, since the switch is not connected to me. Once you add its address I can read its ports.",
    "I am unable to send email; there is no tool for it. What I can do is put the text here for you to send.",
)
NOT_A_REFUSAL = (
    "Both nodes are up, sir.",
    "The guest web is stopped.",
    "Which node do you mean, pve1 or pve2?",
    "The backup failed at 02:00. I cannot see why: the task log ends before the error.",
    "I cannot see why the backup failed: the task log ends before the error.",
    "I can't tell from here whether the disk is failing.",
    "I'm unable to confirm that; the node does not answer.",
    "I cannot tell why it failed: the log is empty.",
    "I can't tell whether the disk is failing from here.",
    "I cannot say for sure what caused it; the log ends early.",
    "I cannot tell from the readings which guest is to blame.",
    "Nothing has changed since this morning.",
    "I can do that.",
    "No.",
    "No, it has been up for three hours.",
    "No guests are stopped.",
    "Not possible to say from here would be the honest answer; the log is empty.",
)


def test_bare_refusals_are_told_from_whole_ones_and_from_everything_else():
    for reply in BARE:
        assert opens_with_refusal(reply) and missing(reply) is not None, reply
    for reply in WHOLE:
        assert opens_with_refusal(reply) and missing(reply) is None, reply
    for reply in NOT_A_REFUSAL:
        assert not opens_with_refusal(reply) and missing(reply) is None, reply


def test_what_is_missing_is_named_for_the_model():
    assert missing("I can't do that.") == "saying what stands in the way and without offering the closest thing that can be done"
    assert missing("I cannot restart it because none of my tools changes the lab.") == "offering the closest thing that can be done"
    assert missing("I can't do that. You could restart it yourself.") == "saying what stands in the way"
    # The refusing words are not their own way forward ("I can't" holds "I can"), and an offer put as a
    # question is no way forward either: the end of a reply is trimmed of those.
    assert missing("I can't.") is not None
    assert missing("I cannot restart it because no tool does that. Shall I check its state?") == "offering the closest thing that can be done"


def feed(opening, text, size=7):
    return "".join(opening.feed(text[i:i + size]) for i in range(0, len(text), size))


def test_an_ordinary_reply_passes_after_its_first_sentence():
    opening = Opening()
    assert opening.feed("Both nodes") == "" and opening.feed(" are up. ") == "Both nodes are up. "
    assert opening.feed("Load is low.") == "Load is low." and opening.held() == ""
    long = Opening()
    text = "The guest list is long and this sentence goes on without a full stop for quite a while, as a listing of many guests would"
    assert feed(long, text) == text and long.held() == ""


def test_a_refusal_is_held_whole_until_it_is_taken():
    opening = Opening()
    text = "I'm sorry, I can't do that. It is simply not something I do."
    assert feed(opening, text) == "" and opening.held() == text
    assert opening.take() == text and opening.held() == "" and opening.feed(" More.") == " More."


def test_a_refusal_that_runs_long_is_let_through():
    opening = Opening()
    text = "I cannot do that. " + "This sentence explains at length. " * 30
    shown = feed(opening, text)
    assert shown == text and len(text) > HOLD_AT_MOST and opening.held() == ""


def test_a_reply_too_short_to_judge_is_still_held_at_the_end():
    opening = Opening()
    assert opening.feed("I can't") == "" and opening.held() == "I can't" and missing(opening.held()) is not None
    short = Opening()
    assert short.feed("Yes") == "" and short.held() == "Yes" and missing(short.held()) is None


# ------------------------------------------------------------ in a turn

async def fake_status(_):
    return {"host": "jarvis", "uptime_hours": 3.5}


def build(audit, fake):
    tools = ToolRegistry(audit)
    tools.register(Tool("local_status", "d", {"type": "object", "properties": {}}, TIER_READ_ONLY, fake_status))
    return Router([OllamaBackend("http://ollama", "qwen3:8b", fake.client())], tools, audit)


def turn(router, text, session="s"):
    async def go():
        return [event async for event in router.turn(session, text)]
    events = run(go())
    return events, "".join(e["text"] for e in events if e["type"] == "token")


def records(audit):
    return [json.loads(line) for line in audit.path.read_text().splitlines()]


def test_a_bare_no_is_not_shown_and_the_model_tries_again(audit):
    better = "I cannot restart the node: none of my tools changes the lab. You can restart it from the Proxmox page."
    fake = FakeOllama([text_chunks("I'm sorry, ", "I can't do that."), text_chunks(better)])
    router = build(audit, fake)
    events, shown = turn(router, "Restart the node.")
    assert shown == better and events[-1]["refusals_sent_back"] == 1
    sent = fake.requests[1]["messages"]
    assert sent[-2] == {"role": "assistant", "content": "I'm sorry, I can't do that."}
    assert "You do not say a bare no" in sent[-1]["content"] and "without saying what stands in the way" in sent[-1]["content"]
    # Neither the draft nor the note stays in the conversation.
    history = router._sessions["s"]
    assert [m["role"] for m in history] == ["user", "assistant"] and history[-1]["content"] == better
    assert records(audit)[-1]["data"]["refusals_sent_back"] == 1 and records(audit)[-1]["data"]["rounds"] == 2


def test_sent_back_the_model_may_use_a_tool_after_all(audit):
    fake = FakeOllama([text_chunks("I cannot check that."), tool_chunks("local_status", {}), text_chunks("Up for three and a half hours.")])
    events, shown = turn(build(audit, fake), "How long has this machine been up?")
    assert shown == "Up for three and a half hours." and [e["name"] for e in events if e["type"] == "tool"] == ["local_status"]
    assert events[-1]["refusals_sent_back"] == 1


def test_a_second_bare_no_is_shown_as_it_is(audit):
    fake = FakeOllama([text_chunks("I can't do that."), text_chunks("I cannot do that.")])
    events, shown = turn(build(audit, fake), "Restart the node.")
    assert shown == "I cannot do that." and len(fake.requests) == 2 and events[-1]["refusals_sent_back"] == 1


def test_a_whole_refusal_and_an_ordinary_reply_are_shown_untouched(audit):
    for reply in (WHOLE[0], "Both nodes are up. I cannot see the backup server."):
        fake = FakeOllama([text_chunks(*[reply[i:i + 9] for i in range(0, len(reply), 9)])])
        events, shown = turn(build(audit, fake), "Well?", session=reply)
        assert shown == reply and len(fake.requests) == 1 and "refusals_sent_back" not in events[-1]


def test_a_reply_written_for_the_claims_check_is_not_sent_back_again(audit):
    fake = FakeOllama([text_chunks("Understood. I will not ask that again."), text_chunks("I cannot change that myself in a conversation.")])
    events, shown = turn(build(audit, fake), "Never ask me that again.")
    assert shown == "Understood. I cannot change that myself in a conversation." and len(fake.requests) == 2
    assert "refusals_sent_back" not in events[-1] and events[-1]["claims_refused"] == ["promise"]


def test_at_the_last_round_with_tools_nothing_is_sent_back(audit):
    """The note invites a tool call; it is not sent when the round after it would have no tools to call."""
    fake = FakeOllama([text_chunks("I can't do that.")])
    tools = ToolRegistry(audit)
    router = Router([OllamaBackend("http://ollama", "qwen3:8b", fake.client())], tools, audit, max_tool_rounds=1)
    events, shown = turn(router, "Restart the node.")
    assert shown == "I can't do that." and len(fake.requests) == 1 and "refusals_sent_back" not in events[-1]


def test_words_that_lead_into_a_tool_call_are_passed_on_whole(audit):
    lead = [{"model": "qwen3:8b", "message": {"role": "assistant", "content": "I cannot say offhand. ",
             "tool_calls": [{"id": "c", "function": {"index": 0, "name": "local_status", "arguments": {}}}]}, "done": False},
            {"model": "qwen3:8b", "message": {"role": "assistant", "content": ""}, "done": True}]
    refusing = [dict(lead[0], message=dict(lead[0]["message"], content="I can't do that. ")), lead[1]]
    for first in (lead, refusing):
        fake = FakeOllama([first, text_chunks("Up for three and a half hours.")])
        router = build(audit, fake)
        events, shown = turn(router, "Uptime?", session=first[0]["message"]["content"])
        assert shown == first[0]["message"]["content"] + "Up for three and a half hours." and len(fake.requests) == 2
        assert "refusals_sent_back" not in events[-1]
        assert [m["role"] for m in router._sessions[first[0]["message"]["content"]]] == ["user", "assistant", "tool", "assistant"]


def test_a_reply_sent_back_for_refusing_is_still_held_to_what_the_tools_reported(audit):
    """Sent back for a bare no, the model claims it did the thing. That goes back as well, once."""
    honest = "I cannot restart the node: none of my tools changes the lab. You can restart it from the Proxmox page."
    fake = FakeOllama([text_chunks("I can't do that."), text_chunks("I have restarted the node."), text_chunks(honest)])
    router = build(audit, fake)
    events, shown = turn(router, "Restart the node.")
    assert shown == honest and len(fake.requests) == 3
    assert events[-1]["refusals_sent_back"] == 1 and events[-1]["claims_refused"] == ["lab"]
    assert [m["content"] for m in router._sessions["s"]] == ["Restart the node.", honest]


# ------------------------------------------------------------ the personality

def test_the_rules_keep_the_tools_to_the_lab_and_leave_the_rest_to_the_model():
    """On the lab the model refused a story because the rules seemed to say it may answer only from its tools."""
    assert "About the lab and the machine you run on, you know only what your tools return." in SYSTEM_PROMPT
    assert "writing or a story, you answer yourself" in SYSTEM_PROMPT and "Answer from what your tools return, and only from that" not in SYSTEM_PROMPT


def test_the_personality_leads_every_conversation(audit):
    words = personality()
    assert words.startswith("You are Jarvis") and "You do not say no." in words and "Never use Markdown" in words
    assert SYSTEM_PROMPT.startswith(words) and "Use a tool whenever the question is about something" in SYSTEM_PROMPT
    fake = FakeOllama([text_chunks("At your service.")])
    turn(build(audit, fake), "Hello.")
    assert fake.requests[0]["messages"][0]["content"].startswith(words)


def test_a_wait_for_a_tool_is_never_silent_and_never_the_same_twice_running(audit):
    assert all(filler(n) != filler(n + 1) for n in range(20)) and {filler(n) for n in range(20)} == set(FILLERS)
    fake = FakeOllama([tool_chunks("local_status", {}), tool_chunks("local_status", {}), text_chunks("Up for 3.5 hours.")])
    events, _ = turn(build(audit, fake), "Uptime?")
    said = [e["say"] for e in events if e["type"] == "working"]
    assert len(said) == 2 and said[0] != said[1] and set(said) <= set(FILLERS)
