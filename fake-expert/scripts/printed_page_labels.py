"""Conservative printed folio recognition separate from physical page identity."""
from __future__ import annotations

import re
import unicodedata

_RADIUS = 5
_ROMAN = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_ROMAN_VALUE = dict(zip("IVXLCDM", (1, 5, 10, 50, 100, 500, 1000)))


def _number(token):
    if re.fullmatch(r"[1-9][0-9]{0,3}", token):
        return "arabic", int(token)
    cap = token.upper()
    if len(cap) > 0 and _ROMAN.fullmatch(cap):
        values = [_ROMAN_VALUE[c] for c in cap]
        return "roman", sum(-n if i + 1 < len(values) and n < values[i + 1] else n
                            for i, n in enumerate(values))
    return None


def _edge_candidates(raw):
    lines = [line.strip() for line in unicodedata.normalize("NFKC", raw).splitlines() if line.strip()]
    result = {}
    for line in dict.fromkeys(lines[:5] + lines[-5:]):
        words = line.split()
        if len(words) == 1:
            tokens = words
        else:
            # Restrict inline numbers to running headers/footers, avoiding
            # prose references, decimal values and single-number axis labels.
            tokens = []
            if len(line) <= 110 and re.search(r"\b[^\W\d_]{3,}\b", line):
                if not re.match(r"^(?:figure|fig\.|table|plate|scheme|chapter|section)\s", line, re.I):
                    tokens.extend([words[0], words[-1]])
        for token in tokens:
            number = _number(token)
            if number:
                isolated_edge = len(words) == 1 and line in (lines[0], lines[-1])
                previous = result.get(number)
                if previous is None or isolated_edge:
                    result[number] = (token, isolated_edge)
    return result


def from_page_texts(raw_texts):
    """Accept a visible candidate only when another page backs its sequence."""
    available = {page: _edge_candidates(text) for page, text in raw_texts.items()}
    result = {}
    for page, candidates in available.items():
        neighbors = [(i, available[i]) for i in range(page - _RADIUS, page + _RADIUS + 1)
                     if i != page and i in available]
        scores = {}
        for (style, number), (label, isolated) in candidates.items():
            support = [abs(i - page) for i, row in neighbors if (style, number + i - page) in row]
            # A pair of standalone edge numbers may straddle textless pages.
            across_empty = isolated and any(
                row.get((style, number + i - page), (None, False))[1]
                and all(not raw_texts.get(j, "").strip() for j in range(min(i, page) + 1, max(i, page)))
                for i, row in neighbors
            )
            if support and (min(support) == 1 or len(support) >= 2 or across_empty):
                scores[label] = sum(_RADIUS + 1 - d for d in support)
        top = max(scores.values(), default=0)
        winners = [label for label, score in scores.items() if score == top]
        result[page] = winners[0] if len(winners) == 1 else None
    return result


def resolve(reader, start, end, raw_texts=None):
    raw_texts = raw_texts or {}
    local = {p: raw_texts[p] if p in raw_texts else reader.pages[p - 1].extract_text() or ""
             for p in range(max(1, start - _RADIUS), min(len(reader.pages), end + _RADIUS) + 1)}
    resolved = from_page_texts(local)
    return {p: resolved[p] for p in range(start, end + 1)}


def display_label(label, physical_page):
    # Existing anchor/manifest consumers require strings. An explicitly named
    # physical fallback avoids representing a PDF index as a printed folio.
    return label if label is not None else f"PDF physical page {physical_page}"
