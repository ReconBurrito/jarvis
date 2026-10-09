"""Jarvis's own hand on its notes: add lines to a note, or replace one passage in it.

Two small tools, and nothing wider. They cannot create, rename or delete a note, a replacement is limited to a
short passage, and every change is one commit that can be undone.

An ordinary note is changed on main, in Jarvis's name. The files that shape how Jarvis behaves in every
conversation (the standing files and the skills) are never changed this way: the same edit goes to a proposal
branch, one per file, and takes effect when the owner merges it. Raw sources are never edited, nothing is written
through a link, and nothing that looks like a secret is written. Those rules live in BrainRepo; these tools cannot
get round them. Reading, changing and committing happen in one locked step there, so a change made at the same
moment by the owner or by another call is never lost, and a call that reports an error has changed nothing.

What the model passes is treated as untrusted: a note is named exactly (its path or its file name), never by a
loose match, because writing to the wrong note is worse than asking again.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from ..audit import AuditLog, digest
from ..tools import TIER_READ_ONLY, Tool
from .paths import MAX_NOTE_CHARS, check_path, read_only
from .repo import PROTECTED_FILES, BrainError, BrainRepo, is_protected

MAX_ADD = 4000      # characters added in one call
MAX_FIND = 600      # characters of the passage to replace
MAX_NAME = 200
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
ITEM = ("- ", "* ", "+ ")
_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # control characters; a tab and a line break are fine
_AROUND = re.compile(r"^(?:the|my|your|our)\s+|\s+(?:note|notes|file|page|document)$", re.IGNORECASE)


def _key(text: str) -> str:
    return re.sub(r"[\s_-]+", "-", text.strip().casefold())


def find_exact(wanted: str, notes: list[str]) -> tuple[str | None, list[str]]:
    """The note that is named: by its path, or by its file name when only one note has it. (path, near misses)

    "the switch note" and "Switch" name wiki/switch.md, unless a note is called "switch note" itself. A word that
    is only part of a name does not name a note.
    """
    asked = wanted.strip().strip("\"'/")
    for path in (asked, asked + ".md"):
        if path in notes:
            return path, []
    stem = lambda path: re.sub(r"\.md$", "", path.rsplit("/", 1)[-1])  # noqa: E731
    names = [asked]
    shorter = asked
    while (shorter := _AROUND.sub("", shorter, count=1).strip()) and shorter != names[-1]:
        names.append(shorter)
    for name in names:
        bare = re.sub(r"\.md$", "", name, flags=re.IGNORECASE)
        for same in (lambda p: stem(p) == bare or re.sub(r"\.md$", "", p) == bare,          # exactly as it is written
                     lambda p: _key(stem(p)) == _key(bare) or _key(re.sub(r"\.md$", "", p)) == _key(bare)):
            found = [p for p in notes if bare and same(p)]
            if len(found) == 1:
                return found[0], []
            if found:
                return None, found[:12]
    key = _key(re.sub(r"\.md$", "", names[-1], flags=re.IGNORECASE))
    return None, [p for p in notes if key and key in _key(p)][:12]


def proposal_name(path: str) -> str:
    """The one proposal branch that holds Jarvis's pending changes to this file."""
    slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"\.md$", "", path.lower())).strip("-") or "file"
    if path in PROTECTED_FILES:
        return f"note-{slug}"
    # Two skill files can share a slug (a-b and a/b), so the path itself is part of the name.
    return f"note-{slug[:40].rstrip('-')}-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:8]}"


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line, level, title) of every heading that is not inside a code block."""
    found, fence = [], None
    for i, line in enumerate(lines):
        mark = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence is None and mark:
            fence = mark.group(1)
        elif fence is not None and mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence) \
                and not line.strip()[len(mark.group(1)):].strip():
            fence = None
        elif fence is None and (m := HEADING.match(line)):
            found.append((i, len(m.group(1)), m.group(2)))
    return found


def _split(note: str) -> tuple[list[str], str]:
    """The note's lines without the line breaks at its end, and those line breaks, which are kept as they are."""
    body = note.rstrip("\n")
    return (body.split("\n") if body.strip() else []), note[len(body):]


