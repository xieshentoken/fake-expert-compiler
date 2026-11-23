"""Optional local-source acceptance. No default book/path/content is embedded.

Run explicitly with --source, --start-page, --end-page, --boundary-start/end.
Uses only native pypdf and local Poppler. Never authors book semantics or Review.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import _support
from native_intake import inspect_source
from pdf_structure import compile_pdf_intermediate_representation, write_intermediate_representation
from semantic_promotion import build_semantic_workpack, validate_semantic_workpack
from verify_pdf_ir import verify_pdf_ir


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    for name in ('start-page','end-page','boundary-start','boundary-end'):
        parser.add_argument('--'+name,type=int,required=True)
    args=parser.parse_args()
    before=hashlib.sha256(args.source.read_bytes()).hexdigest()
    report=inspect_source(args.source,start_page=args.start_page,end_page=args.end_page)
    with tempfile.TemporaryDirectory(prefix='tkc-local-native-scope-') as temporary:
        root=Path(temporary)
        ir=compile_pdf_intermediate_representation(args.source,args.boundary_start,args.boundary_end)
        write_intermediate_representation(root/'ir',ir)
        verified=verify_pdf_ir(args.source,root/'ir',pdftoppm=None,docling_allow_network=False)
        manifest=build_semantic_workpack(args.source,root/'ir',root/'workpack')
        issues,_=validate_semantic_workpack(args.source,root/'workpack',require_complete_drafts=False)
        errors=[issue.code for issue in issues if issue.severity=='error']
        retained={row['physical_page'] for row in ir['preflight']['pages'] if row.get('page_disposition_proof') or row['page_role']=='confirmed-blank'}
        body_pages={span['page'] for entry in manifest['units'] for span in json.loads((root/'workpack'/entry['path']).read_text())['focus_spans']}
    after=hashlib.sha256(args.source.read_bytes()).hexdigest()
    passed=before==after and verified['passed'] and not errors and not retained.intersection(body_pages)
    print(json.dumps({'passed':passed,'source_sha256':after,'source_unchanged':before==after,'page_count':report['source']['page_count'],
                      'page_summary':report['page_summary'],'selection':report['selection'],'native_ranges':report['native_ranges'],
                      'boundary_ir_replay':verified['passed'],'boundary_workpack_errors':errors,'retained_pages':sorted(retained),
                      'retained_pages_have_no_knowledge_focus':not retained.intersection(body_pages),'knowledge_review':'not-run','whole_book_complete':False},indent=2))
    return 0 if passed else 1

if __name__=='__main__':
    raise SystemExit(main())
