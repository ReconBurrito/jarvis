import random

from jarvis.manner import Tail, accepted, is_offer, trim

OFFERS = (
    "Is there anything else you need?",
    "Is there anything else I can help you with?",
    "Is there anything specific you'd like me to check?",
    "Are there any other issues you'd like me to check?",
    "Any other questions?",
    "Would you like me to check the repository settings?",
    "Would you like to see the updated document?",
    "Would you like the details?",
    "Do you want me to look at the switch as well?",
    "Want me to check the switch?",
    "Do you need anything else?",
    "Can I help with anything else?",
    "Let me know if you need anything else.",
    "Please let me know if there is anything else you need.",
    "So, let me know if you need more detail.",
    "Let me know how you'd like to proceed.",
    "Let me know.",
    "How would you like to proceed?",
    "How else can I help?",
    "What else can I do for you?",
    "What would you like me to do next?",
    "Feel free to ask if you have more questions.",
    "If you need anything else, just ask.",
    "I'm here if you need anything.",
    "I'd be happy to help with anything else.",
    "Please ask me to check the lab's status or logs for more details.",
    "Anything else?",
    "Will there be anything else?",
    "Will that be all?",
    "I hope that helps!",
    "If you would like to know more about the tools I have access to, I can tell you that.",   # seen on the lab
    "If you'd like the details, just ask.",
    "If you would like, I can check the switch as well.",
)
NOT_OFFERS = (
    "The firewall is healthy.",
    # questions that need an answer before Jarvis can carry on
    "Which node do you mean?",
    "Which node would you like me to check?",
    "Would you like them tiled or cascaded?",
    "Do you mean pve2 or pve1?",
    "What would you like the skill to be called?",
    "What would you like me to check?",
    "Let me know which one you mean.",
    "Let me know if you mean pve2 or pve1.",
    "Could I have the node name?",
    "Can I assume you mean pve1?",
    "Should I use the note called Backups?",
    "Shall I close the pulse window?",
    "Do you want the one titled Backups?",
    "Do you have the VM ID?",
    "Would you like the pulse window closed?",
    "Is there something specific you mean by the cluster?",
    "Did you mean something else?",
    # statements that carry the answer
    "I cannot see anything else in the log.",
    "There is nothing else failing.",
    "I can see three guests on that node.",
    "If the repository is not signed, the update cannot run.",
    "Is the firewall up? Yes, it has been running for 3 days.",
    "Let me know if you want the log; the error itself was 404 Not Found from mirror.example.",
    "Feel free to ignore the warning: it is only a notice.",
    "What else is failing: only aptupdate.",
    "If you want it restarted, I can't do that in this build.",
    "I can only read the lab's status; if you want it changed, you must do it yourself.",
    "If you mean pve2, I can see it is offline.",
    "I can see the node is down, and if you need the reason, it is not in the tool result.",
    "I'm happy to report that nothing failed overnight.",
    "I'm here to read the lab's status, not to change it.",
    "I am ready to arrange the windows.",
    "If you need anything else, just ask.The error was 401 Unauthorized.",
    "Let me know if you need anything else.The error was 401 Unauthorized.",
    "Anything else?Which node do you mean?",
    "Let me know if you want the log, but the error itself was 401 Unauthorized.",
    "Let me know if you want the log - the error itself was 401 Unauthorized.",
    "I'd be happy to help, but I cannot restart guests in this build.",
    "Happy to help, the answer is 33 guests.",
    "Feel free to ask again in a minute, the node is still booting.",
    "Do not hesitate to reach out to your ISP, as the WAN link has dropped four times today.",
    "I'm here whenever you want the next reading, which the switch gives once a minute.",
    "Should I use pve2 instead?",
    "Shall I close the other window as well?",
    "Shall I look at the backups too?",
    "Tell me if you want pve1 or pve2.",
    "Let me know if you have the VM ID.",
    "Let me know when you have restarted it and I will read the status again.",
    "Let me know if you want me to go ahead.",
    "Is there anything else in the name, such as a number?",
    "Is there anything else it might be called?",
    "Did you have something else in mind?",
    "Anything more specific?",
    "Is there anything you want kept open?",
    "What else do you want in the note?",
    "Would you like the summary saved as a note?",
    "Would you like the details window closed?",
    "If you meant the other node, let me know.",
    # words quoted from a log or a note
    "The log ends with 'E: Failed to fetch. Do you want me to retry?'",
    "Your note says 'Check backups weekly. Let me know if you need anything else.'",
    "The prompt reads \u2018Update failed. Do you want me to retry?\u2019",
    "The prompt was (Update failed. Do you want me to retry?)",
    "The last lines of the log are:\nE: Failed to fetch\nDo you want me to continue?",
    "The notes found are:\nBackups\nHow can I help?",
    "The steps in the note are: 1. Stop the guest. 2. Let me know if you need anything else.",
    'The log ends with "Do you want me to continue?"',
    "Do you want to continue?",
    "",
)


def test_offers_are_told_from_answers_and_from_questions_that_need_answering():
    for sentence in OFFERS:
        assert is_offer(sentence), sentence


def test_a_closing_offer_is_cut_and_the_answer_is_kept():
    answer = "The task aptupdate ended with a warning: 404 Not Found from mirror.example."
    for offer in OFFERS:
        assert trim(f"{answer} {offer}") == (answer + " ", offer), offer
    assert trim(f"{answer} Let me know if you need more. Is there anything else you need?") == (
        answer + " ", "Let me know if you need more. Is there anything else you need?")
    assert trim(f"{answer}\n\nIs there anything else you need?") == (answer + "\n\n", "Is there anything else you need?")


