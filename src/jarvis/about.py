"""What Jarvis is and how it works, in words the model is given at the start of every conversation.

A language model does not know what program it is running in. Left to guess, it claims abilities it does not
have ("I have saved the document"). This text is built from the tools that are really registered, so what it
says Jarvis can do is always what Jarvis can do, and it changes by itself when a tool is added or removed.
"""

from __future__ import annotations

import os
from typing import Iterable

# Where Jarvis runs, in words (JARVIS_HOST_DESCRIPTION in site.env), so it never describes a machine it has left.
HOST = "JARVIS_HOST_DESCRIPTION"

# (tools that give the ability, what it is)
_CAN = (
    (("local_status",), "read the state of the machine you run on"),
    (("proxmox_",), "read the state of the Proxmox cluster, its nodes, guests and storage"),
    (("pbs_",), "read the backup server: backups, datastores, and failed tasks with the end of their logs"),
    (("opnsense_",), "read the state of the OPNsense firewall"),
    (("dns_",), "check whether the DNS servers answer"),
    (("switch_",), "read the state of the network switch"),
    (("notes_",), "search and read your notes"),
    (("note_add", "note_replace"), "add lines to a note (note_add) and replace one passage in a note (note_replace)"),
    (("desktop_",), "open, close, move and arrange the windows on the owner's desktop"),
    (("browser_",), "see which tabs are open in the web browser on the owner's desktop, open a web page there "
                    "(browser_open) and read the text of a page that is open (browser_read)"),
)
# (tools that would give the ability, what you cannot do while none of them is there)
_CANNOT = (
    ((), "change anything in the lab: you cannot start, stop, restart, update, fix, silence or ignore anything there"),
    (("desktop_",), "open, close or move a window, or see the owner's screen"),
    (("note_add", "note_replace"), "write to or change a note, or keep anything in mind for a later conversation"),
    (("web_", "browser_"), "look anything up on the internet"),
    ((), "send email or messages to anyone"),
    ((), "change how you behave by deciding or promising to: that takes a change the owner approves"),
)
# How the owner reaches Jarvis when the caller does not say.
SURFACE = "The owner talks to you by typing."


def _has(names: list[str], wanted: tuple[str, ...]) -> bool:
    return any(name == want or (want.endswith("_") and name.startswith(want)) for name in names for want in wanted)


def describe(tools: Iterable[str], model: str = "", where: str | None = None, surface: str = SURFACE,
             lab: str = "") -> str:
    """lab: a sentence on why the lab's own tools are missing, when they are (the vault is locked, say)."""
    where = (where if where is not None else os.environ.get(HOST, "")).strip() or "a computer in the owner's lab"
    names = sorted(tools)
    can = [what for wanted, what in _CAN if _has(names, wanted)]
    cannot = [what for wanted, what in _CANNOT if not wanted or not _has(names, wanted)]
    if _has(names, ("desktop_",)):
        cannot.append("scroll, type, click or select inside a window: with windows you can only open, close, move and arrange them")
    if _has(names, ("browser_",)):
        cannot.append("click, type, scroll or sign in on a web page, or open the brain, the lab or the desktop itself in the browser")
    if _has(names, ("note_add", "note_replace")):
        cannot.append("edit a note in any other way than note_add and note_replace: you cannot rewrite, rename, create or delete one")
    lasts = "your notes and an audit log of your turns and tool calls" if _has(names, ("notes_",)) else "an audit log of your turns and tool calls"
    lines = [
        "What you are and how you work:",
        f"You are a program called Jarvis that runs on {where}. Your words come from a language "
        f"model on that same machine{f' ({model})' if model else ''}. You have no hands of your own. Everything you do, you do by "
        "calling one of your tools. An action of yours has happened only when a tool has reported it in this turn: "
        "never say you did something that no tool reported. (What happens in the lab is a different matter: there you "
        "report what your tools show.)",
        f"You do not remember earlier conversations. What lasts is {lasts}. {surface}".rstrip(),
        "What you can do: " + "; ".join(can) + "." if can else "You have no tools at the moment, so you can only talk.",
        "What you cannot do: " + "; ".join(cannot) + ".",
        *([lab] if lab else []),
        "When asked for something you cannot do, say in one sentence what stands in the way and name the nearest "
        "thing you can do. When a request has several parts, do the parts you can with your tools and say plainly "
        "which part you could not do. When asked what you are, how you work or what you can do, answer from this "
        "description.",
    ]
    return "\n".join(lines)
