"""Bound open-sided native tables by their painted header and closing rules."""
from __future__ import annotations
from native_text_geometry import digest

PROTOCOL = 'native-horizontal-table-rules-v0.1'


def ruled_table_candidate(caption_box, rules, page_boxes, width, height):
    """Return only a three-rule frame with native header/body text inside it.

    A missing closing rule remains unsupported. No fixed-height extension or
    whole-page fallback supplies the missing boundary.
    """
    eligible = []
    seen = set()
    for rule in rules:
        box = rule['bbox']
        key = tuple(box)
        if key in seen:
            continue
        seen.add(key)
        if 0 <= box[0] < box[2] <= width and 0 <= box[1] <= box[3] <= height and box[2]-box[0] >= width*.3:
            eligible.append(rule)
    eligible.sort(key=lambda row:row['bbox'][1])
    for top in eligible:
        x0, y0, x1, _ = top['bbox']
        if not caption_box[1] <= y0 <= caption_box[3]+height*.04:
            continue
        aligned = [r for r in eligible if r['bbox'][1] > y0+1 and abs(r['bbox'][0]-x0) <= 1 and abs(r['bbox'][2]-x1) <= 1]
        # Extra aligned rules make this narrow three-rule layout ambiguous.
        # Leave denser/multiple tables to the existing routes.
        if len(aligned) != 2:
            continue
        header, bottom = aligned[:2]
        header_y, bottom_y = header['bbox'][1], bottom['bbox'][1]
        if header_y-y0 > height*.08 or bottom_y-header_y <= 4:
            continue
        def inside(row, lo, hi):
            box = row['bbox']
            return row.get('text','').strip() and x0-1 <= box[0] < box[2] <= x1+1 and lo <= box[1] < box[3] <= hi
        headings = [r for r in page_boxes if inside(r,y0,header_y)]
        body = [r for r in page_boxes if inside(r,header_y,bottom_y)]
        if not headings or len({round(r['bbox'][1],1) for r in body}) < 2:
            continue
        bbox = [max(0.,x0-.5),max(0.,y0-.5),min(width,x1+.5),min(height,bottom_y+.5)]
        return bbox, {'geometry_evidence':PROTOCOL,'rules_sha256':digest([top,header,bottom]),
            'native_rows_sha256':digest([{'bbox':r['bbox'],'text':r['text']} for r in headings+body]),
            'closing_rule_y':bottom_y,'body_row_count':len(body),'candidate_only':True}
    return None, {'geometry_evidence':'horizontal-table-rules-unavailable','candidate_only':True}
