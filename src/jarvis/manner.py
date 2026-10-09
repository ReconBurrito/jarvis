"""How a reply ends: with the answer, not with an offer.

A small model keeps closing its replies with "Is there anything else you need?" or "Would you like me to check
that?" whatever its instructions say, and then cannot do what it offered. So the rule is kept here, in code: an
offer of more help at the end of a reply is cut before anyone sees or hears it, and the conversation the model is
shown afterwards holds the reply as it was delivered.

Cutting something that was part of the answer is the worse mistake, so the list of what counts as an offer is
narrow: a whole sentence in one of a few fixed shapes, with no quotation in it. A question the model needs
answered to carry on ("Which node do you mean?", "Tiled or cascaded?") is left alone, and so is a reply that
consists of nothing but the offer.
"""

from __future__ import annotations

import re
from typing import Callable

_MORE = r"(?:anything|something) (?:else|more|further)"
_REST = r"[^,;:]*"  # one clause, to the end of the sentence
_WANT = r"(?:need|want|would like|'d like|require|wish)"
_TO_ME = r"(?:you(?:'d| would) like me to|you want me to|you need me to|i (?:can|could|should) (?:help|do|check|assist|look))"
_OFFERS = tuple(re.compile(p) for p in (
    # Is there anything else?  Is there anything else you need?  Will there be anything else?
    rf"^(?:is|was|will|would) there (?:be )?{_MORE}(?: (?:that )?(?:you|i|we)\b{_REST})?\?$",
    # Is there anything specific you'd like me to check?  Is there anything I can help with?
    rf"^(?:is|was) there (?:anything|something) (?:specific |in particular )?{_TO_ME}\b{_REST}\?$",
    # Are there any other issues you'd like me to check?  Any other questions?
    rf"^are there any (?:other|more|further) (?:\w+ )?{_TO_ME}\b{_REST}\?$",
    r"^(?:are there |do you have |did you have )?any (?:other|more|further) (?:questions|requests|issues|concerns)\?$",
    rf"^(?:do|did) you (?:need|want|require) {_MORE}\b{_REST}\?$",
    rf"^(?:would|will) you like {_MORE}\b{_REST}\?$",
    rf"^(?:can|may|could) i (?:help|assist)\b{_REST}\?$",
    rf"^(?:need |want )?anything else(?: (?:you|i)\b{_REST})?\?$",
    rf"^how else (?:can|may|could) i\b{_REST}\?$",
    rf"^what else (?:can|may|could) i\b{_REST}\?$",
    rf"^how (?:can|may) i (?:help|assist|be of)\b{_REST}\?$",
    r"^how (?:would|do) you (?:like|want|wish)(?: me)? to proceed\?$",
    r"^what (?:would you like|do you want|do you need)(?: me)? to do(?: next| now)?\?$",
    r"^(?:is|will) that (?:be )?all\?$",
    # Would you like me to check the repository settings?  Do you want me to look?  Would you like the details?
    rf"^(?:would|will) you like me to\b{_REST}\?$",
    rf"^(?:do you )?want me to\b{_REST}\?$",
    rf"^would you like to (?:see|hear|know|view|read)\b{_REST}\?$",          # Would you like to see the updated document?
    rf"^would you like (?:the |more |further |additional |some )?(?:details?|information)(?: (?:on|about|of|for)\b{_REST})?\?$",
    # Let me know if you need anything else.  Let me know if there is anything more.
    rf"^let me know (?:if|should) (?:you {_WANT}|there(?:'s| is))\b{_REST}\b(?:anything|something|more|further|else|help|assistance|questions|details?)\b{_REST}$",
    r"^let me know how (?:you would|you'd) like(?: me)? to proceed[.!]?$",
    r"^let me know[.!]?$",
    rf"^(?:feel free|don't hesitate|do not hesitate) to (?:ask|reach out|let me know)(?: me)?(?: (?:if|whenever|should)\b{_REST})?[.!]?$",
    # If you need anything else, just ask.
    rf"^if (?:you (?:{_WANT}|have)|there(?:'s| is)) (?:anything|something|any|more|further)\b{_REST}, (?:please |just )*(?:let me know|ask|ask me|feel free to ask)[.!]?$",
    rf"^i(?:'m| am|'ll be| will be) (?:here|available) (?:if|whenever|should) you (?:need|want|have|require)\b{_REST}$",
    rf"^(?:i(?:'m| am|'d be| would be) )?(?:happy|glad) to (?:help|assist)(?: (?:with|if|further|more)\b{_REST})?[.!]?$",
    r"^ask me to\b[^,;:]*\bfor more (?:details?|information)[.!]?$",  # Please ask me to check the logs for more details.
    r"^i hope (?:that|this) helps[.!]?$",
    # If you would like to know more about the tools I have access to, I can tell you that.
    rf"^if you(?:'d| would) like\b{_REST}, (?:just ask(?: me)?|(?:i|let me) (?:can|could|will|'ll|would be happy to|know)\b{_REST})[.!]?$",
))
_LEAD = re.compile(r"^(?:(?:please|just|and|so|also|otherwise|if so|if not),? )+")
MAX_WORDS = 22  # an offer is short
# What makes a sentence more than an offer: a second clause that says something, a choice, a number, an aside.
_MORE_THAN_AN_OFFER = re.compile(
    r"[;:()\[\]–—…]| - |\d|[.!?]+\s*[^\s.!?]|\b(?:but|though|although|however|because|since|which|whereas|except|unless)\b"
)
_DOUBLE = "\"“”"
_SINGLE = re.compile(r"(?<![A-Za-z0-9])['‘’]|['‘’](?![A-Za-z])")  # a quote mark, not an apostrophe
_OPENS = re.compile(r"[(\[“]|(?<![A-Za-z0-9])['‘](?=\w)")
_CLOSES = re.compile(r"[)\]”]|(?<=[\w.!?,])['’](?![A-Za-z])")
_LIST_NUMBER = re.compile(r"^\(?\d{1,2}[.)]$")
_TERMINATORS = ".!?"
_CLOSERS = "\"')]”’"


