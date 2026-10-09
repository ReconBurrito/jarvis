"""Search the notes by meaning as well as by words.

Keyword search (search.py) misses a note that answers the question in other words: "when do the guests get
saved" finds nothing in a note that says "backups run nightly". This adds the meaning: every section of the
notes and the question are turned into vectors by a small embedding model in the same Ollama that runs the
chat model (qwen3-embedding:0.6b by default, on the same GPU), and the sections nearest the question in
meaning are merged with the keyword results by reciprocal rank fusion.

Everything stays on this machine. The vectors are kept in Jarvis's state folder, keyed by the model and a digest of
the section's text, so only new or changed sections are embedded again. The vectors are derived from the
notes, which hold no secrets. When the model is missing or Ollama does not answer, search falls back to the
keyword results alone and says so.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
from array import array
from pathlib import Path
from typing import Any

import httpx

from .search import Section, ranked, result

# Qwen3-Embedding is trained to read an instruction with the question (not with the documents).
QUERY_INSTRUCTION = "Instruct: Given a question about a home lab, retrieve the notes that answer it\nQuery: "
BATCH = 32
RRF_K = 60  # the usual constant for reciprocal rank fusion
NEAREST = 20  # how many of the nearest sections take part in the fusion
MIN_SIMILARITY = 0.35  # below this, a section is not near the question in meaning


class EmbedError(RuntimeError):
    pass


def section_text(section: Section) -> str:
    return f"{section.path}\n{section.heading}\n{section.text}"[:2000]


def cosine(a: array, b: array) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class Embeddings:
    """Vectors for the notes' sections, from Ollama, kept on disk between restarts."""

    def __init__(self, base_url: str, model: str, cache_file: Path, client: httpx.AsyncClient | None = None,
                 timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.cache_file = cache_file
        self.timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10, read=timeout))
        self._lock = asyncio.Lock()
        self._cache: dict[str, array] | None = None

    def key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\0{text}".encode()).hexdigest()

    def _load(self) -> dict[str, array]:
        if self._cache is None:
            self._cache = {}
            try:
                stored = json.loads(self.cache_file.read_text())
                if stored.get("model") == self.model:
                    for key, packed in stored.get("vectors", {}).items():
                        vector = array("f")
                        vector.frombytes(base64.b64decode(packed))
                        self._cache[key] = vector
            except (OSError, ValueError, TypeError):
                pass  # no cache yet, or an unreadable one: it is rebuilt from the notes
        return self._cache

    def _save(self, keep: set[str]) -> None:
        cache = self._load()
        vectors = {key: base64.b64encode(cache[key].tobytes()).decode() for key in sorted(keep) if key in cache}
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cache_file.with_name(self.cache_file.name + ".tmp")
        temp.write_text(json.dumps({"model": self.model, "vectors": vectors}))
        os.replace(temp, self.cache_file)
        # Sections that left the notes leave the cache too.
        self._cache = {key: cache[key] for key in keep if key in cache}

    async def _embed(self, texts: list[str]) -> list[array]:
        try:
            resp = await self._client.post(f"{self.base_url}/api/embed", json={"model": self.model, "input": texts},
                                           timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise EmbedError(f"Ollama did not answer ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("error", ""))[:200]
            except ValueError:
                pass
            raise EmbedError(f"Ollama answered {resp.status_code} {detail}".strip())
        try:
            vectors = resp.json()["embeddings"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbedError("Ollama's answer had no embeddings") from exc
        if len(vectors) != len(texts):
            raise EmbedError("Ollama returned a different number of embeddings than it was given")
        return [array("f", vector) for vector in vectors]

    async def vectors(self, sections: list[Section]) -> list[array]:
        """A vector for every section, embedding only those not seen before."""
        async with self._lock:
            cache = self._load()
            keys = [self.key(section_text(s)) for s in sections]
            missing = list(dict.fromkeys(k for k in keys if k not in cache))
            if missing:
                texts = {self.key(section_text(s)): section_text(s) for s in sections}
                for start in range(0, len(missing), BATCH):
                    batch = missing[start:start + BATCH]
                    for key, vector in zip(batch, await self._embed([texts[k] for k in batch])):
                        cache[key] = vector
            if missing or set(cache) - set(keys):
                self._save(set(keys))
            return [self._cache[k] for k in keys]  # type: ignore[index]

    async def query(self, question: str) -> array:
        return (await self._embed([QUERY_INSTRUCTION + question]))[0]


async def hybrid_search(notes: dict[str, str], query: str, embeddings: Embeddings | None,
                        limit: int = 5) -> tuple[list[dict[str, Any]], str]:
    """Sections that match the question by words or by meaning, best first, and how the search was done."""
    corpus, by_words = await asyncio.to_thread(ranked, notes, query)   # the service goes on answering meanwhile
    if embeddings is None or not corpus or not query.strip():
        return [result(s, score) for score, s in by_words[:limit]], "words"
    try:
        vectors = await embeddings.vectors(corpus)
        asked = await embeddings.query(query)
    except EmbedError as exc:
        return [result(s, score) for score, s in by_words[:limit]], f"words only: meaning search unavailable ({exc})"
    near = sorted(((cosine(asked, v), s) for v, s in zip(vectors, corpus)), key=lambda item: (-item[0], item[1].path))
    near = [(similarity, s) for similarity, s in near[:NEAREST] if similarity >= MIN_SIMILARITY]

    fused: dict[Section, float] = {}
    how: dict[Section, set[str]] = {}
    for rank, (_, section) in enumerate(by_words, start=1):
        fused[section] = fused.get(section, 0.0) + 1 / (RRF_K + rank)
        how.setdefault(section, set()).add("words")
    for rank, (_, section) in enumerate(near, start=1):
        fused[section] = fused.get(section, 0.0) + 1 / (RRF_K + rank)
        how.setdefault(section, set()).add("meaning")
    best = sorted(fused.items(), key=lambda item: (-item[1], item[0].path))[:limit]
    out = []
    for section, score in best:
        found = result(section, score * 100)
        found["matched"] = " and ".join(sorted(how[section]))
        out.append(found)
    return out, "words and meaning"
