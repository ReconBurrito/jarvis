"""Nothing is claimed that no tool reported.

A small model will say "I have added the line and saved the document" with no tool behind it. The prompt forbids
that and the model does it anyway, so the rule is enforced here: a sentence that claims an action is checked
against what the tools reported in this turn. A claim with nothing behind it is not shown. The model is told so
and gets one more try, in which it can call the right tool or say that it cannot; if it claims again, Jarvis
says in fixed words that the thing did not happen.

Only claims about Jarvis's own actions are judged: what it wrote, what it did to a window, what it did in the
lab, and what it promises about its own behaviour. Reports about the lab ("the guest was stopped at two") are
not claims of this kind and are never touched.
"""

from __future__ import annotations

import re
from typing import Any

# Only plain first-person claims about a few kinds of action are judged, each tied to the thing acted on. The
# patterns are narrow on purpose: refusing a true answer is worse than letting an odd claim through, and the
# self-description and the prompt still ask the model not to make one.
_I = r"\bi(?:'ve| have| had)?(?: (?:just|now|also|already|successfully))*"
_THE = r"(?:(?:the|your|a|an|that|this|my|its|our|new|one|both|those|these|all|whole|same|requested|following)\s+){0,3}"
# What a note is called. "the file server" or "the DNS entry" are not notes, so the noun must stand on its own.
_DOC = (r"(?:line|lines|note|notes|text|document|documents|readme|readme\.md|paragraph|sentence|heading|section|entry|"
        r"edit|edits|change|changes|wording|[a-z0-9_/-]+\.md)")
_WINDOW = r"(?:window|windows|widget|widgets|pulse|chat|desktop|notes window|status window)"
_LAB_THING = (r"(?:it|them|that|this|pve\w*|node|nodes|guest|guests|vm|vms|container|containers|ct\s*\d+|"
              r"vm\s*\d+|service|services|alert|alerts|error|errors|warning|warnings|task|tasks|backup|backups|"
              r"firewall|switch|port|ports|pihole[\w-]*|dns|server|servers|repository|update|updates|check|checks|"
              r"opnsense|proxmox|cluster|datastore|[a-z]+\d+)")
_AFTER = r"(?![^.;:!?]*\b(?:by(?! side)|since|ago|yesterday|earlier today|last (?:week|night|month))\b)"  # not someone else's, not history

