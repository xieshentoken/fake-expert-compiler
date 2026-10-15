"""Candidate PDF text geometry replay, independent of extraction callback state.

This never changes the canonical pypdf page text. Text-line matrices and text
matrices are separate, leading stays in text units, and glyphs retain their
content-stream placement. No OCR, inference, or knowledge Review is performed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

PROTOCOL = "native-text-geometry-v0.1"
IDENTITY = (1., 0., 0., 1., 0., 0.)


def multiply(a, b):
    return [a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
            a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
            a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5]]


def point(m, x, y):
    return m[0]*x+m[2]*y+m[4], m[1]*x+m[3]*y+m[5]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _font(resource):
    # pypdf 6.10.0 is pinned by the compiler. Keep its font decoder/metrics
    # behind this single boundary rather than copying its CMap implementation.
    # Failure is explicit; no alternate parser or dependency is selected.
    from pypdf import __version__
    if __version__ != "6.10.0":
        raise ValueError("native_geometry_pypdf_pin_mismatch")
    from pypdf._font import Font
    return Font.from_font_resource(resource)


def _decode(value, font):
    original = getattr(value, "get_original_bytes", None)
    if callable(original):
        raw = original()
    elif isinstance(value, bytes):
        raw = value
    else:
        return [(c, c) for c in str(value)]
    if isinstance(font.encoding, str):
        text = raw.decode(font.encoding, errors="strict")
    else:
        text = "".join(font.encoding.get(c, chr(c)) for c in raw)
    return [(c, font.character_map.get(c, c)) for c in text]


def text_fragments(page, page_height, *, media_left=0., media_bottom=0.):
    """Return placement-bound text fragments and glyphs; all remain candidates."""
    from pypdf.generic import ContentStream

    rows = []
    font_cache = {}
    reader = page.pdf

    def walk(stream, resources, initial_ctm, chain, ancestors):
        if len(chain) > 8:
            raise ValueError("native_geometry_form_depth_exceeded")
        state = {"ctm":list(initial_ctm), "font":None, "size":12., "char_space":0.,
                 "word_space":0., "horizontal":1., "leading":0., "rise":0.}
        stack = []
        tm, line = list(IDENTITY), list(IDENTITY)
        fonts = resources.get("/Font", {})
        xobjects = resources.get("/XObject", {})
        operations = ContentStream(stream, reader).operations if stream is not None else []

        def newline(tx, ty):
            nonlocal tm, line
            line = multiply(line, [1.,0.,0.,1.,tx,ty])
            tm = list(line)

        def show(value, index):
            nonlocal tm
            key = state["font"]
            if key is None or key not in fonts:
                raise ValueError("native_geometry_font_missing")
            resource = fonts[key].get_object()
            cache_key = id(resource)
            if cache_key not in font_cache:
                font_cache[cache_key] = _font(resource)
            font = font_cache[cache_key]
            if not font.interpretable:
                raise ValueError("native_geometry_font_uninterpretable")
            matrix = multiply(state["ctm"], tm)
            size = state["size"]
            horizontal = state["horizontal"]
            glyphs = []
            advance = 0.
            text = ""
            for encoded, decoded in _decode(value, font):
                width = font.text_width(encoded)/1000.*size*horizontal
                lower = state["rise"]+font.font_descriptor.descent/1000.*size
                upper = state["rise"]+font.font_descriptor.ascent/1000.*size
                corners = [point(matrix,x,y) for x in (advance,advance+width) for y in (lower,upper)]
                box = [min(x for x,y in corners)-media_left,
                       page_height+media_bottom-max(y for x,y in corners),
                       max(x for x,y in corners)-media_left,
                       page_height+media_bottom-min(y for x,y in corners)]
                baseline = point(matrix,advance,state["rise"])
                glyphs.append({"text":decoded,"bbox":[round(v,6) for v in box],
                    "baseline":[round(baseline[0]-media_left,6),round(page_height+media_bottom-baseline[1],6)]})
                text += decoded
                advance += width+(state["char_space"]+(state["word_space"] if encoded==" " else 0.))*horizontal
            tm = multiply(tm,[1.,0.,0.,1.,advance,0.])
            if not text.strip() or not glyphs:
                return
            bbox = [min(g["bbox"][0] for g in glyphs),min(g["bbox"][1] for g in glyphs),
                    max(g["bbox"][2] for g in glyphs),max(g["bbox"][3] for g in glyphs)]
            if not all(math.isfinite(v) for v in bbox):
                raise ValueError("native_geometry_nonfinite")
            receipt = {"protocol":PROTOCOL,"stream_chain":list(chain),"operator_index":index,
                "font":str(key),"matrix":[round(v,9) for v in matrix],"glyphs":glyphs,
                "geometry_precision":"font-metrics-candidate"}
            rows.append({"text":text,"bbox":bbox,"font_size":max(0.01,size*math.hypot(matrix[2],matrix[3])),
                "text_origin":"form-xobject" if chain else "page",
                "fragment_index":len(rows),"fragment_sha256":digest({"text":text,"bbox":bbox,"placement":receipt}),
                "geometry_provenance":receipt})

        for index,(args,op) in enumerate(operations):
            if op==b"q":stack.append(copy.deepcopy(state))
            elif op==b"Q":
                if not stack:raise ValueError("native_geometry_graphics_stack_invalid")
                state=stack.pop()
            elif op==b"cm":state["ctm"]=multiply(state["ctm"],list(map(float,args)))
            elif op==b"BT":tm,line=list(IDENTITY),list(IDENTITY)
            elif op==b"Tm":tm=list(map(float,args));line=list(tm)
            elif op==b"Tf":state["font"],state["size"]=args[0],float(args[1])
            elif op==b"TL":state["leading"]=float(args[0])
            elif op==b"Tc":state["char_space"]=float(args[0])
            elif op==b"Tw":state["word_space"]=float(args[0])
            elif op==b"Tz":state["horizontal"]=float(args[0])/100.
            elif op==b"Ts":state["rise"]=float(args[0])
            elif op in {b"Td",b"TD"}:
                tx,ty=map(float,args)
                if op==b"TD":state["leading"]=-ty
                newline(tx,ty)
            elif op==b"T*":newline(0.,-state["leading"])
            elif op==b"Tj":show(args[0],index)
            elif op==b"TJ":
                for item in args[0]:
                    if isinstance(item,(int,float)):
                        tm=multiply(tm,[1.,0.,0.,1.,-float(item)/1000.*state["size"]*state["horizontal"],0.])
                    else:show(item,index)
            elif op in {b"'",b'"'}:
                if op==b'"':state["word_space"],state["char_space"]=map(float,args[:2])
                newline(0.,-state["leading"]);show(args[-1],index)
            elif op==b"Do":
                obj=xobjects[args[0]].get_object()
                if obj.get("/Subtype")!="/Form":continue
                identity=id(obj)
                if identity in ancestors:raise ValueError("native_geometry_form_cycle")
                walk(obj,obj.get("/Resources") or resources,
                     multiply(state["ctm"],list(map(float,obj.get("/Matrix",IDENTITY)))),
                     (*chain,str(args[0])),(*ancestors,identity))

    walk(page.get_contents(),page.get("/Resources") or {},IDENTITY,(),())
    return rows


def painted_rules(page, page_height, *, media_left=0., media_bottom=0.):
    """Replay painted straight rules for bounded native fraction recognition."""
    from pypdf.generic import ContentStream
    rules=[]
    def walk(stream,resources,initial,chain):
        if len(chain)>8:raise ValueError("native_geometry_form_depth_exceeded")
        matrix=list(initial);stack=[];path=[]
        for index,(args,op) in enumerate(ContentStream(stream,page.pdf).operations):
            if op==b'q':stack.append(list(matrix))
            elif op==b'Q':matrix=stack.pop()
            elif op==b'cm':matrix=multiply(matrix,list(map(float,args)))
            elif op==b'm':path=[point(matrix,*map(float,args))]
            elif op==b'l':path.append(point(matrix,*map(float,args)))
            elif op==b're':
                x,y,w,h=map(float,args)
                path=[point(matrix,x,y),point(matrix,x+w,y),point(matrix,x+w,y+h),point(matrix,x,y+h)]
            elif op in {b'S',b's',b'f',b'F',b'f*',b'B',b'B*',b'b',b'b*'}:
                if len(path)>=2:
                    xs,ys=[p[0] for p in path],[p[1] for p in path]
                    if max(xs)-min(xs)>=3 and max(ys)-min(ys)<=1:
                        rules.append({'bbox':[round(min(xs)-media_left,6),round(page_height+media_bottom-max(ys),6),
                            round(max(xs)-media_left,6),round(page_height+media_bottom-min(ys),6)],
                            'stream_chain':list(chain),'operator_index':index,'protocol':PROTOCOL})
                path=[]
            elif op==b'n':path=[]
            elif op in {b'c',b'v',b'y'}:path=[]
            elif op==b'Do':
                obj=resources.get('/XObject',{})[args[0]].get_object()
                if obj.get('/Subtype')=='/Form':
                    if str(args[0]) in chain:raise ValueError('native_geometry_form_cycle')
                    walk(obj,obj.get('/Resources') or resources,
                         multiply(matrix,list(map(float,obj.get('/Matrix',IDENTITY)))),(*chain,str(args[0])))
    walk(page.get_contents(),page.get('/Resources') or {},IDENTITY,())
    return rules


def order_fragments(rows):
    """Order native fragments by baseline bands, then physical left-to-right.

    Ascent/descent differ between fonts on the same baseline. Sorting their
    bbox tops first can put a Latin number ahead of a Chinese caption prefix.
    """
    def baseline(row):
        return row['geometry_provenance']['glyphs'][0]['baseline'][1]
    bands = []
    for row in sorted(rows, key=lambda r:(baseline(r),r['bbox'][0],r['fragment_index'])):
        if bands and abs(baseline(row)-baseline(bands[-1][0])) <= max(1.2,min(row['font_size'],bands[-1][0]['font_size'])*.4):
            bands[-1].append(row)
        else:
            bands.append([row])
    return [row for band in bands for row in sorted(band,key=lambda r:(r['bbox'][0],r['fragment_index']))]
