"""Replayable native fraction/power notation bindings; never formula execution.

Supported shape: one symbolic lhs, a product of symbols over one raised integer
power. Everything else remains unbound. A flattened suffix alone is insufficient.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import unicodedata

from native_text_geometry import PROTOCOL as GEOMETRY_PROTOCOL, painted_rules, text_fragments

PROTOCOL = "native-formula-numeric-v0.1"
LABEL = re.compile(r"\(\d+[.-]\d+[A-Za-z]?\)")


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode()).hexdigest()


def compact(text):
    return re.sub(r"\s+","",unicodedata.normalize('NFKC',text)).replace('∗','*').replace('−','-')


def _expression(text, aliases):
    node=ast.parse(text,mode='eval').body
    def visit(n):
        if isinstance(n,ast.Name):return ('sym',aliases.get(n.id,n.id))
        if isinstance(n,ast.Constant) and type(n.value) is int and 0<=n.value<=9:return ('num',str(n.value))
        if isinstance(n,ast.BinOp) and type(n.op) in {ast.Mult,ast.Div,ast.Pow}:
            return ({ast.Mult:'mul',ast.Div:'div',ast.Pow:'pow'}[type(n.op)],visit(n.left),visit(n.right))
        raise ValueError('formula_notation_shape_unsupported')
    return visit(node)


def _product(symbols):
    if not symbols:raise ValueError('formula_numerator_missing')
    value=('sym',symbols[0])
    for symbol in symbols[1:]:value=('mul',value,('sym',symbol))
    return value


def create_native_binding(source: Path, proposal: dict):
    """Return a deterministic candidate receipt, or None for unsupported evidence."""
    formula=proposal.get('formula') or {}
    notation=formula.get('normalized_notation','')
    if not isinstance(notation,str) or '**' not in notation or notation.count('=')!=1:return None
    aliases=formula.get('notation_symbols',{})
    if (not isinstance(aliases,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in aliases.items())
        or len(set(aliases.values()))!=len(aliases)):return None
    try:
        left,right=notation.split('=')
        target_lhs=_expression(left.strip(),aliases)
        target_rhs=_expression(right.strip(),aliases)
        if target_lhs[0]!='sym':return None
    except (SyntaxError,ValueError,TypeError):return None
    source_notation=formula.get('source_notation','')
    if not isinstance(source_notation,str):return None
    labels=LABEL.findall(source_notation)
    if len(labels)!=1:return None
    tag=labels[0]
    from pypdf import PdfReader
    from pdf_structure import normalize_text
    from pdf_parser_adapters import build_render_receipts
    reader=PdfReader(source)
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    spans=[];evidence=[]
    for row in proposal.get('evidence_spans',[]):
        page,start,end=row['page'],row['start'],row['end']
        if any(type(v) is not int for v in (page,start,end)) or not 1<=page<=len(reader.pages):return None
        text=normalize_text(reader.pages[page-1].extract_text() or '')
        if not 0<=start<end<=len(text):return None
        excerpt=text[start:end]
        if hashlib.sha256(excerpt.encode()).hexdigest()!=row['span_sha256']:return None
        spans.append({k:row[k] for k in ('page','start','end','span_sha256')});evidence.append((page,excerpt))
    eligible=[page for page,text in evidence if compact(source_notation) in compact(text)]
    if len(set(eligible))!=1:return None
    page_number=eligible[0];page=reader.pages[page_number-1]
    media=page.mediabox;w,h=float(media.width),float(media.height)
    fragments=text_fragments(page,h,media_left=float(media.left),media_bottom=float(media.bottom))
    numbered=[r for r in fragments if r['text'].strip()==tag and r['bbox'][0]>=w*.7]
    if len(numbered)!=1:return None
    number=numbered[0];line_y=number['geometry_provenance']['glyphs'][0]['baseline'][1];em=number['font_size']
    glyphs=[{**g,'em':r['font_size']} for r in fragments for g in r['geometry_provenance']['glyphs']
            if g['text'].strip() and g['bbox'][2]<number['bbox'][0]]
    equals=[g for g in glyphs if g['text']=='=' and abs(g['baseline'][1]-line_y)<em*.5]
    if len(equals)!=1:return None
    equal=equals[0]
    lhs=sorted([g for g in glyphs if g['bbox'][2]<=equal['bbox'][0]+.1 and abs(g['baseline'][1]-line_y)<em*.65],key=lambda g:g['bbox'][0])
    lhs_text=compact(''.join(g['text'] for g in lhs))
    if not re.fullmatch(r'[A-Za-zΑ-ω]+\*?',lhs_text):return None
    candidates=[]
    for rule in painted_rules(page,h,media_left=float(media.left),media_bottom=float(media.bottom)):
        x0,y0,x1,y1=rule['bbox'];rule_y=(y0+y1)/2
        if x0<equal['bbox'][2] or x1>=number['bbox'][0] or abs(rule_y-line_y)>em*.7:continue
        nearby=[g for g in glyphs if x0-.6<=g['bbox'][0] and g['bbox'][2]<=x1+.6 and abs(g['baseline'][1]-rule_y)<em*1.15]
        above=sorted([g for g in nearby if g['baseline'][1]<rule_y-.5],key=lambda g:g['bbox'][0])
        below=sorted([g for g in nearby if g['baseline'][1]>rule_y+.5],key=lambda g:g['bbox'][0])
        if not above or len(below)!=2:continue
        base,power=below
        if not re.fullmatch(r'[A-Za-zΑ-ω]',base['text']) or not re.fullmatch(r'[2-9]',power['text']):continue
        raised=base['baseline'][1]-power['baseline'][1]
        if not (em*.12<raised<em*1.1 and power['em']<base['em']*.9 and -.5<=power['bbox'][0]-base['bbox'][2]<em*.5):continue
        symbols=[g['text'] for g in above]
        if any(not re.fullmatch(r'[A-Za-zΑ-ω]',s) for s in symbols):continue
        rhs=('div',_product(symbols),('pow',('sym',base['text']),('num',power['text'])))
        flat=lhs_text+'='+''.join(symbols)+base['text']+power['text']
        if compact(LABEL.sub('',source_notation))!=compact(flat):continue
        if target_lhs!=('sym',lhs_text) or target_rhs!=rhs:continue
        candidates.append((rule,base,power,rhs))
    if len(candidates)!=1:return None
    renderer=shutil.which('pdftoppm')
    if not renderer:return None
    profile,renders=build_render_receipts(source,source_hash,[page_number],renderer,dpi=120)
    rule,base,power,rhs=candidates[0]
    tools={p:hashlib.sha256(Path(__file__).with_name(p).read_bytes()).hexdigest()
           for p in ('formula_numeric_evidence.py','native_text_geometry.py')}
    result={'protocol':PROTOCOL,'candidate_only':True,'source_sha256':source_hash,'page':page_number,
        'evidence_spans':spans,'source_notation_sha256':hashlib.sha256(source_notation.encode()).hexdigest(),
        'normalized_notation_sha256':hashlib.sha256(notation.encode()).hexdigest(),
        'notation_symbols_sha256':digest(aliases),'geometry_protocol':GEOMETRY_PROTOCOL,
        'tools':tools,'render':renders[0],'render_profile':profile,
        'rule':'native-fraction-raised-integer-v0.1','rule_sha256':digest(rule),
        'glyph_sha256':digest({'base':base,'power':power}),
        'structure_sha256':digest((target_lhs,rhs)),
        'supported_numbers':[{'role':'exponent','base':base['text'],'value':power['text']}]}
    result['receipt_sha256']=digest(result)
    return result


def verify_native_binding(source: Path, proposal: dict):
    saved=(proposal.get('formula') or {}).get('native_numeric_evidence')
    if saved is None:return None
    actual=create_native_binding(source,proposal)
    if actual is None or saved!=actual:
        raise ValueError('formula_numeric_binding_mismatch')
    return actual
