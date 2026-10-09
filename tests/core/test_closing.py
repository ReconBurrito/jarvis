from fakes import run

from jarvis.closing import CASES, closing, is_dismissal, looks_like_request, worth_judging


def test_questions_instructions_and_a_yes_are_requests():
    for said in ("What's the overall service health?", "Whats the health", "How many guests are running", "Is it fine",
                 "Were the backups fine", "Jarvis, how's the firewall", "Check the switch.", "Actually, show me the switch",
                 "No, but can you check the weather", "Yes please.", "Yeah, pve1.", "Go ahead.", "Okay, do it.", "And is DNS up?",
                 "You just did it again. Please check your settings.", "You keep doing that.", "Stop asking me that.",
                 "Don't say that again.", "Never use that phrase.", "That is wrong. Check your notes.", "You said it again.",
                 "Close the lab pulse window.", "Open the README.", "Tile the windows.", "Move it to the left.",
                 "Write that down in the switch note.", "Try pve2.", "Repeat that."):
        assert looks_like_request(said), said
    for said in ("We're finished here.", "I'm all set", "It's fine", "Go back to sleep.", "You can stand down.", "As you were.",
                 "That does it for tonight.", "The first one.", "pve2", "No.", "", "Check's in the post", "Forget it, never mind.",
                 "No, I don't need anything.", "I'm all set, have a good night.", "I'm all set. Have a good night.",
                 "Thanks again, that's all.", "You did well, that's all.", "Don't worry about it.", "Stop.",
                 "You already answered it, thanks.", "That covers it, yes."):
        assert not looks_like_request(said), said


def test_the_rules_alone_never_take_a_request_for_a_goodbye():
    """Whatever the model says, the fixed rules may not get a labelled remark wrong."""
    for previous, said, goodbye in CASES:
        assert worth_judging(previous, said), said
        if is_dismissal(said):
            assert goodbye, f"the phrase list takes a request for a goodbye: {said}"
        elif looks_like_request(said):
            assert not goodbye, f"the request guard takes a goodbye for a request: {said}"
    assert sum(1 for _, _, goodbye in CASES if goodbye) >= 10 and sum(1 for _, _, goodbye in CASES if not goodbye) >= 10


class Judge:
    name, model = "local", "test"

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def chat(self, messages, tools, format=None):
        self.asked.append(messages)
        if isinstance(self.answer, Exception):
            raise self.answer
        yield {"type": "text", "text": self.answer}


OFFER = "The firewall is fine. Would you like me to check the switch as well?"


def test_the_order_is_phrase_then_request_shape_then_the_model():
    eager = Judge('{"owner_wants": "nothing_more"}')  # a model that calls everything a goodbye
    assert run(closing(eager, OFFER, "No thanks.")) == "phrase" and eager.asked == []
    assert run(closing(eager, OFFER, "What's the overall service health?")) is None and eager.asked == []
    assert run(closing(eager, OFFER, "Yes please.")) is None and eager.asked == []
    assert run(closing(eager, "The firewall is fine.", "I'm all set.")) is None and eager.asked == [], "nothing was asked or offered"
    assert run(closing(eager, OFFER, "I'm all set, but " + "please look at everything " * 3)) is None and eager.asked == []
    assert run(closing(eager, OFFER, "I'm all set.")) == "model" and len(eager.asked) == 1
    assert eager.asked[0][1]["content"] == "Assistant: The firewall is fine. Would you like me to check the switch as well? Owner: I'm all set. ->"


def test_doubt_or_failure_means_carry_on():
    assert run(closing(Judge('{"owner_wants": "something"}'), OFFER, "The switch first.")) is None
    assert run(closing(Judge("not json"), OFFER, "I'm all set.")) is None
    assert run(closing(Judge('{"owner_wants": "maybe"}'), OFFER, "I'm all set.")) is None
    assert run(closing(Judge(RuntimeError("down")), OFFER, "I'm all set.")) is None
    assert run(closing(None, OFFER, "I'm all set.")) is None and run(closing(None, OFFER, "No.")) == "phrase"
    assert run(closing(None, OFFER, "Well?")) is None and run(closing(None, OFFER, "Okay?")) is None, "a question is never a goodbye"
    for said in ("101, thanks.", "No, 102.", "2, please.", "Okay.", "Alright.", "Now."):
        assert run(closing(None, "I found two guests. Which one do you mean?", said)) is None, said
    assert run(closing(None, OFFER, "Thank you.")) == "phrase" and run(closing(None, OFFER, "Thanks, Jarvis.")) == "phrase"
