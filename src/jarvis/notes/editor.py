"""What the notes window may read and write.

The window is the owner's own hands on the notes, in Jarvis's panel on their desktop. It lists the notes, opens
one, and saves one. A save names the version of the text it started from, so an edit never silently overwrites a
change made somewhere else in the meantime (by Jarvis, or on the remote).
"""

from __future__ import annotations

from typing import Any

from .paths import MAX_NOTE_CHARS, check_path, read_only, version
from .repo import BrainError, BrainRepo, is_protected

OWNER = "Owner"   # the author of what is saved from the window; the committer stays Jarvis


class EditConflict(BrainError):
    pass


def tree(repo: BrainRepo) -> dict[str, Any]:
    notes = []
    for path in repo.notes():
        try:
            check_path(path)
            size = repo.resolve(path).stat().st_size
        except (BrainError, OSError):
            continue   # a file the window could not open anyway (an odd name, or a link out of the notes)
        notes.append({"path": path, "size": size, "protected": is_protected(path), "read_only": read_only(path)})
    return {"detail": repo.detail, "notes": notes}


def open_note(repo: BrainRepo, path: str) -> dict[str, Any]:
    raw = repo.resolve(check_path(path))
    if not raw.is_file():
        raise BrainError(f"no such note: {path}")
    data = raw.read_bytes()
    try:
        text, lossy = data.decode("utf-8"), False
    except UnicodeDecodeError:
        # Shown as well as it can be, but never saved from the window: a save would replace what could not be read.
        text, lossy = data.decode("utf-8", errors="replace"), True
    return {"path": path, "text": text, "version": version(text), "protected": is_protected(path),
            "read_only": read_only(path) or lossy, "lossy": lossy}


async def save_note(repo: BrainRepo, path: str, text: str, base: str) -> dict[str, Any]:
    """Save the owner's edit. `base` is the version the edit started from, or "" for a note that is new."""
    check_path(path)
    if read_only(path):
        raise BrainError(f"{path} is a source and is never edited")
    if len(text) > MAX_NOTE_CHARS:
        raise BrainError("the note is too large")
    if "\x00" in text:
        raise BrainError("the note holds a character that is not text")
    await repo.start()   # pick up what changed elsewhere before comparing versions
    if not repo.ready:
        raise BrainError(f"the notes are not available: {repo.detail}")
    target = repo.resolve(path)
    if target.is_file():
        try:
            target.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise BrainError(f"{path} is not plain UTF-8 text; change it by hand") from None

    def unchanged() -> None:
        # Run by the repository with its lock held, so nothing can change the note between this check and the save.
        if target.is_file():
            if version(repo.read(path)) != base:
                raise EditConflict(f"{path} changed since you opened it")
        elif base:
            raise EditConflict(f"{path} no longer exists")

    body = text.replace("\r\n", "\n")
    body = body if body.endswith("\n") else body + "\n"
    commit, now = await repo.save_as_owner({path: body}, f"edit: {path}", OWNER, unchanged=unchanged)
    # Taken under the repository's lock, after the save reached the remote: the version names exactly this text,
    # which can hold a change made elsewhere and merged in. The window shows it, so nothing is dropped later.
    text = now[path]
    return {"path": path, "commit": commit, "version": version(text), "text": text, "protected": is_protected(path)}
