"""Knowing when a conversation is over.

When Jarvis ends a reply by asking or offering something and the person
answers that they need nothing more, the right reply is a short sign-off: no
lookup, no further question. Three steps, in this order:

1. Common ways of saying so are recognized outright.
2. Anything shaped like a question, a request, a yes or a complaint is never a
   goodbye, and goes on as a normal turn without being judged.
3. What is left, a short remark that is neither, is put to the local model as a
   two-way choice with worked examples, so a phrase nobody thought of in advance
   is still understood by its meaning. When the model is unsure, it is a request.

Treating a request as a goodbye is the worse mistake, so every doubt is settled
in favour of carrying on. `python -m jarvis.closing` runs a set of labelled
remarks through the live model and shows where it is wrong.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

from .text import speakable

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_ASKS = re.compile(
    r"\b(?:please\s+(?:specify|tell|say|choose|pick|confirm|provide|clarify|name|give|repeat|let)"
    r"|let me know|tell me which|which one|would you like|do you want|shall i|should i)\b",
    re.IGNORECASE,
)


def expects_answer(reply: str) -> bool:
    """Does this reply end by asking the person for something, with or without a question mark."""
    sentences = [s for s in _SENTENCE_END.split(speakable(reply)) if s.strip()]
    if not sentences:
        return False
    last = sentences[-1].strip()
    return last.endswith("?") or bool(_ASKS.search(last))


# Said when the person answers a question of Jarvis's by saying they are done. Nothing is looked up or asked.
SIGN_OFFS = ("Very good.", "Of course.")
_POLITE = frozenset("thanks thank you cheers please jarvis for now then okay ok alright well".split())
_DONE = frozenset({
    "no", "nope", "nah", "no no", "nothing", "nothing else", "no nothing else", "nothing more", "no nothing more",
    "thats all", "that is all", "that will be all", "thatll be all", "thatll be it", "that will be it", "thats it",
    "that would be all", "thats everything", "that is everything", "im good", "i am good", "were good", "all good",
    "im done", "were done", "goodbye", "bye", "good night", "goodnight", "not right", "nothing right",
})


def is_dismissal(text: str) -> bool:
    """Is this the person saying they need nothing more ("No, that'll be it, thanks")?"""
    words = re.sub(r"[^a-z ]", "", text.lower().replace("'", "").replace("’", "")).split()
    core = [w for w in words if w not in _POLITE]
    if not core:
        return any(w in ("thanks", "thank", "cheers") for w in words)  # "Thank you." is; a bare "Okay." is not
    phrase = " ".join(core)
    return phrase in _DONE or (core[0] in ("no", "nope", "nah") and " ".join(core[1:]) in _DONE)


MAX_WORDS = 12  # a longer reply is a request, not a goodbye
_LEAD_IN = frozenset("hey jarvis ok okay and but so also then please actually well right now no nope nah".split())
_ASKING = frozenset(
    "what whats how hows which who whos whose when where wheres why can could would will should shall is are was were "
    "do does did have has am may might "
    "check show give tell list get run look find read search open start restart report summarise summarize make create "
    "add remove update compare explain describe count ping scan test verify ingest propose remind note save turn set "
    # what the owner tells Jarvis to do with the desktop and the notes: "Close the lab pulse window."
    "close move tile cascade arrange minimise minimize maximise maximize restore hide put place write append edit "
    "change correct replace delete rename record bring switch use try repeat reboot "
    "yes yeah yep yup sure".split()
)


_GO_AHEAD = ("go ahead", "go on", "do it", "do that", "please do")
# "Stop asking me that." and "You just did it again." are about what Jarvis does, and are never a goodbye.
_COMPLAINT = re.compile(
    r"\byou (?:just|keep|kept|still|didn't|haven't|promised)\b"
    r"|\b(?:did|doing|done|said|asked|saying|asking) (?:it|that|this|me)(?: \w+)? again\b"
    r"|\b(?:stop|quit) (?:asking|saying|doing|offering|repeating|that|it)\b"
    r"|\b(?:don't|dont|do not|never) (?:ask|say|do|offer|repeat|call|use)\b"
)
_SENTENCE_BREAK = re.compile(r"[.!?]+\s+")
# What a second sentence must start with to count as an instruction: "Please check your settings."
_INSTRUCTING = frozenset(
    "check show give tell list get run look find read search open start restart report summarise summarize make create "
    "add remove update compare explain describe count ping scan test verify ingest propose remind note save set".split()
)


def _opening(text: str) -> list[str]:
    words = re.findall(r"[a-z']+", text)
    while words and words[0] in _LEAD_IN:
        words.pop(0)
    return words


def looks_like_request(said: str) -> bool:
    """A question, an instruction, a yes or a complaint. Such a reply is never a goodbye, whatever a model might think."""
    if "?" in said:
        return True
    text = said.lower().replace("\u2019", "'")
    if _COMPLAINT.search(text):
        return True
    first, *later = _SENTENCE_BREAK.split(text)
    words = _opening(first)
    if words:
        start = words[0].replace("'", "") if words[0] in ("what's", "how's", "where's", "who's") else words[0]
        if start in _ASKING or " ".join(words).startswith(_GO_AHEAD):
            return True
    return any(words and words[0] in _INSTRUCTING for words in map(_opening, later))


def worth_judging(previous_reply: str, said: str) -> bool:
    """Only a short reply to something Jarvis asked or offered can be a sign that the person is done."""
    return bool(said.strip()) and len(said.split()) <= MAX_WORDS and expects_answer(previous_reply)


JUDGE_SYSTEM = """You label one reply in a conversation between an assistant and its owner. The assistant has just asked or offered something. Decide what the owner's reply means.

