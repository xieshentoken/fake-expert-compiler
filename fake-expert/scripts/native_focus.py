"""Deterministic partition of canonical source spans into authoring units."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

PROTOCOL = "native-focus-partition-v0.1"
DEFAULT_LIMIT = 20000
_SENTENCE_END = re.compile(r"[.!?。！？；;]\s+")


def plan_digest(units):
    rows = [[segment, [[row[k] for k in ("page", "start", "end", "span_sha256")] for row in spans]]
            for segment, spans in units]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def partition(spans, limit):
    """Prefer physical page boundaries, then a sentence or word boundary.

    Each span is already trimmed by the canonical focus extractor. Splitting
    preserves every byte of that canonical text, including the seam whitespace.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("invalid_native_focus_limit")
    if sum(row["end"] - row["start"] for row in spans) <= limit:
        return [spans]
    batches = []
    current = []
    size = 0
    for source in spans:
        text = source["text"]
        offset = 0
        while len(text) - offset > limit:
            if current:
                batches.append(current)
                current, size = [], 0
            end = offset + limit
            region = text[offset:end]
            seams = [match.end() for match in _SENTENCE_END.finditer(region) if match.end() >= limit // 2]
            whitespace = region.rfind(" ", limit // 2)
            cut = seams[-1] if seams else whitespace + 1 if whitespace >= 0 else limit
            batches.append([_slice(source, offset, offset + cut)])
            offset += cut
        if offset == len(text):
            continue
        remaining = len(text) - offset
        if size + remaining > limit:
            batches.append(current)
            current, size = [], 0
        current.append(source if offset == 0 else _slice(source, offset, len(text)))
        size += remaining
    if current:
        batches.append(current)
    return batches


def _slice(span, start, end):
    text = span["text"][start:end]
    row = deepcopy(span)
    row.update(start=span["start"] + start, end=span["start"] + end,
               text=text, span_sha256=hashlib.sha256(text.encode()).hexdigest())
    return row