def _norm(sentence: str) -> str:
    return _LEAD.sub("", " ".join(sentence.lower().replace("’", "'").split()))


def is_offer(sentence: str) -> bool:
    """Is this sentence an offer of more help, as opposed to an answer or a question that needs answering?"""
    if any(mark in sentence for mark in _DOUBLE) or _SINGLE.search(sentence):
        return False  # words quoted from a log or a note are part of the answer, whatever they say
    text = _norm(sentence)
    if not text or len(text.split()) > MAX_WORDS or _MORE_THAN_AN_OFFER.search(text):
        return False
    if re.search(r"\bor\b", text) and re.search(rf"\b{_MORE}\b", text) is None and not text.startswith("ask me to"):
        return False  # "Would you like them tiled or cascaded?" is a choice the owner has to make
    return any(pattern.search(text) for pattern in _OFFERS)


LIVE_AFTER = 30  # words; a sentence this long is a listing, and is passed on as it is written from here


class Tail:
    """Passes a reply on sentence by sentence, so that each sentence can be judged before anyone sees it.

    A closing offer is held back and cut. With `reject`, a sentence for which it returns a reason is not shown,
    and neither is anything after it: `refused` then holds (the sentence, the reason). feed() returns the text
    that can be shown now. finish() ends the reply and returns what is still to be shown and the offer that was
    cut. release() gives everything back uncut, for text that is followed by a tool call.
    """

    def __init__(self, reject: Callable[[str], str | None] | None = None) -> None:
        self._reject = reject
        self.refused: tuple[str, str] | None = None
        self.dropped: tuple[str, str] | None = None  # a claim release() left out before a tool call
        self._after = ""         # the refused sentence and everything after it
        self._current = ""       # the sentence being written, with the space before it
        self._live = False       # the sentence is long and is being passed on as it comes
        self._ended = False      # the sentence has its full stop and waits for the space after it
        self._held = ""          # complete offers kept back, in case the reply goes on after them
        self._shown = False      # has anything of this answer been passed through
        self._quote = False      # inside a "quotation" that is still open
        self._depth = 0          # inside a bracket or a 'quotation' that is still open
        self._listing = False    # inside lines that follow a line ending in a colon: a list or lines quoted from a log
        self._numbered = False   # the sentence before this one was a list number

    def _pass(self, text: str) -> str:
        if text.strip():
            self._shown = True
        return text

    def _aside(self) -> bool:
        """Is the sentence being written inside a quotation, a bracket, a list item or lines quoted after a colon?"""
        return self._quote or self._depth > 0 or self._listing or self._numbered

    def _complete(self) -> str:
        """The current sentence is whole: refuse it, hold it as an offer, or pass it (and what was held) on."""
        out = ""
        aside = self._aside()
        if self._current.count('"') % 2:
            self._quote = not self._quote
        self._depth = max(0, self._depth + len(_OPENS.findall(self._current)) - len(_CLOSES.findall(self._current)))
        if not self._live:
            # A claim is judged wherever it stands, also in a list or after a colon; quoted words are left out by
            # the check itself. Only offers get the benefit of the doubt in an aside.
            why = self._reject(self._current) if self._reject is not None and self._current.strip() else None
            if why:
                self.refused = (" ".join(self._current.split()), why)
                self._after, self._held = self._held + self._current, ""
            elif not self._current.strip():
                if self._held:
                    self._held += self._current  # space after a held offer stays with it
                else:
                    out = self._current
            elif not aside and is_offer(self._current):
                self._held += self._current
            else:
                out = self._pass(self._held + self._current)
                self._held = ""
        if self._current.strip():
            self._numbered = bool(_LIST_NUMBER.match(self._current.strip()))
        self._current, self._live, self._ended = "", False, False
        return out

    def feed(self, text: str) -> str:
        out = []
        for ch in text:
            if self.refused is not None:
                self._after += ch
                continue
            if ch == "\n":
                written = self._current.strip()
                if written.endswith(":"):
                    self._listing = True
                elif not written:
                    self._listing = False  # an empty line ends the list
            if ch.isspace() and (self._ended or (ch == "\n" and self._current.strip())):
                out.append(self._complete())  # a full stop and a space, or the end of a line, ends a sentence
                if self.refused is not None:
                    self._after += ch
                    continue
            elif self._ended and ch not in _CLOSERS and ch not in _TERMINATORS:
                self._ended = False  # the stop was inside a word: 3.5, enterprise.proxmox.com
            if ch.isspace() and not self._current and not self._held:
                out.append(ch)  # the space after a sentence that was passed on goes with it, so it can be spoken at once
                continue
            self._current += ch
            if ch in _TERMINATORS and self._current.strip(_TERMINATORS + " \n\t"):
                self._ended = True
            if self._live:
                out.append(self._pass(ch))
            elif ch.isspace() and self._reject is None and len(self._current.split()) >= LIVE_AFTER:
                # Without a claim check a long listing is shown as it is written. With one, every sentence is judged
                # whole; speech waits for whole sentences anyway.
                out.append(self._pass(self._held + self._current))
                self._held, self._live = "", True
        return "".join(out)

    def release(self) -> str:
        """Text that leads into a tool call: given back whole, apart from a claim the check refuses, which is dropped
        together with what follows it (`refused` says what it was). What follows the tool call is judged as an
        answer of its own."""
        if self._reject is not None and self.refused is None and self._current.strip():
            why = self._reject(self._current)
            if why:
                self.refused = (" ".join(self._current.split()), why)
        out = self._held + self._current + self._after if self.refused is None else ""
        self.dropped = self.refused
        self._held, self._current, self._after, self._live, self._ended, self._shown = "", "", "", False, False, False
        self._quote, self._depth, self._listing, self._numbered, self.refused = False, 0, False, False, None
        return out

    def finish(self) -> tuple[str, str]:
        """End of the reply: (text still to be shown, the closing offer that was cut)."""
        out = self._complete() if self._current and self.refused is None else ""
        held, self._held = self._held, ""
        if not held or self.refused is not None:
            return out, ""
        if not self._shown:
            return out + self._pass(held), ""  # the reply is nothing but this; say it
        return out, held.strip()


