"""Explicit operator-selected text boundaries for native documents without headings."""
import hashlib
import json

SCHEMA = "tkc.native-structure-selections/v0.1"


def selected_segments(path, source_hash, source_id, page_texts, scope, page_records):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data,dict) or set(data) != {"schema_version","source_sha256","boundaries"} or data["schema_version"] != SCHEMA or data["source_sha256"] != source_hash:
        raise ValueError("structure_selections_binding_invalid")
    boundaries=data["boundaries"]
    if not isinstance(boundaries,list) or not boundaries:
        raise ValueError("structure_selections_empty")
    result=[]; previous=None
    for index,row in enumerate(boundaries):
        if not isinstance(row,dict) or set(row) != {"page","start","end","span_sha256"}:
            raise ValueError("structure_selection_shape_invalid")
        page,start,end=(row[k] for k in ("page","start","end"))
        if any(isinstance(v,bool) or not isinstance(v,int) for v in (page,start,end)) or page not in page_texts or not scope[0] <= page <= scope[1] or not 0 <= start < end <= len(page_texts[page]):
            raise ValueError("structure_selection_span_invalid")
        title=page_texts[page][start:end]
        if len(title)>300 or hashlib.sha256(title.encode()).hexdigest()!=row["span_sha256"] or (previous is not None and (page,start) <= previous):
            raise ValueError("structure_selection_hash_or_order_invalid")
        previous=(page,start)
        end_page=boundaries[index+1]["page"] if index+1<len(boundaries) else scope[1]
        identifier="seg-"+hashlib.sha256(f"{source_id}:{page}:{start}:{end}:{row['span_sha256']}".encode()).hexdigest()[:20]
        result.append({"id":identifier,"title":title,"depth":0,"parent_id":None,"has_children":False,
            "outline_page":page,"physical_pages":[page,end_page],"scope_page_range":[page,end_page],
            "source_page_range":[page,end_page],"structure_order":index,
            "page_envelope_policy":"include-next-heading-page",
            "pdf_page_labels":[page_records[p]["pdf_page_label"] for p in range(page,end_page+1)],
            "printed_page_label_candidates":[page_records[p]["printed_page_label_candidate"] for p in range(page,end_page+1)]})
    return result