def add_text(note: str, text: str, under: str = "") -> str:
    """The note with the text added at its end, or at the end of the section with that heading.

    Nothing else in the note changes, not even the empty lines at its end."""
    lines, ending = _split(note)
    ending = ending or "\n"
    addition = text.strip("\n").split("\n")
    if not under.strip():
        # A list item goes straight under a list; anything else gets an empty line before it.
        joined = bool(lines) and lines[-1].lstrip().startswith(ITEM) and addition[0].lstrip().startswith(ITEM)
        return "\n".join(lines + ([""] if lines and not joined else []) + addition) + ending
    headings = _headings(lines)
    wanted = re.sub(r"[^\w]+", "", under.casefold())
    title = lambda h: re.sub(r"[^\w]+", "", h[2].casefold())  # noqa: E731
    found = [h for h in headings if wanted and title(h) == wanted]
    if not found and len(wanted) >= 3:
        found = [h for h in headings if title(h).startswith(wanted)]
    if len(found) != 1:
        names = ", ".join(h[2] for h in headings)[:400] or "none"
        raise BrainError(f"there is {'no' if not found else 'more than one'} heading like {under[:60]!r}; the headings are: {names}")
    start = found[0][0]
    # The end of the section's own text: before the next heading of any level, so the text does not land in a
    # subsection.
    end = next((i for i, _, _ in headings if i > start), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    joined = lines[end - 1].lstrip().startswith(ITEM) and addition[0].lstrip().startswith(ITEM)
    tail = lines[end:]
    return "\n".join(lines[:end] + ([] if joined else [""]) + addition + ([""] if tail and tail[0].strip() else []) + tail) + ending


def replace_text(note: str, find: str, replace: str) -> str:
    """The note with the one place where `find` stands replaced. Nothing else in the note changes."""
    places = [m.start() for m in re.finditer(f"(?={re.escape(find)})", note)]
    if not places:
        raise BrainError("those words are not in the note; read it with notes_read and give the exact text, a whole line if you can")
    if len(places) > 1:
        raise BrainError(f"those words are in the note {len(places)} times; give more of the line so that only one place matches")
    changed = note[:places[0]] + replace + note[places[0] + len(find):]
    if not changed.strip():
        raise BrainError("that would leave the note empty; a note is not deleted this way")
    return changed


def _text(arguments: dict[str, Any], key: str, limit: int, what: str) -> str:
    value = arguments.get(key, "")
    if not isinstance(value, str):
        raise BrainError(f"{key} must be plain text")
    if len(value) > limit:
        raise BrainError(f"{what} is too long; keep it under {limit} characters")
    if _FORBIDDEN.search(value) or "\r" in value:
        raise BrainError(f"{key} holds control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise BrainError(f"{key} is not valid text") from None
    return value


def make_note_write_tools(
    repo: BrainRepo,
    audit: AuditLog,
    changed: Callable[[str, str], Any] | None = None,
) -> list[Tool]:
    """`changed(user, path)` is called after a note was changed on main, so an open desktop can show it."""

    async def locate(wanted: str) -> str:
        if not repo.ready:
            await repo.start()
        if not repo.ready:
            raise BrainError(f"the notes are not available: {repo.detail}")
        notes = repo.notes()
        path, near = find_exact(wanted, notes)
        if path is None:
            listed = near or [p for p in notes if not p.startswith(("raw/", "memory/"))][:40]
            raise BrainError(f"there is no note called {wanted[:80]!r}" + ("; did you mean one of: " if near else "; the notes are: ") + ", ".join(listed))
        check_path(path)
        if read_only(path):
            raise BrainError(f"{path} is a raw source and is never edited")
        return path

    async def store(session: str, path: str, how: str, change: Callable[[str], str]) -> dict[str, Any]:
        user = session.split(":", 1)[0]
        made: dict[str, Any] = {}

        def bounded(text: str) -> str:
            new = change(text)
            if len(new) > MAX_NOTE_CHARS:
                raise BrainError("the note would be too large")
            made["chars"], made["digest"] = len(new), digest(new)
            return new

        record = {"user": user, "by": "jarvis", "path": path, "how": how, "protected": is_protected(path)}
        if is_protected(path):
            branch = await repo.edit_proposal(proposal_name(path), path, bounded, f"proposed by Jarvis: {how} in {path}")
            audit.append("note_proposed", {**record, **made, "branch": branch})
            return {
                "done": False, "proposed": True, "path": path, "branch": branch,
                "note": (f"{path} shapes how you behave, so nothing has changed yet. Tell the owner the change waits on "
                         f"the branch {branch} and takes effect when they merge it."),
            }
        commit = await repo.edit(path, bounded, f"jarvis: {how} in {path}")
        audit.append("note_changed", {**record, **made, "commit": commit})
        if changed is not None:
            changed(user, path)
        return {"done": True, "path": path, "commit": commit}

    async def note_add(arguments: dict[str, Any], session: str) -> dict[str, Any]:
        try:
            text = _text(arguments, "text", MAX_ADD, "the addition").strip("\n")
            under = _text(arguments, "under", MAX_NAME, "the heading")
            if not text.strip():
                raise BrainError("there is nothing to add")
            path = await locate(_text(arguments, "note", MAX_NAME, "the note's name"))
            return await store(session, path, "add", lambda note: add_text(note, text, under))
        except (BrainError, OSError) as exc:
            return {"error": str(exc), "done": False}

    async def note_replace(arguments: dict[str, Any], session: str) -> dict[str, Any]:
        try:
            find = _text(arguments, "find", MAX_FIND, "the passage to replace")
            replace = _text(arguments, "replace", MAX_ADD, "the replacement")
            if not find.strip():
                raise BrainError("say which words to replace")
            if find == replace:
                raise BrainError("find and replace are the same, so nothing would change")
            path = await locate(_text(arguments, "note", MAX_NAME, "the note's name"))
            return await store(session, path, "replace", lambda note: replace_text(note, find, replace))
        except (BrainError, OSError) as exc:
            return {"error": str(exc), "done": False}

    outcome = (
        "The result says what happened: done true means the note is changed; proposed true means nothing has changed "
        "yet and the owner must merge a branch, and you say exactly that; an error means nothing happened."
    )
    return [
        Tool(
            name="note_add",
            description=(
                "Add lines to one of your notes when the owner asks you to write something down in it. Name the note by "
                "its path or its file name. The text is added at the end of the note, or at the end of the section named "
                "in under. Write the text as it should stand in the note; a list item starts with a hyphen. Use it with "
                "the note USER, and one short line, when the owner tells you to always or never do something. " + outcome
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The note's path or file name, for example wiki/switch.md, switch or README"},
                    "text": {"type": "string", "description": "The lines to add"},
                    "under": {"type": "string", "description": "Optional: the heading of the section to add them to"},
                },
                "required": ["note", "text"],
            },
            # Tier 0 means "changes nothing in the lab". The notes repository is Jarvis's own notebook, under git, and the owner
            # approved these two writes; what steers Jarvis still goes through a proposal.
            tier=TIER_READ_ONLY,
            handler=note_add,
            with_session=True,
            private=("text", "under"),
        ),
        Tool(
            name="note_replace",
            description=(
                "Correct one of your notes when the owner asks: replace one short passage with another. Read the note "
                "with notes_read first and give find exactly as it stands in the note, a whole line if you can; it must "
                "match in one place only. An empty replace removes the passage. " + outcome
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The note's path or file name, for example HEARTBEAT or wiki/switch.md"},
                    "find": {"type": "string", "description": "The exact words in the note to replace"},
                    "replace": {"type": "string", "description": "What to put in their place"},
                },
                "required": ["note", "find", "replace"],
            },
            tier=TIER_READ_ONLY,
            handler=note_replace,
            with_session=True,
            private=("find", "replace"),
        ),
    ]