"nothing_more": the owner declines the offer, or says they need nothing further, are finished or all set, tells the assistant to stand down, or says goodbye.
"something": the owner asks a question, asks for anything, names a thing to look at, or answers the assistant's question. If you are not sure, it is "something".

Examples:
Assistant: Would you like me to check the switch as well? Owner: I'm all set. -> nothing_more
Assistant: Is there anything else you need? Owner: I don't need anything. -> nothing_more
Assistant: Shall I look at the backups too? Owner: You can stand down for the night. -> nothing_more
Assistant: Which node do you mean? Owner: Forget it, never mind. -> nothing_more
Assistant: Would you like the details? Owner: That does it for today. -> nothing_more
Assistant: Are there any other issues you'd like me to check? Owner: We're finished here. -> nothing_more
Assistant: Are there any other issues you'd like me to check? Owner: The overall service health. -> something
Assistant: Which node do you mean? Owner: The first one. -> something
Assistant: Which node do you mean? Owner: pve2 -> something
Assistant: Would you like the details? Owner: Just the failed ones. -> something
Assistant: Shall I look at the backups too? Owner: The switch first. -> something
Assistant: What would you like to ask? Owner: The firewall uptime. -> something
Assistant: Which node do you mean? Owner: That is not what I asked. -> something
Assistant: Which one would you like? Owner: I told you a moment ago. -> something

Reply with JSON only."""
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"owner_wants": {"type": "string", "enum": ["nothing_more", "something"]}},
    "required": ["owner_wants"],
}


def _asked(previous_reply: str) -> str:
    """The end of Jarvis's last reply: the question or offer the person is answering."""
    sentences = [s.strip() for s in _SENTENCE_END.split(speakable(previous_reply)) if s.strip()]
    return " ".join(sentences[-2:])[-300:]


async def judged_done(backend: Any, previous_reply: str, said: str) -> bool:
    """Ask the local model whether this reply is the person signing off. Any doubt or failure means no."""
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": f"Assistant: {_asked(previous_reply)} Owner: {' '.join(said.split())} ->"},
    ]
    reply = ""
    try:
        async for event in backend.chat(messages, [], format=JUDGE_SCHEMA):
            if event["type"] == "text":
                reply += event["text"]
        return json.loads(reply).get("owner_wants") == "nothing_more"
    except Exception:  # an unreachable model or a malformed reply: carry on with a normal turn
        return False


