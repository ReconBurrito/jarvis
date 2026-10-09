import json

from fakes import FakeOllama, run, text_chunks, tool_chunks, audit  # noqa: F401 (a fixture)

from jarvis.audit import verify
from jarvis.llm import OllamaBackend
from jarvis.router import Router
from jarvis.tools import TIER_READ_ONLY, Tool, ToolRegistry


async def fake_status(_):
    return {"host": "jarvis", "gpu": {"memory_used_mib": 6300}}


def build(audit, *fakes, **kwargs):
    backends = []
    for i, fake in enumerate(fakes):
        backend = OllamaBackend("http://ollama", f"model-{i}", fake.client())
        backend.name = ["local", "remote"][i]
        backends.append(backend)
    tools = ToolRegistry(audit)
    tools.register(Tool("local_status", "d", {"type": "object", "properties": {}}, TIER_READ_ONLY, fake_status))
    return Router(backends, tools, audit, **kwargs)


async def collect(router, session, text):
    return [event async for event in router.turn(session, text)]


def records(audit):
    return [json.loads(line) for line in audit.path.read_text().splitlines()]


def test_plain_turn_streams_and_audits(audit):
    fake = FakeOllama([text_chunks("All ", "quiet, sir.")])
    router = build(audit, fake)
    events = run(collect(router, "s1", "status?"))
    assert events[0] == {"type": "route", "backend": "local", "model": "model-0"}
    assert "".join(e["text"] for e in events if e["type"] == "token") == "All quiet, sir."
    assert events[-1]["type"] == "done" and events[-1]["audit_seq"] == 1
    rec = records(audit)[0]
    assert rec["kind"] == "turn" and rec["data"]["backend"] == "local" and rec["data"]["tools"] == []
    assert "user_text" not in rec["data"] and rec["data"]["user_chars"] == 7
    assert fake.requests[0]["messages"][0]["role"] == "system"


