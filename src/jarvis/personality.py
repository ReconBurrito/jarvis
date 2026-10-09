"""Who Jarvis is, in fixed words, and the short lines it says while its tools run.

The personality is a file, not something the model is asked to keep up: personality.md is read once and put
at the head of every conversation. What it says about refusals is also enforced in code (jarvis.refusal),
because a small model forgets an instruction sooner than a program forgets a check.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Said (or shown) while a tool call runs, so a wait is never silent. Short, in character, and true of any tool.
FILLERS = (
    "One moment.",
    "Checking.",
    "Cross-referencing.",
    "Looking into it.",
    "Consulting the instruments.",
    "A moment, sir.",
)


@lru_cache(maxsize=1)
def personality() -> str:
    return (Path(__file__).with_name("personality.md")).read_text(encoding="utf-8").strip()


def filler(turn: int) -> str:
    """The line for this tool round. It walks through the list, so the same line is not said twice in a row."""
    return FILLERS[turn % len(FILLERS)]
