"""Model text made fit to be read aloud: no Markdown, symbols and grouped numbers spelled out."""

from __future__ import annotations

import re

_SYMBOLS = (
    (re.compile(r"(?<=\d)\s*°\s*C\b"), " degrees Celsius"),
    (re.compile(r"(?<=\d)\s*°\s*F\b"), " degrees Fahrenheit"),
    (re.compile(r"(?<=\d)\s*%"), " percent"),
    (re.compile(r"(?<=\d)\s*GiB\b"), " gigabytes"),
    (re.compile(r"(?<=\d)\s*MiB\b"), " megabytes"),
    (re.compile(r"(?<=\d)\s*ms\b"), " milliseconds"),
)


_GROUPED = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d,]|\.\d)")
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


def number_words(n: int) -> str:
    """A whole number in British words: 1919 -> "one thousand nine hundred and nineteen"."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10 - 2] + (f" {_ONES[n % 10]}" if n % 10 else "")
    if n < 1000:
        return f"{_ONES[n // 100]} hundred" + (f" and {number_words(n % 100)}" if n % 100 else "")
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= size:
            rest = n % size
            joiner = " and " if 0 < rest < 100 else " "
            return f"{number_words(n // size)} {name}" + (f"{joiner}{number_words(rest)}" if rest else "")
    return str(n)


_DECIMAL = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?!\.?\d)")


def speakable(text: str) -> str:
    """Remove Markdown and spell out a few symbols, so the voice never reads formatting aloud."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links and images keep their label
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)  # headings
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", line)  # list markers
        line = re.sub(r"^\s*>\s?", "", line)  # quotes
        if re.fullmatch(r"[\s|:\-=_*]*", line):  # rules and table separators
            continue
        if "|" in line:
            line = ", ".join(cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip())
        line = line.strip()
        if line and line[-1] not in ".!?:;,":
            line += "."
        lines.append(line)
    text = " ".join(lines)
    text = re.sub(r"(\*\*|__|~~|`)", "", text)
    text = re.sub(r"(?<![\w*])[*_](?=\S)|(?<=\S)[*_](?![\w*])", "", text)  # single emphasis marks
    for pattern, spoken in _SYMBOLS:
        text = pattern.sub(spoken, text)
    # "1,919" is one number, and "29.9" is "29 point 9"; version numbers and addresses are left alone.
    text = _GROUPED.sub(lambda m: number_words(int(m.group(0).replace(",", ""))), text)
    text = _DECIMAL.sub(lambda m: f"{m.group(1)} point {' '.join(m.group(2))}", text)
    return re.sub(r"\s+", " ", text).strip()