_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hands", re.compile(rf"{_I} (?:scrolled|typed|clicked)\b|\b(?:and|now) scrolled (?:down|up|to)\b"
                         rf"|\bscrolled (?:down|up|to)\b[^.!?]*\bnow\W*$")),
    ("write", re.compile(
        rf"{_I} (?:added|saved|appended|inserted|written|wrote|edited|updated|changed|corrected|replaced|removed|deleted|"
        rf"recorded|stored|put|placed|logged|jotted)\s+(?:(?:to|in|into)\s+)?{_THE}(?:[\w'\"-]+\s+){{0,4}}?{_DOC}\b{_AFTER}"
        rf"|\b{_THE}{_DOC}(?:\s+\"[^\"]*\")?\s+(?:has|have)\s+(?:now\s+|also\s+|just\s+|successfully\s+)*been\s+(?:added|saved|appended|"
        rf"inserted|written|updated|changed|corrected|replaced|removed|deleted|recorded|stored)\b{_AFTER}"
        rf"|\b{_THE}{_DOC}\s+(?:is|are)\s+now\s+(?:saved|updated|added)\b"
        rf"|\b{_THE}(?:note|document|readme|file|[a-z0-9_/-]+\.md)\s+now\s+(?:contains|says|includes|ends with|reads|has)\b"
        rf"|\band (?:then )?saved (?:the|it|your)\b|\band (?:then )?(?:added|appended|inserted|wrote|updated|changed|corrected|"
        rf"replaced|removed|deleted)\s+{_THE}(?:[\w'\"-]+\s+){{0,2}}?{_DOC}\b{_AFTER}|{_I} saved and\b|{_I} (?:saved|updated) (?:it|that|this|them)\b{_AFTER}|^\W*(?:it|that|this) (?:has been|is now) saved\W*$|\bwent ahead and (?:added|saved|updated|changed|wrote)\b"
        rf"|^\W*(?:saved|added|appended|wrote) (?:the|it|that|your)\b"
        rf"|\b{_THE}{_DOC}\s+(?:is|are)\s+(?:now\s+)?(?:saved|updated)(?:\s+now)?\W*$"
        rf"|^\W*successfully (?:added|saved|updated|changed|wrote|written)\b"
        rf"|\b{_THE}{_DOC}\s+(?:was|were)\s+(?:added|saved|updated|changed|written)\s+successfully\b"
        rf"|{_I} (?:committed|pushed|merged)\b|\b{_THE}{_DOC}\s+(?:has|have)\s+been\s+(?:committed|pushed)\b")),
    ("action", re.compile(
        r"^\W*done,|^\W*(?:done|saved|added|all done|all set|it's done|it is done|that's done|that is done|consider it done|"
        r"line added|note updated|note saved|window closed)\W*$|\bi(?:'ve| have)? (?:made|done|applied) (?:the|that|this|it|"
        r"your) ?(?:change|changes|edit|edits)?\b|\bi(?:'ve| have) taken care of (?:it|that|this)\b")),
    ("desktop", re.compile(
        rf"{_I} (?:closed|opened|moved|minimi[sz]ed|maximi[sz]ed|restored|tiled|cascaded|arranged|resized)\s+{_THE}"
        rf"(?:[\w'-]+\s+){{0,2}}?{_WINDOW}\b{_AFTER}"
        rf"|{_I} (?:closed|moved|minimi[sz]ed|maximi[sz]ed|restored|tiled|cascaded|resized) (?:it|them)\b{_AFTER}"
        rf"|\b{_THE}{_WINDOW}\s+(?:has|have)\s+(?:now\s+|just\s+)?been\s+(?:closed|opened|moved|minimi[sz]ed|"
        rf"maximi[sz]ed|restored|tiled|cascaded|arranged)\b{_AFTER}|\b{_THE}{_WINDOW}\s+(?:is|are)\s+now\s+(?:closed|"
        rf"minimi[sz]ed|maximi[sz]ed|tiled|side by side)\b")),
    ("browse", re.compile(
        rf"{_I} (?:opened|visited|loaded|went to|navigated(?: to)?|browsed(?: to)?|searched(?: for)?|looked up|logged (?:in|into)|"
        rf"signed (?:in|into)|clicked|typed|entered|filled in|submitted|scrolled)\b[^.!?]{{0,80}}\b(?:web ?pages?|pages?|"
        rf"sites?|websites?|urls?|links?|search (?:bar|box|field|results)|browser|tabs?|forms?|buttons?|fields?|https?://|"
        rf"google|youtube|[a-z0-9-]+\.(?:com|org|net|io|dev|gov|edu|co|uk))\b"
        rf"|\band (?:then )?(?:typed|clicked|entered|submitted|searched for|pressed enter)\b"
        rf"|{_I} (?:adjusted|set|turned|changed|raised|lowered|maxed|muted|unmuted|paused|played|started|stopped)\b[^.!?]{{0,40}}\b(?:volume|sound|audio|video|playback)\b"
        rf"|\bthe browser (?:is|was) now (?:loading|showing|on|open)\b|\b(?:the )?(?:page|site|tab) is now (?:loading|open|showing)\b")),
    ("lab", re.compile(
        rf"{_I} (?:restarted|rebooted|silenced|suppressed|ignored|muted|upgraded|updated|shut down|turned off|"
        rf"switched off|acknowledged|fixed|repaired|disabled|applied)\s+{_THE}{_LAB_THING}\b"
        rf"|\bi(?:'ll| will| am going to|'m going to) (?:now )?(?:restart|reboot|silence|suppress|ignore|mute|upgrade|"
        rf"shut down|fix|repair|disable)\s+{_THE}{_LAB_THING}\b")),
    ("promise", re.compile(
        r"\bi(?:'ll| will| shall)(?: now| also)? (?:not|never|no longer) (?:ask|say|use|offer|mention|repeat|end|add|"
        r"include|bother)\b|\band (?:will|shall) (?:not|never|no longer) (?:ask|say|use|offer|mention|repeat|end)\b|\bi won't (?:ask|say|use|offer|mention|repeat|end|add|include|bother)\b"
        r"|\bfrom now on,? i(?:'ll| will| won't| shall)\b|\bin (?:the )?future,? i(?:'ll| will| won't| shall)\b"
        r"|\bi(?:'ll| will) (?:always (?:ask|say|use|offer|end|keep|answer|reply)|remember to)\b|\bi(?:'ll| will) remember (?:that|this|it)\W*$"
        r"|\bi(?:'ll| will)\b[^.!?]*\b(?:from now on|going forward|in future|next time)\b|\bgoing forward,? i(?:'ll| will| won't)\b"
        r"|\bi(?:'ll| will) not\b[^.!?]*\bagain\b|\bi won't\b[^.!?]*\bagain\b|^\W*will do\W*$"
        r"|\bi(?:'ve| have)? ?(?:noted|taken note of|made a note of|remembered) (?:(?:your|that|this|the) (?:instruction|"
        r"preference|request|wish|rule|feedback|point)\b|(?:it|that)\W*$)|^\W*noted\W*$|\bi(?:'ve| have) updated my (?:settings|behaviou?r|preferences)\b")),
)
# What stands behind which kind of claim: a tool that did the thing in this turn.
_BACKED_BY = {
    "write": lambda done: bool(done & {"note_add:done", "note_replace:done"}),
    "promise": lambda done: bool(done & {"note_add:proposed", "note_replace:proposed"}),
    "desktop": lambda done: bool(done & {"desktop_window:done", "desktop_arrange:done"}),
    "browse": lambda done: bool(done & {"web_browse:done", "web_research:done"}),
    "hands": lambda done: bool(done & {"web_browse:done"}),
    "action": lambda done: bool(done & {"note_add:done", "note_replace:done", "desktop_window:done", "desktop_arrange:done",
                                         "note_add:proposed", "note_replace:proposed"}),
}
SAID_INSTEAD = {
    "write": "I have not written or changed anything: none of my tools reported it.",
    "action": "Nothing was done: none of my tools reported it.",
    "desktop": "Nothing on the desktop was changed: none of my tools reported it.",
    "hands": "I cannot scroll, type or click inside a window, so that did not happen.",
    "browse": "I have not done that in the browser: no browsing task ran.",
    "lab": "I have not changed anything in the lab, and in this build I cannot.",
    "promise": "I cannot change how I behave by saying so. That takes a change you approve.",
}
CHECK_NOTE = (
    '(A check by your own program, not a message from the owner. Your draft said: "{sentence}" No tool reported '
    "that in this turn, so it is not true and it was not shown. {hint} If one of your tools can do what the owner "
    "asked, call it now. If none can, tell the owner in one sentence that you cannot do that part. Claim nothing "
    "that a tool did not report.)"
)
# kind: (the tools the hint names, the hint, the hint when none of those tools is there)
_HINTS = {
    "write": (("note_add", "note_replace"), "To change a note, call note_add or note_replace.", "You have no tool that writes to a note."),
    "action": (("note_add", "note_replace", "desktop_window", "desktop_arrange"),
               "To change a note, call note_add or note_replace; to change a window, call desktop_window or desktop_arrange.",
               "You have no tool that does that."),
    "desktop": (("desktop_window", "desktop_arrange"), "To change a window, call desktop_window or desktop_arrange.",
                "You have no tool that changes a window."),
    "hands": (("web_browse",), "You cannot scroll, type or click inside a window yourself; to do it in the web browser, call web_browse with the whole task.",
              "You cannot scroll, type or click inside a window."),
    "browse": (("web_browse",), "To do anything in the web browser, call web_browse with the whole task; to look something up, call web_research.",
               "You have no tool that uses a web browser."),
    "lab": ((), "You cannot change anything in the lab.", "You cannot change anything in the lab."),
    "promise": (("note_add",), "You cannot change your behaviour by promising. To put a standing wish forward, call note_add with the note USER and one short line.",
                "You cannot change your behaviour by promising, and you have no tool that keeps a wish for later."),
}