async def closing(backend: Any, previous_reply: str, said: str) -> str | None:
    """"phrase" or "model" when this reply ends the conversation, None when it does not."""
    if not worth_judging(previous_reply, said) or "?" in said or any(ch.isdigit() for ch in said):
        return None  # a question, or an answer with a number in it ("101, thanks."), is never a goodbye
    if is_dismissal(said):
        return "phrase"
    if looks_like_request(said) or backend is None:
        return None
    return "model" if await judged_done(backend, previous_reply, said) else None


# ---- a check against the live model ----------------------------------------------------------------

_OFFER = "The backup server has one failed task in the last day: aptupdate. Are there any other issues you'd like me to check?"
_WHICH = "I found two nodes. Which one do you mean?"
CASES = [
    # (what Jarvis said last, what the person replies, is it a goodbye)
    (_OFFER, "No, I don't need anything.", True),
    (_OFFER, "Actually, I'm all set.", True),
    (_OFFER, "You can stand down for the evening.", True),
    (_OFFER, "That does it for tonight.", True),
    (_OFFER, "We're finished here.", True),
    (_OFFER, "Not right now, maybe later.", True),
    (_OFFER, "I think that covers it.", True),
    (_OFFER, "Go back to sleep.", True),
    (_OFFER, "As you were.", True),
    (_WHICH, "Forget it, never mind.", True),
    (_WHICH, "Leave it, I'll look myself.", True),
    (_OFFER, "No, that'll be all, thanks.", True),
    (_OFFER, "What's the overall service health?", False),
    (_OFFER, "The overall service health.", False),
    (_OFFER, "Backups.", False),
    (_OFFER, "The switch, and then DNS.", False),
    (_OFFER, "Yes please.", False),
    (_OFFER, "Just the Proxmox nodes.", False),
    (_OFFER, "Everything on the firewall.", False),
    (_OFFER, "I need the uptime of the firewall.", False),
    (_OFFER, "How many guests are running", False),
    (_WHICH, "The first one.", False),
    (_WHICH, "pve2", False),
    (_WHICH, "Both of them.", False),
    (_WHICH, "The one with the backup server on it.", False),
    (_WHICH, "Neither, show me the storage instead.", False),
    (_OFFER, "You just did it again. Please check your settings.", False),
    (_OFFER, "You said that already.", False),
    (_OFFER, "Stop asking me that.", False),
    (_OFFER, "Don't say that phrase to me.", False),
    ("I cannot see any information about that. Please provide more details.", "Close the lab pulse window.", False),
    (_OFFER, "Open the README.", False),
    (_OFFER, "Tile the windows.", False),
    (_OFFER, "Write that down in the switch note.", False),
    (_OFFER, "That was not my question.", False),
    (_WHICH, "I gave you the name a minute ago.", False),
    (_WHICH, "No, that's wrong, check your notes.", False),
]


async def _check() -> int:
    from .config import Settings
    from .llm import OllamaBackend

    settings = Settings.load()
    backend = OllamaBackend(settings.ollama_url, settings.model)
    wrong = harmful = 0
    for previous, said, goodbye in CASES:
        how = await closing(backend, previous, said)
        by = how or ("request shape" if looks_like_request(said) else "model")
        ok = (how is not None) == goodbye
        wrong += not ok
        harmful += how is not None and not goodbye
        print(f"{'ok   ' if ok else 'WRONG'}  {'goodbye' if how else 'request':8} by {by:13} {said}")
    print(f"{len(CASES) - wrong} of {len(CASES)} right; requests taken for a goodbye: {harmful}")
    return 1 if harmful else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_check()))