def trim(reply: str) -> tuple[str, str]:
    """The reply without a closing offer, and what was cut."""
    tail = Tail()
    kept = tail.feed(reply)
    rest, cut = tail.finish()
    return kept + rest, cut


_YES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "please", "please do", "yes please", "go ahead", "go on",
    "do it", "do that", "yes do it", "yes do that", "yes go ahead", "sure go ahead", "ok do it", "okay do it", "ok go ahead",
    "okay go ahead", "alright do it", "alright go ahead",
})
_FILLER = frozenset("jarvis thanks thank you then well".split())
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_YES_OR_NO = re.compile(r"^(?:would|will|shall|should|do|did|does|can|could|may|is|are|was|were|have|has|want)\b", re.IGNORECASE)


def accepted(previous_reply: str, said: str) -> str | None:
    """When the person answers a yes-or-no question of Jarvis's with a bare yes, the question they said yes to."""
    if any(ch.isdigit() for ch in said):
        return None  # "Yes, 2." says more than yes
    words = re.sub(r"[^a-z ]", "", said.lower()).split()
    if " ".join(w for w in words if w not in _FILLER) not in _YES:
        return None
    sentences = [s.strip() for s in _SENTENCES.split(previous_reply.strip()) if s.strip()]
    if not sentences:
        return None
    last = sentences[-1]
    if not last.endswith("?") or not _YES_OR_NO.match(last) or re.search(r"\bor\b", last):
        return None  # "Which one?" and "Tiled or cascaded?" are not answered by a yes
    if re.search(rf"\b{_MORE}\b", last.lower()):
        return None  # a yes to "anything else?" names nothing to do
    return last[-300:]
