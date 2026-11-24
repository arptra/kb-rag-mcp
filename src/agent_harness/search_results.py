"""Select a query-relevant excerpt for model context."""

import re

_WORDS = re.compile(r"[\w-]{3,}")


def source_passage(text: str, question: str, limit: int) -> str:
    """Keep a contiguous passage around query terms when a hit exceeds the display budget."""
    if len(text) <= limit:
        return text
    if limit < 4:
        return text[:limit]
    width = limit - 2
    terms = {w.casefold() for w in _WORDS.findall(question)}
    if not terms:
        return text[:limit - 1] + "…"
    end = len(text) - width
    offsets = range(0, end, max(1, width // 2))

    def score(start: int) -> int:
        words = set(_WORDS.findall(text[start:start + width].casefold()))
        return len(terms & words)

    start = max((*offsets, end), key=score)
    if start == 0:
        return text[:limit - 1] + "…"
    if start == end:
        return "…" + text[1 - limit:]
    return "…" + text[start:start + width] + "…"
