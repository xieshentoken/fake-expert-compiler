#!/usr/bin/env python3
"""Build and finalize a private, answer-free semantic assurance review workbench."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from compiler_version import (
    SEMANTIC_REVIEW_WORKBENCH_COMPILER_VERSION,
    SEMANTIC_REVIEW_WORKBENCH_PROTOCOL,
    SEMANTIC_REVIEW_WORKBENCH_REVISION_SCHEMA,
    SEMANTIC_REVIEW_WORKBENCH_SCHEMA,
    SEMANTIC_REVIEW_WORKBENCH_SUBMISSION_SCHEMA,
)
from semantic_assurance import (
    ATTESTATION_LEVEL,
    ATTESTATION_SCHEMA,
    expected_attestation_id,
    review_plan_sha256,
    sha256_json,
    validate_attestations,
)
from semantic_promotion import (
    ASSURANCE_REVIEW_PROTOCOL,
    _proposal_items,
    load_json,
    load_jsonl,
    validate_reviewed_workpack,
    validate_semantic_workpack,
)


WORKBENCH_SCHEMA = SEMANTIC_REVIEW_WORKBENCH_SCHEMA
SUBMISSION_SCHEMA = SEMANTIC_REVIEW_WORKBENCH_SUBMISSION_SCHEMA
REVISION_SCHEMA = SEMANTIC_REVIEW_WORKBENCH_REVISION_SCHEMA
COMPILER_VERSION = SEMANTIC_REVIEW_WORKBENCH_COMPILER_VERSION
ISSUE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ALLOWED_PRE_REVIEW_ERRORS = {
    "review_receipt_missing",
    "reviewer_coverage_insufficient",
}
REVIEW_CHECKS = (
    "evidence_span_checked",
    "support_completeness_checked",
    "scope_and_applicability_checked",
    "conflict_checked",
    "atomic_assertions_checked",
    "differences_recorded",
)
DECLARATIONS = (
    "source_evidence_inspected",
    "no_prediction_or_hidden_answer_used",
    "independent_from_proposer",
    "differences_recorded",
)


class SemanticReviewWorkbenchError(RuntimeError):
    """Stable private workbench failure."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_canonical_json(row) + b"\n" for row in rows))
    os.chmod(path, 0o600)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def _empty_output(path: Path) -> Path:
    output = path.expanduser().resolve()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise SemanticReviewWorkbenchError("output_must_be_empty")
    else:
        output.mkdir(parents=True)
    os.chmod(output, 0o700)
    return output


def _safe_workpack_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise SemanticReviewWorkbenchError("workpack_path_invalid")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SemanticReviewWorkbenchError("workpack_path_unsafe")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SemanticReviewWorkbenchError("workpack_path_unsafe") from error
    if candidate.is_symlink() or not resolved.is_file():
        raise SemanticReviewWorkbenchError("workpack_file_missing_or_symlink")
    return resolved


def _load_frozen_inputs(
    source_path: Path,
    workpack_root: Path,
) -> dict[str, Any]:
    source = source_path.expanduser().resolve()
    root = workpack_root.expanduser().resolve()
    if not source.is_file() or not root.is_dir():
        raise SemanticReviewWorkbenchError("source_or_workpack_missing")
    source_sha256 = _sha256_file(source)
    proposal_issues, _ = validate_semantic_workpack(
        source,
        root,
        require_complete_drafts=True,
    )
    proposal_errors = [issue for issue in proposal_issues if issue.severity == "error"]
    if proposal_errors:
        raise SemanticReviewWorkbenchError(
            f"proposal_invalid:{proposal_errors[0].code}"
        )
    reviewed_issues, _ = validate_reviewed_workpack(source, root)
    unexpected = [
        issue
        for issue in reviewed_issues
        if issue.severity == "error" and issue.code not in ALLOWED_PRE_REVIEW_ERRORS
    ]
    if unexpected:
        raise SemanticReviewWorkbenchError(
            f"review_plan_invalid:{unexpected[0].code}"
        )

    manifest = load_json(root / "workpack.json")
    plan = load_json(root / "review-plan.json")
    queue = load_jsonl(root / "review-queue.jsonl")
    assertions = load_jsonl(root / "semantic-assertions.jsonl")
    support = load_jsonl(root / "support-matrix.jsonl")
    if (
        not isinstance(manifest, dict)
        or manifest.get("source", {}).get("sha256") != source_sha256
        or not isinstance(plan, dict)
        or plan.get("review_protocol") != ASSURANCE_REVIEW_PROTOCOL
        or plan.get("source_sha256") != source_sha256
        or plan.get("workpack_id") != manifest.get("workpack_id")
        or plan.get("semantic_item_count") != len(queue)
    ):
        raise SemanticReviewWorkbenchError("frozen_bindings_invalid")

    units = {
        entry["unit_id"]: load_json(_safe_workpack_path(root, entry["path"]))
        for entry in manifest.get("units", [])
        if isinstance(entry, dict)
        and isinstance(entry.get("unit_id"), str)
        and isinstance(entry.get("path"), str)
    }
    drafts = {
        path.stem: load_json(path)
        for path in sorted((root / "drafts").glob("*.json"))
        if path.is_file() and not path.is_symlink()
    }
    proposal_items = _proposal_items(drafts)
    assertion_by_id = {
        row["assertion_id"]: row
        for row in assertions
        if isinstance(row, dict) and isinstance(row.get("assertion_id"), str)
    }
    support_by_id = {
        row["assertion_id"]: row
        for row in support
        if isinstance(row, dict) and isinstance(row.get("assertion_id"), str)
    }
    if set(assertion_by_id) != set(support_by_id):
        raise SemanticReviewWorkbenchError("assurance_material_incomplete")
    if any(row.get("item_ref") not in proposal_items for row in queue):
        raise SemanticReviewWorkbenchError("proposal_item_missing")

    page_assets: dict[int, tuple[Path, str]] = {}
    for task in load_jsonl(_safe_workpack_path(root, manifest["visual_tasks"])):
        if not isinstance(task, dict) or not isinstance(task.get("page"), int):
            continue
        asset_path = task.get("page_asset")
        asset_hash = task.get("page_asset_sha256")
        if not isinstance(asset_path, str) or not isinstance(asset_hash, str):
            continue
        asset = _safe_workpack_path(root, asset_path)
        if _sha256_file(asset) != asset_hash:
            raise SemanticReviewWorkbenchError("page_asset_hash_mismatch")
        previous = page_assets.setdefault(task["page"], (asset, asset_hash))
        if previous != (asset, asset_hash):
            raise SemanticReviewWorkbenchError("page_asset_binding_conflict")

    return {
        "source_sha256": source_sha256,
        "root": root,
        "manifest": manifest,
        "plan": plan,
        "queue": queue,
        "units": units,
        "proposal_items": proposal_items,
        "assertions": assertion_by_id,
        "support": support_by_id,
        "page_assets": page_assets,
    }


