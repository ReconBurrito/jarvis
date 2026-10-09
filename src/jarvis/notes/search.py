"""Keyword search over the notes (BM25 over sections), in plain Python.

Small on purpose: it has no index to keep in step with the files, so a note is
searchable the moment it is written. A dedicated search engine can replace it
behind the same function once the notes are many enough to need one.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")
STOP = frozenset(
    "a an and are as at be by for from has have how in is it its of on or that the this to was what when where "
    "which who will with you your i me my do does did can could should would about".split()
)


@dataclass(frozen=True)
class Section:
    path: str
    heading: str
    text: str


def words(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP]


def sections(path: str, text: str, max_chars: int = 1200) -> list[Section]:
    """Cut a note at its headings, and long sections again at paragraph breaks."""
    out: list[Section] = []
    heading, lines = "", []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if not body:
            return
        chunk = ""
        for paragraph in re.split(r"\n\s*\n", body):
            if chunk and len(chunk) + len(paragraph) > max_chars:
                out.append(Section(path, heading, chunk.strip()))
                chunk = ""
            chunk += paragraph + "\n\n"
        if chunk.strip():
            out.append(Section(path, heading, chunk.strip()))

    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            flush()
            heading, lines = match.group(1).strip(), []
        else:
            lines.append(line)
    flush()
    return out


def ranked(notes: dict[str, str], query: str) -> tuple[list[Section], list[tuple[float, Section]]]:
    """Every section of the notes, and the sections that share a word with the question, best first (BM25)."""
    terms = words(query)
    corpus = [s for path, text in notes.items() for s in sections(path, text)]
    if not terms or not corpus:
        return corpus, []
    counted = [Counter(words(f"{s.path} {s.heading} {s.text}")) for s in corpus]
    lengths = [sum(c.values()) for c in counted]
    average = sum(lengths) / len(lengths) or 1.0
    asked = set(terms)
    containing = {term: sum(1 for c in counted if term in c) for term in asked}   # once per question, not per section
    scored = []
    for section, counts, length in zip(corpus, counted, lengths):
        score = 0.0
        for term in asked:
            seen = counts.get(term, 0)
            if not seen:
                continue
            idf = math.log(1 + (len(corpus) - containing[term] + 0.5) / (containing[term] + 0.5))
            score += idf * seen * 2.2 / (seen + 1.2 * (0.25 + 0.75 * length / average))
        if score > 0:
            scored.append((score, section))
    scored.sort(key=lambda item: (-item[0], item[1].path))
    return corpus, scored


def result(section: Section, score: float) -> dict:
    return {"path": section.path, "heading": section.heading, "text": section.text[:900], "score": round(score, 2)}


def search(notes: dict[str, str], query: str, limit: int = 5) -> list[dict]:
    """Best-matching sections for a question, each with the note it came from."""
    _, scored = ranked(notes, query)
    return [result(s, score) for score, s in scored[:limit]]
