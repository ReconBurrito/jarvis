"""Which paths are notes, and which of them Jarvis may change."""

from __future__ import annotations

import hashlib
import re

from .repo import BrainError

MAX_NOTE_CHARS = 400_000
NOTE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./-]{0,190}\.md$")


def version(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def check_path(path: str) -> str:
    """A note path: a Markdown file, plainly named, nothing hidden."""
    parts = path.split("/")
    if not NOTE_PATH.match(path) or any(part in ("", ".", "..") or part.startswith(".") or part != part.strip() for part in parts):
        raise BrainError(f"not a note path: {path!r}")
    return path


def read_only(path: str) -> bool:
    return path.startswith("raw/")  # sources are kept exactly as they were filed
