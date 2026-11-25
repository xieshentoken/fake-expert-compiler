"""Candidate table boundaries from bounded, visible raster rules."""
from __future__ import annotations

import re

_DARK = bytes(1 if value < 135 else 0 for value in range(256))


def horizontal_rules(decoded, page_width, page_height):
    """Find long uninterrupted ink runs and collapse thick rules."""
    width, height, color_type, pixels = decoded
    if not 1 <= width <= 5000 or not 1 <= height <= 5000:
        return []
    channels = 4 if color_type == 6 else 3
    threshold = max(24, int(width * 0.19))
    runs = re.compile(rb"\x01{" + str(threshold).encode() + rb",}")
    bands = []
    stride = width * channels
    for y in range(height):
        row = pixels[y * stride:(y + 1) * stride:channels].translate(_DARK)
        for hit in runs.finditer(row):
            left, right = hit.span()
            previous = next((b for b in reversed(bands[-12:]) if y - b[3] <= 2
                             and min(right, b[2]) - max(left, b[0]) >= width * .05), None)
            if previous is None:
                bands.append([left, y, right, y])
            else:
                previous[0] = min(previous[0], left)
                previous[2] = max(previous[2], right)
                previous[3] = y
    return [[round(x0 / width * page_width, 4), round((y0 + y1) / 2 / height * page_height, 4),
             round(x1 / width * page_width, 4)]
            for x0, y0, x1, y1 in bands if y1 - y0 <= max(5, height * 0.01)]


def ruled_candidate(caption, rules, width, height, next_caption_y):
    """A near top rule, header rule and closing rule must share a footprint."""
    candidates = []
    for top in rules:
        if not -3 <= top[1] - caption[3] <= min(40, height * .075):
            continue
        if top[0] - 18 > caption[0] or caption[0] > top[2]:
            continue
        aligned = [row for row in rules if 5 <= row[1] - top[1] <= min(235, height * .39)
                   and row[1] < next_caption_y
                   and abs(row[0] - top[0]) <= width * .035
                   and abs(row[2] - top[2]) <= width * .035]
        if len(aligned) < 2:
            continue
        left, head, right = top
        header = aligned[0][1]
        header_depth = header - head
        if not 7 <= header_depth <= 68:
            continue
        if header_depth > 41:
            span = right - left
            grouped = any(
                head + 3 <= y <= header - 3
                and left - 2 <= x0 < x1 <= right + 2
                and .25 * span <= x1 - x0 <= .94 * span
                for x0, y, x1 in rules
            )
            if not grouped:
                continue
        if len(aligned) > 2:
            return None
        bottom = aligned[1][1]
        if bottom - header < 10:
            continue
        candidates.append((
            [max(0, left - 1), max(0, head - 1), min(width, right + 1), min(height, bottom + 1)],
            {"geometry_evidence": "raster-table-rules-v0.1", "candidate_only": True,
             "rule_y_pdf_points": [head, header, bottom]},
        ))
    return candidates[0] if len(candidates) == 1 else None
