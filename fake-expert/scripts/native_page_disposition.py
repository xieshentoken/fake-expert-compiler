"""Source-bound page roles and operator-provided dispositions, never knowledge Review.

prepare emits empty decisions. import validates externally completed decisions
against the live source and explicitly supplied operator identity, then copies
that submission into a fresh receipt. This is transport validation, not identity
authentication, independent Review, Gold or a signature.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from compiler_version import (NATIVE_DISPOSITION_PROTOCOL as PROTOCOL,
                              NATIVE_STRUCTURE_PAGE_PROTOCOL as STRUCTURAL_PROTOCOL,
                              DIRECT_REFERENCE_COMPILER_VERSION)
ROLES = ('blank', 'structural', 'context', 'non-knowledge')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def commitment(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def page_binding(source, page_number, renderer='pdftoppm', *, reader=None):
    from pypdf import PdfReader, __version__
    source = Path(source).resolve()
    source_hash = digest(source)
    reader = reader or PdfReader(source)
    if type(page_number) is not int or not 1 <= page_number <= len(reader.pages):
        raise ValueError('page_disposition_scope_invalid')
    page = reader.pages[page_number-1]
    box = [float(v) for v in page.mediabox]
    rotation, unit = int(page.get('/Rotate',0)), float(page.get('/UserUnit',1))
    if rotation not in (0,90,180,270) or unit != 1 or not all(math.isfinite(v) for v in box):
        raise ValueError('page_disposition_geometry_unsupported')
    width,height = box[2]-box[0],box[3]-box[1]
    if rotation in (90,270):
        width,height = height,width
    if min(width,height) <= 0 or math.ceil(width*150/72)*math.ceil(height*150/72)>25_000_000:
        raise ValueError('page_disposition_geometry_unsupported')
    executable = Path(shutil.which(str(renderer)) or str(renderer)).resolve()
    renderer_hash = digest(executable)
    version = subprocess.run([str(executable),'-v'],capture_output=True,text=True,timeout=10)
    if version.returncode:
        raise ValueError('page_disposition_renderer_failed')
    with tempfile.TemporaryDirectory(prefix='tkc-page-disposition-') as temp:
        prefix = Path(temp)/'page'
        result = subprocess.run([str(executable),'-f',str(page_number),'-l',str(page_number),'-singlefile','-r','150',str(source),str(prefix)], capture_output=True,timeout=30)
        target = prefix.with_suffix('.ppm')
        if result.returncode or result.stderr.strip() or not target.is_file():
            raise ValueError('page_disposition_render_failed')
        data = target.read_bytes()
    header = re.match(rb'P6\s+(\d+)\s+(\d+)\s+255(?:\r\n|\n| )',data)
    dimensions = [math.ceil(width*150/72),math.ceil(height*150/72)]
    if header is None or [int(header[1]),int(header[2])] != dimensions or len(data)-header.end()!=dimensions[0]*dimensions[1]*3:
        raise ValueError('page_disposition_render_geometry_mismatch')
    content = page.get_contents()
    if digest(executable)!=renderer_hash or digest(source)!=source_hash:
        raise ValueError('page_disposition_renderer_drift')
    return {'source_sha256':digest(source),'physical_page':page_number,'pypdf_version':__version__,
            'content_sha256':hashlib.sha256(content.get_data() if content is not None else b'').hexdigest(),
            'text_sha256':hashlib.sha256((page.extract_text() or '').encode()).hexdigest(),
            'render':{'renderer':'pdftoppm','renderer_sha256':renderer_hash,'version':(version.stderr or version.stdout).splitlines()[0],
                      'sha256':hashlib.sha256(data).hexdigest(),'box':'MediaBox','media_box_points':box,
                      'rotation_clockwise':rotation,'dpi':150,'dimensions':dimensions,
                      'nonwhite':any(v!=255 for v in data[header.end():])},
            'rule_version':PROTOCOL}


def structural_proof(source, page_number, renderer='pdftoppm', *, reader=None):
    """Only a sparse Part/Chapter title exactly matching an outline destination.

    This retains the original text as structural context, never body evidence.
    No image, invisible/white text or opacity can qualify by a title alone.
    """
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    reader = reader or PdfReader(source)
    page = reader.pages[page_number-1]
    raw = page.extract_text() or ''
    if not re.match(r'^\s*(?:part|chapter)\s*(?:\d+|[ivxlcdm]+)\b',raw,re.I):
        return None
    if sum(c.isalnum() for c in raw) < 5 or len(raw)>300:
        return None
    def compact(s):
        return ''.join(c.casefold() for c in s if c.isalnum())
    matches=[]
    def visit(items):
        for item in items:
            if isinstance(item,list):
                visit(item)
            elif reader.get_destination_page_number(item)==page_number-1 and compact(item.title)==compact(raw):
                matches.append(item.title)
    try:
        visit(reader.outline)
        if not matches or any(page.get(k) for k in ('/Annots','/AA','/Group')):
            return None
        resources=page.get('/Resources',{}).get_object()
        fill_visible, color_space, states = True, 'device', []
        for operands,op in ContentStream(page.get_contents(),reader).operations:
            if op==b'q':
                states.append((fill_visible,color_space))
            elif op==b'Q':
                fill_visible,color_space=states.pop()
            elif op in (b'Tj',b'TJ',b"'",b'"') and not fill_visible:
                return None
            if op in (b'Do',b'INLINE IMAGE') or (op==b'Tr' and operands!=[0]):
                return None
            if op in (b'g',b'rg',b'k'):
                fill_visible = list(map(float,operands)) == {b'g':[0.],b'rg':[0.,0.,0.],b'k':[0.,0.,0.,1.]}[op]
            if op==b'cs':
                cs=resources.get('/ColorSpace',{}).get_object()[operands[0]].get_object()
                if len(cs)!=4 or str(cs[0])!='/Separation' or str(cs[1])!='/Black':
                    return None
                alternate, function=cs[2].get_object(),cs[3].get_object()
                if str(alternate[0])!='/ICCBased' or alternate[1].get_object().get('/N')!=3:
                    return None
                if function.get('/FunctionType')!=0 or function.get('/BitsPerSample')!=8 or list(function.get('/Size',[]))!=[255] or list(function.get('/Encode',[]))!=[0,254] or list(function.get('/Domain',[]))!=[0,1] or list(function.get('/Decode',[]))!=[0,1,0,1,0,1]:
                    return None
                samples=function.get_data()
                if len(samples)!=765 or max(samples[-3:])>64:
                    return None
                color_space='black-separation'
                fill_visible=False
            if op in (b'sc',b'scn'):
                fill_visible=color_space=='black-separation' and list(map(float,operands))==[1.]
            if op==b'gs':
                state=resources.get('/ExtGState',{}).get_object()[operands[0]].get_object()
                if set(state)-{'/Type','/SA','/SM'}:
                    return None
        # All non-text drawing must itself have the restricted outside-paper
        # proof. Unknown operators, page-interior strokes/fills and curves fail.
        text_ops={b'BT',b'ET',b'Tf',b'TL',b'Tc',b'Tw',b'Tz',b'Tr',b'Ts',b'Td',b'TD',b'Tm',b'T*',b'Tj',b'TJ',b"'",b'"'}
        fill_ops={b'g',b'rg',b'k',b'cs',b'sc',b'scn'}
        operations=[(a,o) for a,o in ContentStream(page.get_contents(),reader).operations if o not in text_ops|fill_ops]
        from native_print_marks import outside_print_marks
        if any(o in {b'S',b's'} for a,o in operations):
            if not outside_print_marks(page,operations):
                return None
        elif any(o not in {b'q',b'Q',b'cm',b'gs',b'G',b'RG',b'K',b'w',b'J',b'j',b'M',b'd'} for a,o in operations):
            return None
        from native_text_geometry import text_fragments
        box=[float(v) for v in page.mediabox]
        fragments=text_fragments(page,box[3]-box[1],media_left=box[0],media_bottom=box[1])
        if not fragments or any(not (0<=f['bbox'][0]<f['bbox'][2]<=box[2]-box[0] and 0<=f['bbox'][1]<f['bbox'][3]<=box[3]-box[1]) for f in fragments):
            return None
        from native_print_marks import clips_contain_text
        text_box=[min(f['bbox'][0] for f in fragments)+box[0],
                  box[3]-max(f['bbox'][3] for f in fragments),
                  max(f['bbox'][2] for f in fragments)+box[0],
                  box[3]-min(f['bbox'][1] for f in fragments)]
        if not clips_contain_text(page,ContentStream(page.get_contents(),reader).operations,text_box):
            return None
        binding=page_binding(source,page_number,renderer,reader=reader)
        if not binding['render']['nonwhite']:
            return None
        return {'protocol':STRUCTURAL_PROTOCOL,'binding':binding,'outline_title':matches[0],
                'role':'structural','knowledge_review':False,'gold':False}
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,OverflowError,OSError,subprocess.SubprocessError):
        return None


def prepare(source, pages, renderer='pdftoppm'):
    from pypdf import PdfReader
    reader=PdfReader(source)
    if not pages or len(set(pages))!=len(pages):
        raise ValueError('page_disposition_pages_invalid')
    tasks=[{'binding':page_binding(source,n,renderer,reader=reader),'role':None,'reason':None} for n in sorted(pages)]
    return {'schema_version':'tkc.page-disposition-submission/v0.1','protocol':PROTOCOL,
            'source_sha256':digest(source),'operator_id':None,'operator_attestation':None,
            'items':tasks,'knowledge_review':False,'gold':False}


def validate_submission(source, submission, operator_id, renderer='pdftoppm'):
    from pypdf import PdfReader
    keys={'schema_version','protocol','source_sha256','operator_id','operator_attestation','items','knowledge_review','gold'}
    if not isinstance(submission,dict) or set(submission)!=keys or submission['schema_version']!='tkc.page-disposition-submission/v0.1' or submission['protocol']!=PROTOCOL:
        raise ValueError('page_disposition_schema_invalid')
    if not isinstance(operator_id,str) or not operator_id.strip() or submission['operator_id']!=operator_id or not isinstance(submission['operator_attestation'],str) or not submission['operator_attestation'].strip():
        raise ValueError('page_disposition_operator_required')
    if submission['source_sha256']!=digest(source) or submission['knowledge_review'] is not False or submission['gold'] is not False:
        raise ValueError('page_disposition_source_or_authority_invalid')
    reader=PdfReader(source)
    if not isinstance(submission['items'],list) or not submission['items']:
        raise ValueError('page_disposition_items_invalid')
    seen=set()
    for item in submission['items']:
        if not isinstance(item,dict) or set(item)!={'binding','role','reason'} or item['role'] not in ROLES or not isinstance(item['reason'],str) or not item['reason'].strip():
            raise ValueError('page_disposition_decision_required')
        n=item['binding'].get('physical_page')
        if n in seen:
            raise ValueError('page_disposition_duplicate_page')
        seen.add(n)
        if item['binding']!=page_binding(source,n,renderer,reader=reader):
            raise ValueError('page_disposition_binding_mismatch')
        if item['role']=='blank' and ((reader.pages[n-1].extract_text() or '').strip() or item['binding']['render']['nonwhite']):
            raise ValueError('page_disposition_blank_has_text')
    return submission


def import_submission(source, submission, operator_id, renderer='pdftoppm'):
    value=validate_submission(source,submission,operator_id,renderer)
    return {'schema_version':'tkc.page-disposition-receipt/v0.1','protocol':PROTOCOL,
            'submission_sha256':commitment(value),'submission':value,'authority':'operator-provided-transport-only'}


def load_receipt(source, receipt, renderer='pdftoppm'):
    value=json.loads(Path(receipt).read_text()) if isinstance(receipt,(Path,str)) else receipt
    if not isinstance(value,dict) or set(value)!={'schema_version','protocol','submission_sha256','submission','authority'} or value.get('schema_version')!='tkc.page-disposition-receipt/v0.1' or value.get('protocol')!=PROTOCOL or value.get('authority')!='operator-provided-transport-only' or value['submission_sha256']!=commitment(value['submission']):
        raise ValueError('page_disposition_receipt_invalid')
    validate_submission(source,value['submission'],value['submission'].get('operator_id'),renderer)
    return {item['binding']['physical_page']:{'protocol':PROTOCOL,'receipt':value,'physical_page':item['binding']['physical_page'],'role':item['role']} for item in value['submission']['items']}


def replay(source, proof, renderer='pdftoppm'):
    n=proof.get('physical_page') or proof.get('binding',{}).get('physical_page')
    if proof.get('protocol')==STRUCTURAL_PROTOCOL:
        actual=structural_proof(source,n,renderer)
    elif proof.get('protocol')==PROTOCOL:
        actual=load_receipt(source,proof.get('receipt'),renderer).get(n)
    else:
        raise ValueError('page_disposition_protocol_unsupported')
    if actual!=proof:
        raise ValueError('page_disposition_proof_mismatch')
    return proof['role']


def structural_locator_overlay(segments, page_texts, proofs, candidates, resolutions):
    """A replayed outline-only title can locate its own structural parent.

    This projection is local to workpack construction; it never edits the IR or
    resolves an arbitrary missing heading. Body headings keep their normal gate.
    """
    candidates=list(candidates)
    resolutions=list(resolutions)
    compact=lambda value: ''.join(c.casefold() for c in str(value) if c.isalnum())
    for segment in segments:
        n=segment.get('start_page') or (segment.get('physical_pages') or [None])[0]
        proof=proofs.get(n,{})
        if proof.get('protocol')!=STRUCTURAL_PROTOCOL or compact(proof.get('outline_title'))!=compact(segment.get('title')):
            continue
        if any(r.get('segment_id')==segment['id'] and r.get('status')=='resolved' for r in resolutions):
            continue
        text=page_texts[n]
        cid='hcd-'+commitment({'proof':proof,'segment':segment['id']})[:20]
        candidate={'candidate_id':cid,'segment_id':segment['id'],'physical_page':n,
                   'char_span':[0,len(text)],'page_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                   'span_sha256':hashlib.sha256(text.encode()).hexdigest(),'raw_title':text,
                   'candidate_method':'source-bound-outline-structure','disposition_sha256':commitment(proof)}
        candidate['candidate_sha256']=commitment(candidate)
        candidates.append(candidate)
        resolutions=[r for r in resolutions if r.get('segment_id')!=segment['id']]
        resolutions.append({'segment_id':segment['id'],'status':'resolved','selected_candidate_id':cid,
                            'selected_candidate_sha256':candidate['candidate_sha256'],
                            'resolution_id':'hr-'+commitment(candidate)[:20],'structure_order':segment.get('structure_order',0)})
    return candidates,resolutions


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','validate','import'))
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--pages',type=int,nargs='+')
    parser.add_argument('--submission',type=Path)
    parser.add_argument('--operator-id')
    parser.add_argument('--pdftoppm',default='pdftoppm')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        if args.command=='prepare':
            result=prepare(args.source,args.pages,args.pdftoppm)
        else:
            value=json.loads(args.submission.read_text())
            result=(import_submission if args.command=='import' else validate_submission)(args.source,value,args.operator_id,args.pdftoppm)
        data=json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        if args.output:
            with args.output.open('x') as stream:
                stream.write(data)
            args.output.chmod(0o600)
        else:
            print(data,end='')
    except (OSError,ValueError,TypeError,AttributeError,subprocess.SubprocessError) as error:
        parser.exit(1,str(error)+'\n')



def verify_package_dispositions(source, package, receipts=(), renderer='pdftoppm'):
    """Replay public hash-only page commitments using the original and local receipts."""
    from pypdf import PdfReader
    from native_blank_pages import confirm_blank_page
    package=Path(package)
    control_path=package/'references/runtime/direct-reference.json'
    if not control_path.is_file():
        return []
    control=json.loads(control_path.read_text())
    source_hash=digest(source)
    reader=PdfReader(source)
    supplied={}
    try:
        for receipt in receipts:
            for n,proof in load_receipt(source,receipt,renderer).items():
                key=(n,commitment(proof))
                if key in supplied:
                    raise ValueError('page_disposition_duplicate_receipt')
                supplied[key]=proof
    except (ValueError,OSError) as error:
        return [{'kind':'page-disposition','passed':False,'error':str(error)}]
    results=[]
    seen={}
    for binding in control.get('workpack_bindings',[]):
        if binding.get('source_sha256')!=source_hash:
            continue
        for row in binding.get('page_dispositions',[]):
            n=row.get('physical_page')
            result={'kind':'page-disposition','physical_page':n,'role':row.get('role'),'passed':False,'full_replay':False}
            try:
                if type(n) is not int or not 1<=n<=len(reader.pages):
                    raise ValueError('page_disposition_scope_invalid')
                if n in seen and seen[n]!=row:
                    raise ValueError('page_disposition_conflicting_commitment')
                seen[n]=row
                protocol=row.get('proof_protocol')
                if protocol==PROTOCOL:
                    proof=supplied.get((n,row.get('proof_sha256')))
                    if proof is None:
                        raise ValueError('page_disposition_receipt_required')
                elif protocol==STRUCTURAL_PROTOCOL:
                    proof=structural_proof(source,n,renderer,reader=reader)
                else:
                    proof=confirm_blank_page(reader.pages[n-1],reader.pages[n-1].extract_text() or '',Path(source),source_hash,n,renderer)
                if proof is None or proof.get('protocol')!=protocol or commitment(proof)!=row.get('proof_sha256') or proof.get('role','blank')!=row.get('role'):
                    raise ValueError('page_disposition_proof_mismatch')
                result.update(passed=True,full_replay=True)
            except (ValueError,OSError,TypeError,KeyError) as error:
                result['error']=str(error)
            results.append(result)
    manifest=json.loads((package/'manifest.json').read_text())
    if manifest.get('build',{}).get('compiler_version')==DIRECT_REFERENCE_COMPILER_VERSION:
        for binding in control.get('workpack_bindings',[]):
            if binding.get('source_sha256')!=source_hash:
                continue
            scope=binding.get('scope',{})
            for n in range(scope.get('start_page',1),scope.get('end_page',0)+1):
                if n not in seen and sum(c.isalnum() for c in (reader.pages[n-1].extract_text() or ''))<binding.get('native_minimum_characters',40):
                    results.append({'kind':'page-disposition','physical_page':n,'passed':False,'full_replay':False,
                                    'error':'page_disposition_proof_required'})
    return results


if __name__=='__main__':
    main()
