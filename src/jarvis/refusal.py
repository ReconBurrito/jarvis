"""Jarvis does not say a bare no.

A small model refuses too easily: "I'm sorry, I can't do that." and nothing more. The personality forbids it
and the model does it anyway, so the rule is kept here. A reply that opens with a refusal must also say what
stands in the way and offer the closest workable thing. One that does not is not shown: the model is told so
and gets one more try, in which it can call a tool after all or refuse properly. What it says the second time
is shown as it is, and the audit record says that a refusal was sent back.

Only the opening of a reply is judged, and only for whether a reason and a way forward are there at all. A
reply that reports what the lab shows, that says what Jarvis cannot see or know, that answers a question with
a plain no, or that asks which of two things was meant, is never touched.
"""

from __future__ import annotations

import re

# Words that come before the refusal itself, with whatever punctuation follows them: "I'm sorry. ", "No, ".
_SOFTENERS = (
    r"(?:(?:(?:i'm|i am) (?:sorry|afraid)|sorry|(?:i )?apologi[sz]e|my apologies|unfortunately|regrettably|no|alas|"
    r"with regret|sir)[\s.,;:!-]+(?:but |that )?)*"
)
# Not being able to see or know something is a report of what the tools show, not a refusal to do something.
# "I cannot tell stories" refuses; "I cannot tell why it failed" reports what the tools do not show.
_KNOWING = (r"(?!(?: to)? (?:see|find|know|determine|confirm|verify|be sure|be certain)\b)"
            r"(?!(?: to)? (?:tell|say)(?: you)?(?: for (?:sure|certain))? (?:whether|if|why|how|what|which|when|where|who|from|for (?:sure|certain))\b)")
_REFUSAL = re.compile(
    rf"^\W*{_SOFTENERS}(?:"
    rf"i (?:can't|cannot|can not|couldn't|could not|am unable|am not able)\b{_KNOWING}"
    r"|i (?:won't|will not|must decline|have to decline|must refuse|am not permitted|am not allowed|"
    r"do not have the (?:ability|means)|don't have the (?:ability|means)|lack the (?:ability|means))\b"
    rf"|i'm (?:unable|not able)\b{_KNOWING}"
    r"|i'm (?:not permitted|not allowed)\b"
    r"|(?:that|this|it)(?:'s| is| would be) (?:not possible|impossible|not something i can|beyond (?:me|my))\b"
    r"|(?:that|this|it) (?:can't|cannot|can not) be done\b"
    r")"
)
# A reason: why it cannot be done as asked.
_REASON = re.compile(
    r"\b(?:because|since|as (?:i|there|it|the|no|none|my)|requires?|needs?|would (?:need|require|take)|there(?:'s| is| are) no|"
    r"i (?:have|hold|see) no|i've no|i (?:do not|don't) have (?:a|any|the)|none of my|no tool|not (?:built|installed|"
    r"connected|configured|available|set up|reachable|permitted|allowed)|read-only|only (?:read|see|report)|lacks?|without|"
    r"until|yet|approval|approve[sd]?|stands? in the way|out of (?:reach|range)|does not exist|doesn't exist)\b"
)
# A way forward: the closest thing that can be done, stated. "I can" inside "I can't" is not one. An offer put
# as a question ("Shall I ...?") does not count either: the end of a reply is trimmed of such offers
# (jarvis.manner), and the bare no would be what is left.
_NOT = r"(?!'t|n't|not| not)"
_FORWARD = re.compile(
    rf"\b(?:instead|i (?:can|could|will){_NOT}\b|i'll|i have (?:put|asked|raised|queued)|you (?:can|could|may|might){_NOT}\b|"
    r"what i can do|the (?:closest|nearest)|alternatively|another way|if you (?:give|grant|add|approve|tell|name|want|"
    r"would like|install|connect)|i would need|i'd need|i need|once (?:you|it|the|that)|waits? for|waiting for|"
    r"for your approval|ask me to|try)\b"
)
_SENTENCE_END = re.compile(r"[.!?](?:[\"')\]]*)(?:\s|$)")

DECIDE_AFTER = 90   # characters; by then an opening shows whether it is a refusal
HOLD_AT_MOST = 700  # characters; a refusal that goes on this long has plainly said more than no

NOTE = (
    "(A check by your own program, not a message from the owner. Your draft refused without {missing}, so it was "
    "not shown. You do not say a bare no. If one of your tools can do what was asked, or part of it, call it now. "
    "If it truly cannot be done, say in one sentence what stands in the way, and in the next the closest thing "
    "that can be done: another way, a step the owner can take, or what you would need. State it plainly; do not "
    "ask whether the owner would like it.)"
)


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").replace("‘", "'").split())


def opens_with_refusal(text: str) -> bool:
    return _REFUSAL.search(_norm(text)) is not None


def missing(reply: str) -> str | None:
    """What a refusal lacks: words for the note to the model, or None when the reply may be shown."""
    text = _norm(reply)
    if not _REFUSAL.search(text):
        return None
    # The refusing clause itself is not its own reason or way forward ("I can't" holds "i can").
    rest = text[_REFUSAL.search(text).end():]
    reason, forward = _REASON.search(rest) is not None, _FORWARD.search(rest) is not None
    if reason and forward:
        return None
    if not reason and not forward:
        return "saying what stands in the way and without offering the closest thing that can be done"
    return "offering the closest thing that can be done" if reason else "saying what stands in the way"


class Opening:
    """Holds back the start of a reply until it is clear whether it opens with a refusal.

    feed() returns the text that may go on to the reader now. An opening that is no refusal is let through
    after its first sentence (or DECIDE_AFTER characters) and everything after it passes untouched. A refusal
    is held whole, up to HOLD_AT_MOST characters, so that it can be judged before anyone sees it: held() then
    gives the text, and take() hands it over and lets the rest pass.
    """

    def __init__(self) -> None:
        self._text = ""
        self._open = False      # decided: this reply passes through
        self._refusal = False   # decided: this reply opens with a refusal and is being held

    def feed(self, text: str) -> str:
        if self._open:
            return text
        self._text += text
        if self._refusal:
            return self.take() if len(self._text) > HOLD_AT_MOST else ""
        if _SENTENCE_END.search(self._text) or len(self._text) >= DECIDE_AFTER:
            if opens_with_refusal(self._text):
                self._refusal = True
                return ""
            return self.take()
        return ""

    def held(self) -> str:
        """The reply so far, when it is being held as a refusal (or was too short to decide); else nothing."""
        return "" if self._open else self._text

    def take(self) -> str:
        """Everything held, handed over; what follows passes through."""
        text, self._text, self._open = self._text, "", True
        return text