def worked(name: str, result: Any) -> str | None:
    """What a tool call did: "done", "proposed", or None when it failed or reported that nothing happened."""
    if not isinstance(result, dict) or "error" in result:
        return None
    if result.get("proposed") is True:
        return "proposed"
    return "done" if result.get("done") is True else None


# Words in quotation marks are not Jarvis's own claim. A single quoted word ("saved") is still part of the sentence.
_QUOTED = re.compile(r'"[^"]*\s[^"]*"|“[^”]*\s[^”]*”|(?<![a-z0-9])\'[^\']*\s[^\']*\'(?![a-z])')
_NEGATED = re.compile(
    r"\b(?:not|never|cannot|can't|couldn't|unable|didn't|haven't|hasn't|wasn't|weren't|won't be able|no tool|nothing|"
    r"none|no (?:line|note|notes|change|changes|window|windows))\b")
_PROMISE_NOT = re.compile(r"\b(?:i|and)(?:'ll| will| shall)(?: now| also)? (?:not|never|no longer)\b|\bi won't\b")


def claims(sentence: str) -> list[str]:
    """The kinds of action this sentence claims Jarvis took. Words it quotes are not its own claim."""
    text = " ".join(sentence.lower().replace("’", "'").replace("‘", "'").split())
    text = _QUOTED.sub(" ", text)
    found = []
    for kind, pattern in _CLAIMS:
        if not pattern.search(text):
            continue
        if _NEGATED.search(text) and not (kind == "promise" and _PROMISE_NOT.search(text)):
            continue  # "I have not saved anything" is the opposite of a claim
        found.append(kind)
    return found


class Claims:
    """What the tools have reported in this turn, and so what a reply may claim."""

    def __init__(self, available: list[str] | None = None) -> None:
        self._done: set[str] = set()
        self._available = set(available or [])

    def tool(self, name: str, result: Any) -> None:
        what = worked(name, result)
        if what:
            self._done.add(f"{name}:{what}")

    def unbacked(self, sentence: str) -> str | None:
        """The kind of claim this sentence makes with nothing behind it, or None when it may be said."""
        for kind in claims(sentence):
            backed = _BACKED_BY.get(kind)
            if backed is None or not backed(self._done):
                return kind
        return None

    def note(self, kind: str, sentence: str) -> str:
        tools, hint, without = _HINTS[kind]
        if tools and not any(tool in self._available for tool in tools):
            hint = without  # a hint must not send the model to a tool it does not have
        return CHECK_NOTE.format(sentence=" ".join(sentence.split())[:300], hint=hint)
