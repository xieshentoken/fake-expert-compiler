"""Read-only source/scope validation and frozen inventories for existing workpacks."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from semantic_promotion import validate_semantic_workpack
from native_page_disposition import load_receipt


def inventory(root):
    root=Path(root).expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('reused_workpack_path_invalid')
    files={}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('reused_workpack_symlink_forbidden')
        if path.is_file():
            files[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise ValueError('reused_workpack_empty')
    return files


def validate(source, root):
    issues,summary=validate_semantic_workpack(source,root,require_complete_drafts=False)
    errors=[issue for issue in issues if issue.severity=='error']
    if errors:
        raise ValueError('reused_workpack_invalid:'+','.join(issue.code for issue in errors[:10]))
    manifest=json.loads((root/'workpack.json').read_text())
    if manifest.get('scan_candidate'):
        raise ValueError('reused_workpack_requires_explicit_scan_profile')
    return manifest


def bind_workpacks(source, paths, intake, dispositions=None):
    if not paths:
        return []
    selected=intake['selection']['physical_pages']
    ledger={row['physical_page']:row for row in intake['page_ledger']}
    expected_dispositions=load_receipt(source,dispositions) if dispositions else {}
    result=[]
    for path in paths:
        root=Path(path).expanduser().absolute()
        before=inventory(root)
        manifest=validate(source,root)
        scope=manifest['source']['scope']
        start,end=scope['start_page'],scope['end_page']
        if not selected[0]<=start<=end<=selected[1]:
            raise ValueError('reused_workpack_scope_outside_selection')
        rows=[json.loads(line) for line in (root/manifest.get('page_index','page-index.jsonl')).read_text().splitlines() if line.strip()]
        indexed={row['page']:row for row in rows}
        for n in range(start,end+1):
            if n not in indexed:
                raise ValueError('reused_workpack_page_index_missing')
            # Reuse only pages still eligible under current shared rules. Existing
            # pure native evidence needs no new extraction/tool fingerprint.
            current=ledger[n]
            if current['classification'] not in ('native','confirmed_blank'):
                raise ValueError('reused_workpack_unresolved_page:'+str(n))
            actual=indexed[n].get('page_disposition_proof')
            expected=expected_dispositions.get(n) or current.get('disposition_proof')
            if actual!=expected:
                raise ValueError('reused_workpack_disposition_mismatch:'+str(n))
            if current['classification']=='confirmed_blank' and indexed[n].get('blank_page_proof')!=current['proof']:
                raise ValueError('reused_workpack_blank_proof_stale:'+str(n))
        if inventory(root)!=before:
            raise ValueError('reused_workpack_changed_during_validation')
        result.append({'path':str(root.resolve()),'physical_pages':[start,end],'source_sha256':manifest['source']['sha256'],
                       'workpack_id':manifest['workpack_id'],'files':before})
    result.sort(key=lambda b:b['physical_pages'])
    cursor=selected[0]
    for binding in result:
        start,end=binding['physical_pages']
        if start<cursor:
            raise ValueError('reused_workpack_scope_overlap')
        if start>cursor:
            raise ValueError('reused_workpack_scope_gap:'+str(cursor))
        cursor=end+1
    if cursor!=selected[1]+1:
        raise ValueError('reused_workpack_scope_gap:'+str(cursor))
    return result


def verify_binding(source,binding):
    root=Path(binding['path'])
    if inventory(root)!=binding['files']:
        raise ValueError('reused_workpack_inventory_drift')
    manifest=validate(source,root)
    scope=manifest['source']['scope']
    if manifest['source']['sha256']!=binding['source_sha256'] or manifest['workpack_id']!=binding['workpack_id'] or [scope['start_page'],scope['end_page']]!=binding['physical_pages']:
        raise ValueError('reused_workpack_binding_drift')
    return root