def _assigned_queue(plan: dict[str, Any], queue: list[dict[str, Any]], reviewer: str) -> list[dict[str, Any]]:
    reviewers = sorted(
        value for value in plan.get("reviewer_instances", []) if isinstance(value, str)
    )
    if reviewer not in reviewers:
        raise SemanticReviewWorkbenchError("reviewer_not_registered")
    assigned = [
        row
        for row in queue
        if reviewer in reviewers[: int(row.get("required_reviewer_count", 2))]
    ]
    if not assigned:
        raise SemanticReviewWorkbenchError("reviewer_has_no_assigned_items")
    return assigned


def _span_texts(unit: dict[str, Any]) -> dict[tuple[Any, ...], str]:
    result: dict[tuple[Any, ...], str] = {}
    for field in ("focus_spans", "context_spans"):
        for span in unit.get(field, []):
            if not isinstance(span, dict):
                continue
            key = (
                span.get("page"),
                span.get("start"),
                span.get("end"),
                span.get("span_sha256"),
            )
            if isinstance(span.get("text"), str):
                result[key] = span["text"]
    return result


def _review_data(inputs: dict[str, Any], reviewer: str, output: Path) -> dict[str, Any]:
    plan = inputs["plan"]
    assigned = _assigned_queue(plan, inputs["queue"], reviewer)
    assets_dir = output / "assets"
    assets_dir.mkdir()
    os.chmod(assets_dir, 0o700)
    copied_assets: dict[int, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for row in assigned:
        unit_id = row["unit_id"]
        unit = inputs["units"].get(unit_id)
        if not isinstance(unit, dict):
            raise SemanticReviewWorkbenchError("review_unit_missing")
        span_text = _span_texts(unit)
        assertions: list[dict[str, Any]] = []
        pages: set[int] = set()
        for assertion_id in row.get("assertion_ids", []):
            assertion = inputs["assertions"].get(assertion_id)
            support = inputs["support"].get(assertion_id)
            if not isinstance(assertion, dict) or not isinstance(support, dict):
                raise SemanticReviewWorkbenchError("review_assertion_missing")
            evidence: list[dict[str, Any]] = []
            for span in assertion.get("evidence_spans", []):
                if not isinstance(span, dict):
                    continue
                key = (
                    span.get("page"),
                    span.get("start"),
                    span.get("end"),
                    span.get("span_sha256"),
                )
                text = span_text.get(key)
                if text is None:
                    raise SemanticReviewWorkbenchError("evidence_text_missing")
                pages.add(int(span["page"]))
                evidence.append({**span, "text": text})
            assertions.append(
                {
                    "assertion": assertion,
                    "support": support,
                    "evidence": evidence,
                }
            )
        page_rows: list[dict[str, Any]] = []
        for page in sorted(pages):
            asset = inputs["page_assets"].get(page)
            if asset is None:
                page_rows.append({"page": page, "asset": None, "sha256": None})
                continue
            source_asset, asset_hash = asset
            relative = f"assets/page-{page:04d}.png"
            target = output / relative
            if page not in copied_assets:
                shutil.copyfile(source_asset, target)
                os.chmod(target, 0o600)
                if _sha256_file(target) != asset_hash:
                    raise SemanticReviewWorkbenchError("copied_asset_hash_mismatch")
                copied_assets[page] = {
                    "page": page,
                    "path": relative,
                    "sha256": asset_hash,
                }
            page_rows.append(copied_assets[page])
        _, proposal = inputs["proposal_items"][row["item_ref"]]
        items.append(
            {
                "queue": row,
                "unit": {
                    "unit_id": unit_id,
                    "title": unit.get("title"),
                    "segment_id": unit.get("segment_id"),
                    "physical_pages": sorted(
                        {span.get("page") for span in unit.get("focus_spans", []) if isinstance(span, dict)}
                    ),
                },
                "proposal": proposal,
                "assertions": assertions,
                "pages": page_rows,
            }
        )
    return {
        "schema_version": WORKBENCH_SCHEMA,
        "protocol": SEMANTIC_REVIEW_WORKBENCH_PROTOCOL,
        "source_sha256": inputs["source_sha256"],
        "workpack_id": inputs["manifest"]["workpack_id"],
        "review_session_id": plan["review_session_id"],
        "review_plan_sha256": review_plan_sha256(plan),
        "reviewer_instance": reviewer,
        "proposer_instances": plan.get("proposer_instances", []),
        "items": items,
    }


def _instructions(manifest: dict[str, Any]) -> str:
    return f"""# 人工语义 Review 使用说明

本工作包属于 `fake-expert` 的独立语义审查，不是 Gold、模型预测或自动答案。

- Reviewer：`{manifest['reviewer_instance']}`
- Review session：`{manifest['review_session_id']}`
- Workbench：`{manifest['workbench_id']}`
- 分配对象数：`{manifest['item_count']}`

## 操作

1. 双击 `review.html`，逐项查看候选知识、每条 atomic assertion、support 记录、证据文本和页面 render。
2. 每条 assertion 都必须选择：`confirmed`、`correction-required` 或 `unsupported`，并填写针对该条来源的具体观察。
3. `accepted` 只能与 `full`、空 issue codes、所有 assertion 均 `confirmed` 同时使用。
4. `rejected` 或 `quarantined` 必须填写稳定 issue code，例如 `scope_overbroad`、`source_support_partial`。
5. 六项检查、四项整批声明必须由 reviewer 实际完成后勾选。不要只填写“无差异”“none”或复制同一理由。
6. 可随时导入此前导出的 submission 恢复进度。全部完成后点击“导出 submission”。

## 边界

- 页面不包含 prediction、隐藏答案或其他 reviewer 的结果。
- submission 只记录 reviewer 的人工判断；它单独不能满足双 reviewer 门禁。
- 不要编辑 `manifest.json`、`data.json`、`assets/` 或 `review.html`。
- 将导出的 JSON 交回 host 后，使用 `semantic_review_workbench.py finalize` 生成该 reviewer 的 fragment；两名 reviewer 的 fragment 必须再通过现有 `semantic_workpack.py merge-review` 合并。
"""


def _html_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("</", "<\\/")


def _render_html(manifest: dict[str, Any], data: dict[str, Any]) -> str:
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Independent Semantic Review</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1d232a;background:#f3f1eb}*{box-sizing:border-box}body{margin:0}.top{height:58px;display:flex;align-items:center;gap:12px;padding:10px 16px;background:#17202a;color:#fff}.top strong{font-size:16px}.top .muted{color:#bac5cf;font-size:12px}.layout{display:grid;grid-template-columns:250px minmax(420px,1fr) 440px;height:calc(100vh - 58px)}aside,.form{overflow:auto;background:#fff}aside{border-right:1px solid #d9dde2;padding:12px}.form{border-left:1px solid #d9dde2;padding:14px}.viewer{overflow:auto;padding:14px}.item{width:100%;text-align:left;border:1px solid #d8dde3;background:#fff;border-radius:8px;padding:10px;margin-bottom:8px;cursor:pointer}.item.active{border-color:#315f8f;background:#edf4fb}.item.done::after{content:" ✓";color:#18794e}.card{background:#fff;border:1px solid #d9dde2;border-radius:10px;padding:12px;margin-bottom:12px}.card h3,.card h4{margin:0 0 8px}.proposal{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;max-height:280px;overflow:auto;background:#f7f8fa;padding:10px;border-radius:6px}.assertion{border-left:4px solid #52779d}.evidence{white-space:pre-wrap;background:#fff8e6;border:1px solid #ead9a8;padding:8px;margin-top:7px;max-height:220px;overflow:auto;font-size:13px}.pages{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.pages button,.actions button{border:1px solid #aab3bc;background:#fff;border-radius:6px;padding:7px 10px;cursor:pointer}.pages button.active{background:#17202a;color:#fff}.imagebox{background:#25282c;overflow:auto;max-height:620px;text-align:center;border-radius:8px}.imagebox img{max-width:100%;transform-origin:top center}.field{margin-bottom:12px}.field label{display:block;font-weight:600;font-size:13px;margin-bottom:5px}.field input,.field select,.field textarea{width:100%;border:1px solid #b7c0c8;border-radius:6px;padding:8px;background:#fff}.field textarea{min-height:74px;resize:vertical}.checks label,.declarations label{display:block;font-weight:400;margin:7px 0}.difference{border:1px solid #d8dde3;border-radius:8px;padding:9px;margin:8px 0}.difference .locked{font-size:12px;color:#4c5965;margin-bottom:6px}.actions{display:flex;gap:8px;flex-wrap:wrap;position:sticky;bottom:0;background:#fff;padding:10px 0}.primary{background:#1d5f96!important;color:#fff!important}.warning{background:#fff1d6;border:1px solid #e7bd65;padding:9px;border-radius:7px;font-size:13px;margin-bottom:12px}.status{font-size:12px;color:#46515c}.hidden{display:none}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}@media(max-width:1100px){.layout{grid-template-columns:210px 1fr}.form{position:fixed;right:0;top:58px;bottom:0;width:440px;box-shadow:-4px 0 16px #0002}.viewer{margin-right:440px}}
</style></head><body>
<div class="top"><strong>Independent Semantic Review</strong><span id="identity" class="muted"></span><span id="progress" class="muted"></span></div>
<div class="layout"><aside><div id="items"></div></aside><main class="viewer"><div id="source"></div></main><section class="form">
<div class="warning">只根据本页的候选、atomic assertions、证据文本与页面 render 独立判断。不要使用 prediction、隐藏答案或另一 reviewer 的结果。</div>
<h2 id="formTitle"></h2><div class="field"><label>Verdict</label><select id="verdict"><option value="">请选择</option><option value="accepted">accepted</option><option value="rejected">rejected</option><option value="quarantined">quarantined</option></select></div>
<div class="field"><label>Evidence support</label><select id="support"><option value="">请选择</option><option value="full">full</option><option value="partial">partial</option><option value="none">none</option><option value="contradicted">contradicted</option><option value="not-applicable">not-applicable</option></select></div>
<div class="field"><label>Issue codes（逗号分隔；accepted 留空）</label><input id="issues" placeholder="例如 scope_overbroad,source_support_partial"></div>
<div class="field"><label>审查理由（必须针对本对象与来源）</label><textarea id="rationale" placeholder="说明核对了哪些来源内容、为什么得出此 verdict；不要只写“无差异”。"></textarea></div>
<div class="row"><div class="field"><label>Extraction confidence 0–1</label><input id="cx" type="number" min="0" max="1" step="0.01"></div><div class="field"><label>Interpretation confidence 0–1</label><input id="ci" type="number" min="0" max="1" step="0.01"></div></div>
<h3>逐 assertion 差异</h3><div id="differences"></div><h3>检查项</h3><div id="checks" class="checks"></div><div id="gap" class="hidden"></div>
<h3>整批声明</h3><div id="declarations" class="declarations"></div>
<div class="actions"><button id="prev">上一项</button><button id="save">保存当前项</button><button id="next">下一项</button><button id="import">导入 submission</button><button id="export" class="primary">导出 submission</button><input id="file" class="hidden" type="file" accept="application/json"></div><div id="status" class="status"></div>
</section></div>
<script id="manifest" type="application/json">__MANIFEST__</script><script id="data" type="application/json">__DATA__</script>
<script>
const M=JSON.parse(document.getElementById('manifest').textContent),D=JSON.parse(document.getElementById('data').textContent),key=`semantic-review:${M.workbench_id}:${M.reviewer_instance}`;
const checkNames=['evidence_span_checked','support_completeness_checked','scope_and_applicability_checked','conflict_checked','atomic_assertions_checked','differences_recorded'];const declarationNames=['source_evidence_inspected','no_prediction_or_hidden_answer_used','independent_from_proposer','differences_recorded'];let index=0,pageIndex=0,state={items:{},declarations:{}};try{state=JSON.parse(localStorage.getItem(key)||JSON.stringify(state))}catch(_){state={items:{},declarations:{}}}state.items=state.items||{};state.declarations=state.declarations||{};
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function blank(item){return{item_ref:item.queue.item_ref,review_input_sha256:item.queue.review_input_sha256,assertion_bundle_sha256:item.queue.assertion_bundle_sha256,verdict:'',evidence_support:'',issue_codes:[],rationale:'',confidence:{extraction:'',interpretation:''},review_checks:{},differences:item.queue.assertion_ids.map(id=>({assertion_id:id,status:'',observation:''})),gap_resolution:null}}
function current(){const item=D.items[index];state.items[item.queue.item_ref]=state.items[item.queue.item_ref]||blank(item);return state.items[item.queue.item_ref]}
function saveLocal(){localStorage.setItem(key,JSON.stringify(state));renderNav()}
function isDone(item){const v=state.items[item.queue.item_ref];if(!v||!v.verdict||!v.evidence_support||!v.rationale.trim())return false;if(!Number.isFinite(Number(v.confidence.extraction))||!Number.isFinite(Number(v.confidence.interpretation)))return false;if(checkNames.some(k=>v.review_checks[k]!==true)||(item.queue.requires_formula_check&&v.review_checks.formula_checked!==true))return false;if(v.differences.length!==item.queue.assertion_ids.length||v.differences.some(d=>!d.status||!d.observation.trim()))return false;if(v.verdict==='accepted'&&(v.evidence_support!=='full'||v.issue_codes.length||v.differences.some(d=>d.status!=='confirmed')))return false;if(v.verdict!=='accepted'&&!v.issue_codes.length)return false;return true}
function renderNav(){$('items').innerHTML=D.items.map((item,i)=>`<button class="item ${i===index?'active':''} ${isDone(item)?'done':''}" data-i="${i}"><b>${i+1}. ${esc(item.unit.title)}</b><br><span class="status">${esc(item.queue.item_ref)}</span></button>`).join('');document.querySelectorAll('[data-i]').forEach(b=>b.onclick=()=>{readForm();index=Number(b.dataset.i);pageIndex=0;render()});const done=D.items.filter(isDone).length;$('progress').textContent=`${done}/${D.items.length} 完成`}
function renderSource(item){const pageButtons=item.pages.map((p,i)=>`<button data-page="${i}" class="${i===pageIndex?'active':''}">Page ${p.page}</button>`).join('');const p=item.pages[pageIndex]||{};const image=p.asset?`<div class="actions"><button data-zoom="-0.15">缩小</button><button data-zoom="0.15">放大</button><button id="fit">适合宽度</button></div><div class="imagebox"><img id="pageImg" src="${esc(p.asset)}"></div>`:`<div class="warning">该页没有绑定 render，请以证据文本和 source hash 为准。</div>`;const assertions=item.assertions.map((entry,i)=>`<div class="card assertion"><h4>${i+1}. ${esc(entry.assertion.kind)} · ${esc(entry.assertion.assertion_id)}</h4><div class="proposal">${esc(JSON.stringify(entry.assertion.payload,null,2))}</div><p class="status">support=${esc(entry.support.status)} / ${esc(entry.support.support_kind)}</p>${entry.evidence.map(e=>`<div class="evidence"><b>Page ${e.page} · [${e.start},${e.end}]</b>\n${esc(e.text)}</div>`).join('')}</div>`).join('');$('source').innerHTML=`<div class="card"><h3>${esc(item.unit.title)}</h3><p class="status">Pages ${esc(item.unit.physical_pages.join(', '))}</p><h4>候选知识对象</h4><div class="proposal">${esc(JSON.stringify(item.proposal,null,2))}</div></div><div class="card"><div class="pages">${pageButtons}</div>${image}</div>${assertions}`;document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>{pageIndex=Number(b.dataset.page);renderSource(item)});let zoom=1;document.querySelectorAll('[data-zoom]').forEach(b=>b.onclick=()=>{zoom=Math.max(.25,Math.min(3,zoom+Number(b.dataset.zoom)));if($('pageImg'))$('pageImg').style.transform=`scale(${zoom})`});if($('fit'))$('fit').onclick=()=>{zoom=1;$('pageImg').style.transform='scale(1)'}}
function readForm(){if(!D.items.length)return;const item=D.items[index],v=current();v.verdict=$('verdict').value;v.evidence_support=$('support').value;v.issue_codes=$('issues').value.split(',').map(s=>s.trim()).filter(Boolean);v.rationale=$('rationale').value;v.confidence={extraction:$('cx').value,interpretation:$('ci').value};v.review_checks={};document.querySelectorAll('[data-check]').forEach(e=>v.review_checks[e.dataset.check]=e.checked);v.differences=item.queue.assertion_ids.map(id=>({assertion_id:id,status:document.querySelector(`[data-diff-status="${id}"]`).value,observation:document.querySelector(`[data-diff-observation="${id}"]`).value}));declarationNames.forEach(k=>state.declarations[k]=document.querySelector(`[data-declaration="${k}"]`).checked);saveLocal()}
function renderForm(item){const v=current();$('formTitle').textContent=item.unit.title;$('verdict').value=v.verdict;$('support').value=v.evidence_support;$('issues').value=v.issue_codes.join(',');$('rationale').value=v.rationale;$('cx').value=v.confidence.extraction;$('ci').value=v.confidence.interpretation;$('differences').innerHTML=item.assertions.map((entry,i)=>{const d=v.differences.find(x=>x.assertion_id===entry.assertion.assertion_id)||{};return`<div class="difference"><div class="locked">${i+1}. ${esc(entry.assertion.kind)} · ${esc(JSON.stringify(entry.assertion.payload))}</div><select data-diff-status="${entry.assertion.assertion_id}"><option value="">请选择</option><option value="confirmed">confirmed</option><option value="correction-required">correction-required</option><option value="unsupported">unsupported</option></select><textarea data-diff-observation="${entry.assertion.assertion_id}" placeholder="具体说明核对了哪条来源内容以及发现/未发现什么差异。">${esc(d.observation||'')}</textarea></div>`}).join('');v.differences.forEach(d=>{const e=document.querySelector(`[data-diff-status="${d.assertion_id}"]`);if(e)e.value=d.status});const names=[...checkNames,...(item.queue.requires_formula_check?['formula_checked']:[])];$('checks').innerHTML=names.map(k=>`<label><input type="checkbox" data-check="${k}" ${v.review_checks[k]?'checked':''}> ${k}</label>`).join('');$('declarations').innerHTML=declarationNames.map(k=>`<label><input type="checkbox" data-declaration="${k}" ${state.declarations[k]?'checked':''}> ${k}</label>`).join('')}
function render(){renderNav();const item=D.items[index];renderSource(item);renderForm(item);$('status').textContent=''}
function validateAll(){readForm();if(declarationNames.some(k=>state.declarations[k]!==true))return'四项整批声明尚未全部确认';const missing=D.items.filter(item=>!isDone(item));if(missing.length)return`${missing.length} 个对象尚未完整填写`;return''}
function exportSubmission(){const err=validateAll();if(err){$('status').textContent=err;return}const payload={schema_version:'tkc.semantic-review-workbench-submission/v0.1',workbench_id:M.workbench_id,reviewer_instance:M.reviewer_instance,review_plan_sha256:M.review_plan_sha256,exported_at:new Date().toISOString(),declarations:state.declarations,items:D.items.map(item=>state.items[item.queue.item_ref])};const blob=new Blob([JSON.stringify(payload,null,2)+'\\n'],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`semantic-review-submission-${M.workbench_id}-${M.reviewer_instance}.json`;a.click();URL.revokeObjectURL(a.href);$('status').textContent='submission 已导出'}
$('save').onclick=()=>{readForm();$('status').textContent='已保存到本机浏览器'};$('prev').onclick=()=>{readForm();index=Math.max(0,index-1);pageIndex=0;render()};$('next').onclick=()=>{readForm();index=Math.min(D.items.length-1,index+1);pageIndex=0;render()};$('export').onclick=exportSubmission;$('import').onclick=()=>$('file').click();$('file').onchange=async e=>{try{const v=JSON.parse(await e.target.files[0].text());if(v.workbench_id!==M.workbench_id||v.reviewer_instance!==M.reviewer_instance||v.review_plan_sha256!==M.review_plan_sha256)throw Error('submission 与本工作台不匹配');state={items:Object.fromEntries(v.items.map(x=>[x.item_ref,x])),declarations:v.declarations||{}};saveLocal();render();$('status').textContent='submission 已导入'}catch(err){$('status').textContent=err.message}};
$('identity').textContent=`${M.reviewer_instance} · ${M.review_session_id}`;render();
</script></body></html>
""".replace("__MANIFEST__", _html_json(manifest)).replace("__DATA__", _html_json(data))


def generate_workbench(source: Path, workpack: Path, reviewer: str, output: Path) -> dict[str, Any]:
    inputs = _load_frozen_inputs(source, workpack)
    output = _empty_output(output)
    data = _review_data(inputs, reviewer, output)
    data_hash = sha256_json(data)
    plan_hash = review_plan_sha256(inputs["plan"])
    workbench_id = "srw-" + hashlib.sha256(
        _canonical_json(
            {
                "plan_sha256": plan_hash,
                "reviewer": reviewer,
                "data_sha256": data_hash,
            }
        )
    ).hexdigest()[:20]
    asset_rows = sorted(
        {
            (page["path"], page["sha256"])
            for item in data["items"]
            for page in item["pages"]
            if page.get("path") and page.get("sha256")
        }
    )
    manifest = {
        "schema_version": WORKBENCH_SCHEMA,
        "protocol": SEMANTIC_REVIEW_WORKBENCH_PROTOCOL,
        "compiler_version": COMPILER_VERSION,
        "workbench_id": workbench_id,
        "source_sha256": inputs["source_sha256"],
        "workpack_id": inputs["manifest"]["workpack_id"],
        "review_session_id": inputs["plan"]["review_session_id"],
        "review_plan_sha256": plan_hash,
        "reviewer_instance": reviewer,
        "proposer_instances": inputs["plan"].get("proposer_instances", []),
        "item_count": len(data["items"]),
        "assigned_item_refs": [item["queue"]["item_ref"] for item in data["items"]],
        "data": {"path": "data.json", "sha256": data_hash},
        "assets": [
            {"path": path, "sha256": digest}
            for path, digest in asset_rows
        ],
        "predictions_included": False,
        "hidden_answers_included": False,
        "network_required": False,
        "model_invocations": 0,
        "release_included": False,
    }
    instructions = _instructions(manifest)
    manifest["instructions"] = {
        "path": "人工语义Review使用说明.md",
        "sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
    }
    _write_json(output / "data.json", data)
    _write_text(output / "人工语义Review使用说明.md", instructions)
    _write_json(output / "manifest.json", manifest)
    _write_text(output / "review.html", _render_html(manifest, data))
    result = validate_workbench(source, workpack, output)
    if not result["passed"]:
        raise SemanticReviewWorkbenchError(result["issues"][0])
    return {
        "status": "paused-independent-semantic-review",
        "workbench": str(output),
        "workbench_id": workbench_id,
        "reviewer_instance": reviewer,
        "item_count": len(data["items"]),
        "review_plan_sha256": plan_hash,
        "model_invocations": 0,
        "network_used": False,
    }


def validate_workbench(source: Path, workpack: Path, root: Path) -> dict[str, Any]:
    issues: list[str] = []
    inputs = _load_frozen_inputs(source, workpack)
    root = root.expanduser().resolve()
    try:
        manifest = load_json(root / "manifest.json")
        data = load_json(root / "data.json")
    except Exception as error:
        return {"passed": False, "issues": [f"workbench_files_invalid:{error}"]}
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != WORKBENCH_SCHEMA
        or manifest.get("protocol") != SEMANTIC_REVIEW_WORKBENCH_PROTOCOL
    ):
        issues.append("workbench_manifest_invalid")
    reviewer = manifest.get("reviewer_instance")
    try:
        assigned = _assigned_queue(inputs["plan"], inputs["queue"], str(reviewer))
    except SemanticReviewWorkbenchError as error:
        issues.append(str(error))
        assigned = []
    expected_refs = [row["item_ref"] for row in assigned]
    if (
        manifest.get("compiler_version") != COMPILER_VERSION
        or manifest.get("source_sha256") != inputs["source_sha256"]
        or manifest.get("workpack_id") != inputs["manifest"].get("workpack_id")
        or manifest.get("review_session_id") != inputs["plan"].get("review_session_id")
        or manifest.get("review_plan_sha256") != review_plan_sha256(inputs["plan"])
        or manifest.get("assigned_item_refs") != expected_refs
        or manifest.get("item_count") != len(expected_refs)
        or manifest.get("predictions_included") is not False
        or manifest.get("hidden_answers_included") is not False
        or manifest.get("network_required") is not False
        or manifest.get("release_included") is not False
    ):
        issues.append("workbench_binding_invalid")
    if not isinstance(data, dict) or data.get("reviewer_instance") != reviewer:
        issues.append("workbench_data_invalid")
    elif [item.get("queue", {}).get("item_ref") for item in data.get("items", [])] != expected_refs:
        issues.append("workbench_item_coverage_invalid")
    if manifest.get("data", {}).get("sha256") != sha256_json(data):
        issues.append("workbench_data_hash_mismatch")
    for entry in manifest.get("assets", []):
        try:
            path = _safe_workpack_path(root, entry.get("path"))
        except SemanticReviewWorkbenchError:
            issues.append("workbench_asset_invalid")
            continue
        if _sha256_file(path) != entry.get("sha256"):
            issues.append("workbench_asset_hash_mismatch")
    instructions = root / str(manifest.get("instructions", {}).get("path", ""))
    if (
        not instructions.is_file()
        or instructions.is_symlink()
        or _sha256_file(instructions) != manifest.get("instructions", {}).get("sha256")
    ):
        issues.append("workbench_instructions_invalid")
    html = root / "review.html"
    expected_html = _render_html(manifest, data).encode("utf-8") if isinstance(data, dict) else b""
    if not html.is_file() or html.is_symlink() or html.read_bytes() != expected_html:
        issues.append("workbench_html_invalid")
    return {
        "passed": not issues,
        "issues": sorted(set(issues)),
        "workbench_id": manifest.get("workbench_id") if isinstance(manifest, dict) else None,
        "reviewer_instance": reviewer,
        "item_count": len(expected_refs),
    }


def _submission_records(
    submission: dict[str, Any],
    manifest: dict[str, Any],
    inputs: dict[str, Any],
) -> list[dict[str, Any]]:
    if (
        submission.get("schema_version") != SUBMISSION_SCHEMA
        or submission.get("workbench_id") != manifest.get("workbench_id")
        or submission.get("reviewer_instance") != manifest.get("reviewer_instance")
        or submission.get("review_plan_sha256") != manifest.get("review_plan_sha256")
        or any(submission.get("declarations", {}).get(name) is not True for name in DECLARATIONS)
    ):
        raise SemanticReviewWorkbenchError("submission_binding_or_declarations_invalid")
    assigned = _assigned_queue(inputs["plan"], inputs["queue"], manifest["reviewer_instance"])
    submitted = submission.get("items")
    if not isinstance(submitted, list):
        raise SemanticReviewWorkbenchError("submission_items_invalid")
    by_ref = {
        row.get("item_ref"): row
        for row in submitted
        if isinstance(row, dict) and isinstance(row.get("item_ref"), str)
    }
    if len(by_ref) != len(submitted) or set(by_ref) != {row["item_ref"] for row in assigned}:
        raise SemanticReviewWorkbenchError("submission_item_coverage_invalid")
    records: list[dict[str, Any]] = []
    for queue_row in assigned:
        item = by_ref[queue_row["item_ref"]]
        if (
            item.get("review_input_sha256") != queue_row.get("review_input_sha256")
            or item.get("assertion_bundle_sha256") != queue_row.get("assertion_bundle_sha256")
        ):
            raise SemanticReviewWorkbenchError("submission_item_binding_invalid")
        verdict = item.get("verdict")
        support = item.get("evidence_support")
        issue_codes = item.get("issue_codes")
        rationale = item.get("rationale")
        confidence = item.get("confidence")
        checks = item.get("review_checks")
        differences = item.get("differences")
        if (
            verdict not in {"accepted", "rejected", "quarantined"}
            or support not in {"full", "partial", "none", "contradicted", "not-applicable"}
            or not isinstance(issue_codes, list)
            or len(issue_codes) != len(set(issue_codes))
            or any(not isinstance(code, str) or not ISSUE_CODE_RE.fullmatch(code) for code in issue_codes)
            or not isinstance(rationale, str)
            or len(rationale.strip()) < 12
            or not isinstance(confidence, dict)
            or any(
                not isinstance(confidence.get(name), (int, float, str))
                or isinstance(confidence.get(name), bool)
                for name in ("extraction", "interpretation")
            )
        ):
            raise SemanticReviewWorkbenchError("submission_review_fields_invalid")
        normalized_confidence: dict[str, float] = {}
        for name in ("extraction", "interpretation"):
            try:
                value = float(confidence[name])
            except (TypeError, ValueError) as error:
                raise SemanticReviewWorkbenchError("submission_confidence_invalid") from error
            if not 0 <= value <= 1:
                raise SemanticReviewWorkbenchError("submission_confidence_invalid")
            normalized_confidence[name] = value
        required_checks = set(REVIEW_CHECKS)
        if queue_row.get("requires_formula_check") is True:
            required_checks.add("formula_checked")
        if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
            raise SemanticReviewWorkbenchError("submission_checks_incomplete")
        if not isinstance(differences, list):
            raise SemanticReviewWorkbenchError("submission_differences_invalid")
        difference_by_id = {
            row.get("assertion_id"): row
            for row in differences
            if isinstance(row, dict) and isinstance(row.get("assertion_id"), str)
        }
        if (
            len(difference_by_id) != len(differences)
            or set(difference_by_id) != set(queue_row.get("assertion_ids", []))
            or any(
                row.get("status") not in {"confirmed", "correction-required", "unsupported"}
                or not isinstance(row.get("observation"), str)
                or len(row["observation"].strip()) < 12
                for row in differences
            )
        ):
            raise SemanticReviewWorkbenchError("submission_differences_invalid")
        ordered_differences = [difference_by_id[value] for value in queue_row["assertion_ids"]]
        if verdict == "accepted":
            if support != "full" or issue_codes or any(row["status"] != "confirmed" for row in ordered_differences):
                raise SemanticReviewWorkbenchError("submission_acceptance_inconsistent")
        elif not issue_codes:
            raise SemanticReviewWorkbenchError("submission_issue_codes_required")
        record: dict[str, Any] = {
            "schema_version": ATTESTATION_SCHEMA,
            "attestation_id": expected_attestation_id(
                inputs["plan"], queue_row, manifest["reviewer_instance"]
            ),
            "attestation_level": ATTESTATION_LEVEL,
            "item_ref": queue_row["item_ref"],
            "item_kind": queue_row["item_kind"],
            "reviewer_instance": manifest["reviewer_instance"],
            "proposer_instance": queue_row["proposer_instance"],
            "review_session_id": inputs["plan"]["review_session_id"],
            "review_plan_sha256": review_plan_sha256(inputs["plan"]),
            "item_sha256": queue_row["item_sha256"],
            "review_input_sha256": queue_row["review_input_sha256"],
            "assertion_ids": queue_row["assertion_ids"],
            "assertion_bundle_sha256": queue_row["assertion_bundle_sha256"],
            "verdict": verdict,
            "evidence_support": support,
            "issue_codes": issue_codes,
            "rationale": rationale.strip(),
            "confidence": normalized_confidence,
            "review_method": "fresh-source-inspection",
            "review_checks": {name: True for name in sorted(required_checks)},
            "inspected_artifacts": {
                "source_sha256": inputs["plan"]["source_sha256"],
                "evidence_span_sha256s": queue_row.get("evidence_span_sha256s", []),
                "render_sha256s": queue_row.get("render_sha256s", []),
            },
            "differences": ordered_differences,
        }
        if queue_row.get("item_kind") == "gap":
            gap_resolution = item.get("gap_resolution")
            if not isinstance(gap_resolution, dict):
                raise SemanticReviewWorkbenchError("submission_gap_resolution_invalid")
            record["gap_resolution"] = gap_resolution
        records.append(record)

    validation_queue = [{**row, "required_reviewer_count": 1} for row in assigned]
    issues, _, _ = validate_attestations(records, validation_queue, inputs["plan"])
    if issues:
        raise SemanticReviewWorkbenchError(f"projected_fragment_invalid:{issues[0].code}")
    return records


def finalize_submission(
    source: Path,
    workpack: Path,
    workbench: Path,
    submission_path: Path,
    output: Path,
) -> dict[str, Any]:
    validation = validate_workbench(source, workpack, workbench)
    if not validation["passed"]:
        raise SemanticReviewWorkbenchError(validation["issues"][0])
    inputs = _load_frozen_inputs(source, workpack)
    manifest = load_json(workbench / "manifest.json")
    submission = load_json(submission_path.expanduser().resolve())
    if not isinstance(submission, dict):
        raise SemanticReviewWorkbenchError("submission_invalid")
    records = _submission_records(submission, manifest, inputs)
    output = _empty_output(output)
    fragment_path = output / "semantic-review-attestations.jsonl"
    submission_copy = output / "review-submission.json"
    _write_jsonl(fragment_path, records)
    _write_json(submission_copy, submission)
    fragment_hash = _sha256_file(fragment_path)
    submission_hash = _sha256_file(submission_copy)
    revision = {
        "schema_version": REVISION_SCHEMA,
        "protocol": SEMANTIC_REVIEW_WORKBENCH_PROTOCOL,
        "compiler_version": COMPILER_VERSION,
        "status": "complete-single-reviewer-fragment",
        "workbench_id": manifest["workbench_id"],
        "reviewer_instance": manifest["reviewer_instance"],
        "review_session_id": manifest["review_session_id"],
        "review_plan_sha256": manifest["review_plan_sha256"],
        "item_count": len(records),
        "fragment": {
            "path": fragment_path.name,
            "sha256": fragment_hash,
            "record_count": len(records),
        },
        "submission": {"path": submission_copy.name, "sha256": submission_hash},
        "independent_gate_complete": False,
        "requires_other_registered_reviewers": True,
        "release_included": False,
    }
    revision["revision_sha256"] = sha256_json(revision)
    _write_json(output / "revision.json", revision)
    verified = validate_revision(source, workpack, output)
    if not verified["passed"]:
        raise SemanticReviewWorkbenchError(verified["issues"][0])
    return {
        "status": revision["status"],
        "revision": str(output),
        "reviewer_instance": revision["reviewer_instance"],
        "item_count": len(records),
        "fragment": str(fragment_path),
        "fragment_sha256": fragment_hash,
        "independent_gate_complete": False,
    }


def validate_revision(source: Path, workpack: Path, root: Path) -> dict[str, Any]:
    issues: list[str] = []
    root = root.expanduser().resolve()
    inputs = _load_frozen_inputs(source, workpack)
    try:
        revision = load_json(root / "revision.json")
        submission = load_json(root / "review-submission.json")
        records = load_jsonl(root / "semantic-review-attestations.jsonl")
    except Exception as error:
        return {"passed": False, "issues": [f"revision_files_invalid:{error}"]}
    if (
        not isinstance(revision, dict)
        or revision.get("schema_version") != REVISION_SCHEMA
        or revision.get("protocol") != SEMANTIC_REVIEW_WORKBENCH_PROTOCOL
    ):
        issues.append("revision_manifest_invalid")
    reviewer = revision.get("reviewer_instance")
    manifest = {
        "workbench_id": revision.get("workbench_id"),
        "reviewer_instance": reviewer,
        "review_session_id": revision.get("review_session_id"),
        "review_plan_sha256": revision.get("review_plan_sha256"),
    }
    try:
        expected_records = _submission_records(submission, manifest, inputs)
    except SemanticReviewWorkbenchError as error:
        issues.append(str(error))
        expected_records = []
    if records != expected_records:
        issues.append("revision_fragment_replay_mismatch")
    if (
        revision.get("compiler_version") != COMPILER_VERSION
        or revision.get("status") != "complete-single-reviewer-fragment"
        or revision.get("review_plan_sha256") != review_plan_sha256(inputs["plan"])
        or revision.get("item_count") != len(expected_records)
        or revision.get("fragment", {}).get("sha256") != _sha256_file(root / "semantic-review-attestations.jsonl")
        or revision.get("submission", {}).get("sha256") != _sha256_file(root / "review-submission.json")
        or revision.get("independent_gate_complete") is not False
        or revision.get("requires_other_registered_reviewers") is not True
        or revision.get("release_included") is not False
    ):
        issues.append("revision_binding_invalid")
    unsigned = dict(revision)
    stored_hash = unsigned.pop("revision_sha256", None)
    if stored_hash != sha256_json(unsigned):
        issues.append("revision_hash_mismatch")
    return {
        "passed": not issues,
        "issues": sorted(set(issues)),
        "reviewer_instance": reviewer,
        "item_count": len(expected_records),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--source", type=Path, required=True)
    generate.add_argument("--workpack", type=Path, required=True)
    generate.add_argument("--reviewer-instance", required=True)
    generate.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--source", type=Path, required=True)
    validate.add_argument("--workpack", type=Path, required=True)
    validate.add_argument("--workbench", type=Path, required=True)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--source", type=Path, required=True)
    finalize.add_argument("--workpack", type=Path, required=True)
    finalize.add_argument("--workbench", type=Path, required=True)
    finalize.add_argument("--submission", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    revision = sub.add_parser("validate-revision")
    revision.add_argument("--source", type=Path, required=True)
    revision.add_argument("--workpack", type=Path, required=True)
    revision.add_argument("--revision", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "generate":
            result = generate_workbench(
                args.source, args.workpack, args.reviewer_instance, args.output
            )
        elif args.command == "validate":
            result = validate_workbench(args.source, args.workpack, args.workbench)
        elif args.command == "finalize":
            result = finalize_submission(
                args.source,
                args.workpack,
                args.workbench,
                args.submission,
                args.output,
            )
        else:
            result = validate_revision(args.source, args.workpack, args.revision)
    except (OSError, ValueError, KeyError, SemanticReviewWorkbenchError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("passed", True) else 1


if __name__ == "__main__":
    sys.exit(main())