def test_tool_round_feeds_result_back(audit):
    fake = FakeOllama([tool_chunks("local_status", {}), text_chunks("GPU holds 6.3 GB.")])
    router = build(audit, fake)
    events = run(collect(router, "s1", "how is the GPU?"))
    assert {"type": "tool", "name": "local_status", "arguments": {}, "ok": True} in events
    second = fake.requests[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_name"] == "local_status"
    assert json.loads(second[-1]["content"])["gpu"]["memory_used_mib"] == 6300
    assert [r["kind"] for r in records(audit)] == ["tool_call", "turn"]
    assert records(audit)[1]["data"]["tools"] == ["local_status"] and records(audit)[1]["data"]["rounds"] == 2
    assert verify(audit.path)[0] is True


def test_history_carries_to_next_turn(audit):
    fake = FakeOllama([text_chunks("One."), text_chunks("Two.")])
    router = build(audit, fake)
    run(collect(router, "s1", "first"))
    run(collect(router, "s1", "second"))
    roles = [m["role"] for m in fake.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_falls_back_when_local_is_down(audit):
    router = build(audit, FakeOllama(down=True), FakeOllama([text_chunks("Remote here.")]))
    events = run(collect(router, "s1", "hello"))
    assert events[0] == {"type": "route", "backend": "remote", "model": "model-1"}
    data = records(audit)[0]["data"]
    assert data["backend"] == "remote" and data["fallback_from"][0].startswith("local:")


def test_all_backends_down_is_an_error_and_rolls_back(audit):
    router = build(audit, FakeOllama(down=True))
    events = run(collect(router, "s1", "hello"))
    assert [e["type"] for e in events] == ["error"]
    assert records(audit)[0]["kind"] == "turn_failed"
    assert router._sessions["s1"] == []


def test_tool_loop_is_bounded(audit):
    fake = FakeOllama([tool_chunks("local_status", {}) for _ in range(5)])
    router = build(audit, fake, max_tool_rounds=2)
    events = run(collect(router, "s1", "loop"))
    assert len(fake.requests) == 3
    assert "limit" in "".join(e["text"] for e in events if e["type"] == "token")
    assert events[-1]["type"] == "done"
    assert router._sessions["s1"][-1]["role"] == "assistant" and "tool_calls" not in router._sessions["s1"][-1]


def test_after_the_last_tool_round_the_model_answers_with_what_it_has(audit):
    fake = FakeOllama([tool_chunks("local_status", {}), tool_chunks("local_status", {}), text_chunks("Here is the summary.")])
    router = build(audit, fake, max_tool_rounds=2)
    events = run(collect(router, "s1", "morning report"))
    assert "tools" in fake.requests[0] and "tools" in fake.requests[1]
    assert "tools" not in fake.requests[2], "the final round offers no tools"
    said = "".join(e["text"] for e in events if e["type"] == "token")
    assert said == "Here is the summary." and "limit" not in said
    assert events[-1]["type"] == "done" and events[-1]["audit_seq"]


def test_history_is_trimmed_to_a_user_message(audit):
    fake = FakeOllama([text_chunks("ok") for _ in range(6)])
    router = build(audit, fake, max_history=4)
    for i in range(6):
        run(collect(router, "s1", f"q{i}"))
    hist = router._sessions["s1"]
    assert len(hist) <= 4 and hist[0]["role"] == "user"


def test_audit_text_is_opt_in(audit):
    router = build(audit, FakeOllama([text_chunks("Yes.")]), audit_text=True)
    run(collect(router, "s1", "really?"))
    data = records(audit)[0]["data"]
    assert data["user_text"] == "really?" and data["reply_text"] == "Yes."


def test_client_leaving_mid_turn_rolls_back(audit):
    fake = FakeOllama([text_chunks("One ", "two ", "three.")])
    router = build(audit, fake)

    async def go():
        gen = router.turn("s1", "count")
        async for event in gen:
            if event["type"] == "token":
                break
        await gen.aclose()

    run(go())
    assert router._sessions["s1"] == []
    assert [r["kind"] for r in records(audit)] == ["turn_aborted"]


ASKING = "I found two nodes. Which one do you mean?"


def judge(wants):
    return text_chunks(json.dumps({"owner_wants": wants}))


def test_a_known_goodbye_gets_a_sign_off_without_a_model_call(audit):
    fake = FakeOllama([text_chunks(ASKING)])
    router = build(audit, fake)
    run(collect(router, "s1", "How is the firewall?"))
    events = run(collect(router, "s1", "No, that'll be all, thanks."))
    assert len(fake.requests) == 1
    assert [e["type"] for e in events] == ["token", "done"] and events[0]["text"] in ("Very good.", "Of course.")
    assert events[1]["closing"] == "phrase" and "backend" not in events[1]
    record = records(audit)[-1]["data"]
    assert record["closing"] == "phrase" and record["backend"] is None and record["rounds"] == 0
    assert verify(audit.path)[0] is True


def test_an_unfamiliar_goodbye_is_understood_by_the_model(audit):
    fake = FakeOllama([text_chunks(ASKING), judge("nothing_more"), text_chunks("Both nodes are online.")])
    router = build(audit, fake)
    run(collect(router, "s1", "How is the firewall?"))
    events = run(collect(router, "s1", "You can stand down for the evening."))
    asked = fake.requests[1]
    assert len(fake.requests) == 2, "one judgement, and no answer from the model after it"
    assert asked["format"]["properties"]["owner_wants"]["enum"] == ["nothing_more", "something"]
    # The judgement is a small question of its own: the offer, the reply, worked examples, and no tools.
    assert [m["role"] for m in asked["messages"]] == ["system", "user"] and "tools" not in asked
    assert "If you are not sure, it is \"something\"" in asked["messages"][0]["content"]
    assert asked["messages"][1]["content"] == f"Assistant: {ASKING} Owner: You can stand down for the evening. ->"
    assert [e["type"] for e in events] == ["token", "done"] and events[1]["closing"] == "model"
    assert records(audit)[-1]["data"]["closing"] == "model"
    # The sign-off is part of the conversation, in plain words, and the next request is a normal turn.
    run(collect(router, "s1", "Actually, how are the nodes?"))
    later = fake.requests[2]["messages"]
    assert later[-3:-1] == [{"role": "user", "content": "You can stand down for the evening."}, {"role": "assistant", "content": events[0]["text"]}]
    assert "format" not in fake.requests[2]


def test_an_answer_to_the_question_carries_on_as_a_normal_turn(audit):
    fake = FakeOllama([text_chunks(ASKING), judge("something"), text_chunks("The switch is healthy.")])
    router = build(audit, fake)
    run(collect(router, "s1", "How is the firewall?"))
    events = run(collect(router, "s1", "The switch first."))
    assert "".join(e["text"] for e in events if e["type"] == "token") == "The switch is healthy."
    assert "format" in fake.requests[1]
    assert fake.requests[2]["messages"][-1] == {"role": "user", "content": "The switch first."} and "format" not in fake.requests[2]
    assert "closing" not in records(audit)[-1]["data"] and "closing" not in events[-1]


def test_a_question_or_a_request_is_never_put_up_for_judgement(audit):
    said = ["What's the overall service health?", "How many guests are running", "Yes please.", "Go ahead.",
            "No, but check the switch.", "Actually, show me the backups.", "We're done with that, is DNS up?"]
    fake = FakeOllama([text_chunks(ASKING)] * (len(said) + 1))
    router = build(audit, fake)
    run(collect(router, "s1", "How is the firewall?"))
    for words in said:
        events = run(collect(router, "s1", words))
        assert "closing" not in events[-1], words
    assert len(fake.requests) == len(said) + 1 and all("format" not in r for r in fake.requests)
    assert all("closing" not in r["data"] for r in records(audit))


def test_no_judgement_without_a_question_or_for_a_long_reply(audit):
    fake = FakeOllama([text_chunks("The firewall is fine."), text_chunks("Understood."), text_chunks(ASKING), text_chunks("Checking both.")])
    router = build(audit, fake)
    run(collect(router, "s1", "How is the firewall?"))
    run(collect(router, "s1", "No."))  # nothing was asked, so "No." is not a goodbye
    run(collect(router, "s1", "And the rest?"))
    run(collect(router, "s1", "No, but please look at the switch and at both of the Proxmox nodes while you are at it."))
    assert len(fake.requests) == 4 and all("format" not in r for r in fake.requests)


def test_a_failed_judgement_falls_back_to_a_normal_turn(audit):
    fake = FakeOllama([text_chunks(ASKING), text_chunks("not json"), text_chunks("As you wish.")])
    router = build(audit, fake)
    run(collect(router, "s1", "How is the firewall?"))
    events = run(collect(router, "s1", "Leave it."))
    assert "".join(e["text"] for e in events if e["type"] == "token") == "As you wish."


def test_the_fallback_model_is_never_asked_to_judge(audit):
    local, remote = FakeOllama(down=True), FakeOllama([text_chunks(ASKING), text_chunks("Standing down.")])
    router = build(audit, local, remote)
    run(collect(router, "s1", "How is the firewall?"))
    events = run(collect(router, "s1", "You can stand down."))
    assert all("format" not in r for r in remote.requests) and len(remote.requests) == 2
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Standing down."


def test_a_closing_offer_never_reaches_the_owner_or_the_history(audit):
    fake = FakeOllama([text_chunks("The firewall ", "is fine. ", "Is there anything ", "else you need?"), text_chunks("Both nodes are online.")])
    router = build(audit, fake)
    events = run(collect(router, "s1", "How is the firewall?"))
    assert "".join(e["text"] for e in events if e["type"] == "token").rstrip() == "The firewall is fine."
    assert events[-1]["trimmed"] == "Is there anything else you need?"
    shown = "".join(e["text"] for e in events if e["type"] == "token")
    assert router._sessions["s1"][-1] == {"role": "assistant", "content": shown}
    record = records(audit)[-1]["data"]
    assert record["trimmed_chars"] == 32 and record["reply_chars"] == len(shown), "the record is of what was shown"
    # With no offer left standing, a short remark afterwards is an ordinary turn, never a canned sign-off.
    events = run(collect(router, "s1", "You just did it again."))
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Both nodes are online." and "closing" not in events[-1]
    assert "trimmed" not in events[-1] and "trimmed_chars" not in records(audit)[-1]["data"]


def test_a_complaint_after_a_question_is_never_taken_for_a_goodbye(audit):
    fake = FakeOllama([text_chunks(ASKING), text_chunks("My apologies.")])
    router = build(audit, fake)
    run(collect(router, "s1", "How is the node?"))
    events = run(collect(router, "s1", "You just did it again. Please check your settings."))
    assert "".join(e["text"] for e in events if e["type"] == "token") == "My apologies." and "closing" not in events[-1]
    assert len(fake.requests) == 2, "the judge was not even asked"


def test_text_before_a_tool_call_is_passed_on_whole(audit):
    fake = FakeOllama([[*text_chunks("Shall I look? ")[:-1], *tool_chunks("local_status", {})], text_chunks("GPU holds 6.3 GB.")])
    router = build(audit, fake)
    events = run(collect(router, "s1", "how is the GPU?"))
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Shall I look? GPU holds 6.3 GB."


def test_a_bare_yes_is_sent_with_the_question_it_answers(audit):
    fake = FakeOllama([text_chunks("The pulse window is open. Shall I close the pulse window?"),
                       tool_chunks("local_status", {}), text_chunks("The host is jarvis.")])
    router = build(audit, fake)
    run(collect(router, "s1", "What is on my desktop?"))
    events = run(collect(router, "s1", "Okay."))
    assert "closing" not in events[-1], "a yes is never taken for a goodbye"
    sent = fake.requests[1]["messages"][-1]["content"]
    assert sent.startswith('Okay. (The owner is saying yes to your last question: "Shall I close the pulse window?"')
    after_tool = [m["content"] for m in fake.requests[2]["messages"] if m["role"] == "user"][-1]
    assert after_tool == "Okay.", "the note rides on the first round only"
    assert router._sessions["s1"][2] == {"role": "user", "content": "Okay."}, "the history keeps the plain words"


def test_an_offer_after_a_tool_call_with_nothing_else_is_said(audit):
    fake = FakeOllama([[*text_chunks("Let me check. ")[:-1], *tool_chunks("local_status", {})], text_chunks("Would you like me to check the GPU memory?")])
    router = build(audit, fake)
    events = run(collect(router, "s1", "how is the GPU?"))
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Let me check. Would you like me to check the GPU memory?"
    assert router._sessions["s1"][-1]["content"] == "Would you like me to check the GPU memory?" and "trimmed" not in events[-1]


def test_a_client_leaving_at_the_sign_off_leaves_a_whole_turn(audit):
    fake = FakeOllama([text_chunks(ASKING)])
    router = build(audit, fake)
    run(collect(router, "s1", "How is the node?"))

    async def go():
        gen = router.turn("s1", "No, that'll be all.")
        async for event in gen:
            if event["type"] == "token":
                break
        await gen.aclose()

    run(go())
    history = router._sessions["s1"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"] and history[-1]["content"] in ("Very good.", "Of course.")
    assert records(audit)[-1]["data"]["closing"] == "phrase"


def test_a_turn_given_up_late_takes_only_its_own_messages_with_it(audit):
    """A reader that walks away from an answer may be cleared away only later, after other turns in the same
    conversation. What it then removes is its own question, and nothing that was said since."""
    fake = FakeOllama([text_chunks("One. ", "Two."), text_chunks("Still here.")])
    router = build(audit, fake)

    async def go():
        stale = router.turn("s1", "Tell me a story.")
        while (await stale.__anext__())["type"] != "token":
            pass
        events = [event async for event in router.turn("s1", "And now?")]
        await stale.aclose()
        return events

    assert run(go())[-1]["type"] == "done"
    assert [m["content"] for m in router._sessions["s1"]] == ["And now?", "Still here."]
    assert [r["kind"] for r in records(audit)] == ["turn", "turn_aborted"]