def test_what_is_not_a_closing_offer_is_left_alone():
    for reply in (
        "I found two nodes. Which one do you mean?",
        "I can arrange them. Would you like them tiled or cascaded?",
        "Would you like me to check the switch? It reported a port down an hour ago.",  # the reply goes on after it
        "Is there anything else you need?",  # nothing but the offer: it is said, not swallowed
        "Version 3.5 is installed. The next is 4.0.",
        "pve1 is online. I can see these guests on it:\n- web (running)\n- files (stopped)",
        'The task log ends with: "E: Failed to fetch. Do you want me to retry?"',
        "All quiet.",
        "",
    ):
        assert trim(reply) == (reply, ""), reply
    for sentence in NOT_OFFERS:
        reply = f"The backup failed. {sentence}"
        assert trim(reply) == (reply, ""), reply


def test_only_the_offer_goes_when_the_answer_before_it_has_no_full_stop():
    listing = "pve1 is online. I can see these guests on it:\n- web (running)\n- files (stopped)\n\n"
    assert trim(listing + "Is there anything else you need?") == (listing, "Is there anything else you need?")
    assert trim("The firewall is fine. Is there anything else you need?\n") == ("The firewall is fine. ", "Is there anything else you need?")
    assert trim("The firewall is fine. Is there anything else you need? \n\n") == ("The firewall is fine. ", "Is there anything else you need?")


def test_streaming_shows_the_same_text_however_it_is_cut_into_pieces():
    rng = random.Random(7)
    replies = [f"The firewall is fine. It has been up for 3.5 days. {offer}" for offer in OFFERS] + [
        "I found two nodes. Which one do you mean?", "No. I cannot change anything in the lab.", "Yes."]
    for reply in replies:
        whole = trim(reply)
        for _ in range(20):
            tail, shown, at = Tail(), "", 0
            while at < len(reply):
                step = rng.randint(1, 9)
                shown += tail.feed(reply[at:at + step])
                at += step
            rest, cut = tail.finish()
            assert (shown + rest, cut) == whole, reply


def test_a_reply_is_passed_on_sentence_by_sentence():
    tail = Tail()
    assert tail.feed("The ") == "" and tail.feed("firewall") == "" and tail.feed(" is fine. ") == "The firewall is fine. "
    assert tail.feed("Is there anything") == "" and tail.feed(" else you need?") == ""
    assert tail.finish() == ("", "Is there anything else you need?")
    tail = Tail()
    assert tail.feed("Yes.") == "" and tail.finish() == ("Yes.", "")
    tail = Tail()  # a very long sentence is a listing, and is passed on as it is written once that is clear
    words = "The running guests are " + " ".join(f"guest{n}," for n in range(40))
    assert tail.feed(words).startswith("The running guests are guest0,") and tail.feed(" and web.") == " and web."
    tail = Tail()  # text that leads into a tool call is given back whole, and what follows is an answer of its own
    assert tail.feed("Let me check. Would you like me to look at the GPU?") == "Let me check. "
    assert tail.release() == "Would you like me to look at the GPU?"
    assert tail.feed("Would you like me to check the memory?") == "" and tail.finish() == ("Would you like me to check the memory?", "")


def test_a_sentence_can_be_refused_and_nothing_after_it_is_shown():
    refuse = lambda sentence: "write" if "saved" in sentence else None  # noqa: E731
    tail = Tail(reject=refuse)
    shown = tail.feed("The README is open. I have saved the document. Would you like to see it?")
    rest, cut = tail.finish()
    assert (shown + rest, cut) == ("The README is open. ", "") and tail.refused == ("I have saved the document.", "write")
    tail = Tail(reject=refuse)  # a claim is judged in a list too
    assert tail.feed("Here is what I did:\n1. I have saved the note.\n") == "Here is what I did:\n1. " and tail.refused[1] == "write"
    tail = Tail(reject=refuse)  # and in a long sentence: nothing goes out unjudged
    long = "pve1 is up and " + "fine and " * 30 + "I have saved the note."
    assert tail.feed(long + " ") == "" and tail.refused is not None
    tail = Tail(reject=refuse)  # before a tool call a claim is left out, and the rest is passed on
    assert tail.feed("Checking. I have saved it. ") == "Checking. " and tail.release() == "" and tail.dropped[1] == "write"
    tail = Tail(reject=refuse)
    assert tail.feed("Let me look. ") == "Let me look. " and tail.release() == "" and tail.dropped is None


def test_a_bare_yes_is_tied_to_the_yes_or_no_question_it_answers():
    asked = "The pulse window is open. Shall I close the pulse window?"
    for said in ("Yes.", "yes please", "Go ahead.", "Okay, do it.", "Sure", "Yes, Jarvis.", "Okay.", "Please.", "Alright.", "Okay, thanks."):
        assert accepted(asked, said) == "Shall I close the pulse window?", said
    assert accepted(asked, "Yes, and the notes too.") is None and accepted(asked, "No.") is None
    assert accepted("The firewall is fine.", "Yes.") is None
    assert accepted("I found two nodes. Which one do you mean?", "Yes.") is None
    assert accepted("Would you like them tiled or cascaded?", "Yes.") is None
    assert accepted("Is there anything else you need?", "Yes.") is None
    assert accepted("Do you mean node 1?", "Yes, 2.") is None
