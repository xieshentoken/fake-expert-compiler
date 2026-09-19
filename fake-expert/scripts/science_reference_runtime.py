#!/usr/bin/env python3
"""Read-only science-reference protocol. No inference, promotion or execution."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
from pathlib import Path

import consultation_contract as cc
import science_contract as sc
import science_index as si
import science_workpack as sw
from compiler_version import SCIENCE_CONSULTATION_SCHEMA, SCIENCE_REFERENCE_PROTOCOL
from portable_reference_runtime import query_catalog

DEFAULT_LIMITS = {"max_hops": 3, "max_objects": 64, "max_bytes": 1048576}


def _entry(library, reference):
    matches = [e for e in library if e["package"]["fingerprint"]["package_id"] == reference["package_id"]
               and e["package"]["fingerprint"]["package_version"] == reference["package_version"]]
    if len(matches) != 1:
        raise ValueError("science_object_package_unavailable")
    return matches[0]


def read_object(library, reference):
    sc.validate_shape(reference, "object_ref")
    package = _entry(library, reference)["package"]
    obj = sc._object(reference, package)
    return copy.deepcopy({"object_ref": reference, "body": obj, "evidence_refs": si.evidence_refs(package, obj),
                          "body_is_original_source_text": False, "execution_authorized": False})


def read_locator(library, reference):
    sc.validate_shape(reference, "evidence_ref")
    package = _entry(library, reference["object_ref"])["package"]
    anchor = sc._evidence(reference, package)
    # Allowlist keeps legacy source locators usable without leaking operator paths.
    locator = {k: copy.deepcopy(anchor[k]) for k in ("id", "source_id", "pages", "printed_page_labels",
               "text_spans", "excerpt_sha256") if k in anchor}
    locator["extraction"] = {k: anchor.get("locator", {})[k] for k in
                             ("extractor", "hash_scope", "normalization", "offset_unit") if k in anchor.get("locator", {})}
    locator["scan_locator_present"] = isinstance(anchor.get("scan_locator"), dict)
    return {"evidence_ref": copy.deepcopy(reference), "locator": locator,
            "original_source_status": "external-source-unavailable"}


def read_source(library, reference, source_map=None, source_root=None, *, context_chars=0):
    result = read_locator(library, reference)
    if source_map is None or source_root is None:
        return {**result, "status": "external-source-unavailable", "reason_codes": ["authorized_original_required"]}
    if type(context_chars) is not int or not 0 <= context_chars <= 512:
        raise ValueError("science_context_limit_invalid")
    cc.shape(source_map, "source_map")
    root = Path(source_root).absolute()
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError("science_source_root_unsafe")
    if not root.is_dir():
        return {**result, "status": "external-source-unavailable", "reason_codes": ["authorized_source_root_missing"]}
    package = _entry(library, reference["object_ref"])["package"]
    anchor = sc._evidence(reference, package)
    sources = [s for s in package["manifest"]["sources"] if s["source_id"] == anchor["source_id"]]
    if len(sources) != 1:
        raise ValueError("science_source_identity_ambiguous")
    source_hash = sources[0]["sha256"]
    rows = [s for s in source_map["sources"] if s["source_id"] == anchor["source_id"] and s["source_sha256"] == source_hash]
    if len(rows) != 1:
        raise ValueError("science_source_not_authorized_or_ambiguous")
    grant = rows[0]
    try:
        file = sw._safe_path(root, grant["path"])
    except ValueError as error:
        if str(error) != "science_package_file_missing":
            raise
        return {**result, "status": "external-source-unavailable", "reason_codes": ["authorized_original_missing"]}
    if not file.resolve().is_relative_to(root.resolve()) or file.stat().st_size > 64 * sc.MAX_BYTES:
        raise ValueError("science_source_outside_root_or_too_large")
    pages = anchor.get("pages", [])
    if (not pages or len(pages) > 20 or len(set(pages)) != len(pages)
            or any(type(p) is not int or p not in grant["allowed_pages"] for p in pages)):
        raise ValueError("science_source_pages_not_authorized")
    data = file.read_bytes()
    if hashlib.sha256(data).hexdigest() != source_hash:
        raise ValueError("science_source_hash_mismatch")
    if anchor.get("scan_locator"):
        return {**result, "status": "external-source-unavailable", "reason_codes": ["scan_replay_not_authorized"],
                "source_sha256": source_hash}
    locator = anchor.get("locator", {})
    if (locator.get("extractor") != "pypdf-6.10.0" or locator.get("hash_scope") != "normalized native text spans"
            or locator.get("normalization") != "unicode-nfkc-collapse-whitespace-v1"):
        return {**result, "status": "external-source-unavailable", "reason_codes": ["native_span_locator_required"]}
    try:
        import pypdf
        from verify_source_anchors import normalize_text
    except ImportError:
        return {**result, "status": "external-source-unavailable", "reason_codes": ["native_parser_unavailable"]}
    if pypdf.__version__ != "6.10.0":
        raise ValueError("science_source_parser_version_mismatch")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted or max(pages) > len(reader.pages):
            raise ValueError("science_source_encrypted_or_page_missing")
        texts = {p: normalize_text(reader.pages[p - 1].extract_text() or "") for p in pages}
    except pypdf.errors.PyPdfError as error:
        raise ValueError("science_source_parse_failed") from error
    spans, contexts = [], []
    if not anchor.get("text_spans"):
        raise ValueError("science_source_spans_missing")
    for span in anchor["text_spans"]:
        page, start, end = span["page"], span["start"], span["end"]
        if (page not in texts or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(texts[page])):
            raise ValueError("science_source_span_out_of_range")
        text = texts[page]
        excerpt = text[start:end]
        if (hashlib.sha256(text.encode()).hexdigest() != span["page_text_sha256"]
                or hashlib.sha256(excerpt.encode()).hexdigest() != span["span_sha256"]):
            raise ValueError("science_source_span_hash_mismatch")
        spans.append({"page": page, "start": start, "end": end, "text": excerpt, "span_sha256": span["span_sha256"]})
        if context_chars:
            contexts.append({"page": page, "before": text[max(0, start - context_chars):start],
                             "after": text[end:end + context_chars], "is_supporting_evidence": False})
    if hashlib.sha256("\f".join(r["text"] for r in spans).encode()).hexdigest() != anchor["excerpt_sha256"]:
        raise ValueError("science_source_excerpt_hash_mismatch")
    output = {**result, "status": "source-spans-verified", "original_source_status": "hash-and-native-spans-verified",
              "source_sha256": source_hash, "evidence_spans": spans, "context": contexts,
              "semantic_support_verified": False, "execution_authorized": False}
    sc.canonical_json(output)
    return output


def _record_ref(entry, record):
    return {"base_package": entry["package"]["fingerprint"], "sidecar_sha256": entry["sidecar"]["sidecar_sha256"],
            "record_id": record["id"], "record_sha256": record["content_sha256"]}


def closure(library, seeds, *, limits=None):
    limits = copy.deepcopy(DEFAULT_LIMITS if limits is None else limits)
    cc.shape(limits, "limits")
    objects, records, links, unresolved, missing = [], [], [], [], []
    requirements = {sc.sha256_json(e["package"]["fingerprint"]): sc.validate_sidecar(
        e["sidecar"], e["package"], e["profile"]).get("missing_requirements", []) for e in library}
    visited, pending = set(), [("object", reference, 0) for reference in seeds]
    nodes = 0; bytes_used = 0; omitted = 0
    def gap(reason, reference, field=""):
        nonlocal omitted
        row = {"reason_code": reason, "owner_sha256": sc.sha256_json(reference), "field": field,
               "task": "compile-actual-definition-unit" if "definition" in field else "resolve-scoped-dependency"}
        if row not in unresolved:
            if len(unresolved) < 64: unresolved.append(row)
            else: omitted += 1
    while pending:
        kind, reference, depth = pending.pop(0)
        key = (kind, sc.sha256_json(reference))
        if key in visited:
            continue
        visited.add(key)
        if depth > limits["max_hops"] or nodes >= limits["max_objects"]:
            gap("closure_truncated", reference, "hop_limit" if depth > limits["max_hops"] else "object_limit")
            continue
        if kind == "object":
            entry = _entry(library, reference); package = entry["package"]
            obj = sc._object(reference, package)
            projected = {"object_ref": reference, "body": obj, "evidence_refs": si.evidence_refs(package, obj)}
        else:
            entry = next(e for e in library if e["package"]["fingerprint"] == reference["base_package"])
            record = next(r for r in entry["sidecar"]["records"] if r["id"] == reference["record_id"])
            projected = {"record_ref": reference, "record": record}
        size = len(sc.canonical_json(projected))
        # Reserve half the bound for assurance, conditions, unresolved links and protocol metadata.
        if bytes_used + size > limits["max_bytes"] // 2:
            gap("closure_truncated", reference, "byte_limit")
            continue
        bytes_used += size; nodes += 1
        if kind == "object":
            objects.append(copy.deepcopy(projected))
            attached = [r for r in (entry["sidecar"]["records"] if entry["sidecar"] else []) if r["source_object_ref"] == reference]
            pending.extend(("record", _record_ref(entry, r), depth + 1) for r in attached)
            for relation in package.get("relations", []):
                if obj["id"] not in (relation["source_id"], relation["target_id"]):
                    continue
                target_id = relation["target_id"] if relation["source_id"] == obj["id"] else relation["source_id"]
                targets = [o for o in package["objects"] if o["id"] == target_id]
                link = {"package_fingerprint": package["fingerprint"], "type": relation["type"],
                        "source_id": relation["source_id"], "target_id": relation["target_id"],
                        "relation_sha256": sc.sha256_json(relation), "review_state": "unverified"}
                if link not in links:
                    if len(links) < 128: links.append(link)
                    else: gap("closure_truncated", reference, "link_limit")
                if targets: pending.append(("object", si.object_ref(package, targets[0]), depth + 1))
                else: gap("related_object_missing", reference, target_id)
                if not relation.get("review_binding"):
                    gap("relation_independent_review_required", reference, relation["type"])
        else:
            records.append(copy.deepcopy(projected))
            for requirement in requirements[sc.sha256_json(entry["package"]["fingerprint"])]:
                if requirement["record_id"] == record["id"]:
                    gap(requirement["reason_code"], reference, requirement["field"])
                    missing.append("science:" + sc.sha256_json(reference) + ":" + requirement["field"])
            for field, value in record.items():
                if sc._unknown(value): gap("science_field_unknown", reference, field)
            for nested in sc._walk(record):
                if set(nested) == {"package_id", "package_version", "object_id", "object_sha256"}:
                    pending.append(("object", nested, depth + 1))
            local_refs = [(field, ident) for field in ("symbol_bindings", "dependency_refs") for ident in record.get(field, [])]
            local_refs += [(f, record[f]) for f in ("condition_set_ref", "material_state_ref") if isinstance(record.get(f), str)]
            local_refs += [("derivation", r) for p in record["provenance"] for r in p["derivation_refs"]]
            for field, ident in sorted(set(local_refs)):
                target = next((r for r in entry["sidecar"]["records"] if r["id"] == ident), None)
                if target:
                    target_ref = _record_ref(entry, target)
                    link = {"type": field, "source_record_ref": reference, "target_record_ref": target_ref, "review_state": "candidate"}
                    if link not in links:
                        if len(links) < 128: links.append(link)
                        else: gap("closure_truncated", reference, "link_limit")
                    pending.append(("record", target_ref, depth + 1))
                else: gap("science_record_missing", reference, ident)
            if record["kind"] == "ConditionSet":
                missing.extend(f"{d['source']}:{d['name']}" for d in record["declared_inputs"])
    return {"selected_objects": objects, "science_records": records, "typed_links": links,
            "unresolved_dependencies": unresolved, "unresolved_omitted_count": omitted,
            "declared_inputs": sorted(set(missing)), "closure_status": "incomplete" if unresolved or omitted else "complete",
            "closure_truncated": any(g["reason_code"] == "closure_truncated" for g in unresolved) or omitted > 0,
            "limits": limits}


def query(request, library, index, tokenizer, *, limits=None):
    cc.validate_request(request, library)
    si.validate_index(index, library, tokenizer)
    scoped = [e for e in library if e["package"]["fingerprint"] in request["source_scope"]]
    legacy, allowed = [], []
    for entry in scoped:
        response = query_catalog(entry["catalog"], request["question"])
        legacy.append({"package_fingerprint": entry["package"]["fingerprint"], "response": response})
        # A lexical miss is retained as a legacy miss; the new protocol may return
        # new index candidates. Policy/decision/gap abstentions still block retrieval.
        if response["behavior"] != "abstain" or response["reason_codes"] == ["no_supported_reference"]:
            allowed.append(entry["package"]["fingerprint"])
    matches = si.search(index, request["question"], tokenizer, scope=allowed, domains=request["domain_candidates"], limit=8)
    closed = closure(scoped, [r["object_ref"] for r in matches], limits=limits)
    warnings = ["science-reference-candidate", "semantic-support-unavailable", "not-executable",
                "original-source-not-read", "scope-only-no-whole-book-absence-claim"]
    limitations = ["production-knowledge-publication-blocked", "domain-accuracy-blocked", "independent-science-review-required"]
    if not matches:
        limitations.append("no-support-in-requested-scope")
        closed["closure_status"] = "incomplete"
    if any(row["response"]["behavior"] == "abstain" for row in legacy):
        limitations.append("legacy-abstain-preserved")
    if matches and any(row["response"]["reason_codes"] == ["no_supported_reference"] for row in legacy):
        warnings.append("new-protocol-candidates-with-legacy-lexical-miss")
    if closed["closure_status"] != "complete": limitations.append("evidence-closure-incomplete")
    if closed["closure_truncated"]: warnings.append("closure-truncated")
    if request["execution_requested"] or request["intent"] == "calculate": limitations.append("calculation-unavailable-m2")
    if request["user_assumptions"]: warnings.append("user-assumptions-not-verified-facts")
    facts = {"request:" + row["name"]: row["value"] for row in request["facts"]}
    results, missing = [], set(closed.pop("declared_inputs"))
    missing -= {key for key, value in facts.items() if not sc._unknown(value)}
    for row in closed["science_records"]:
        record = row["record"]
        if record["kind"] == "ConditionSet":
            declared = {f"{r['source']}:{r['name']}" for r in record["declared_inputs"]}
            assessment = sc.assess_conditions(record, {k: v for k, v in facts.items() if k in declared})
            results.append({"record_ref": row["record_ref"], **assessment})
            if assessment["status"] != "satisfied": limitations.append("condition-" + assessment["status"])
    symbols = [row for row in closed["science_records"] if row["record"]["kind"] == "SymbolBinding"]
    resolutions = []
    for glyph in sorted({row["record"]["original_glyph"] for row in symbols}):
        resolution = sc.resolve_symbols([row["record"] for row in symbols], glyph)
        resolutions.append({"glyph": glyph, "status": resolution["status"], "execution_authorized": False,
                            "candidate_refs": [row["record_ref"] for row in symbols if row["record"]["original_glyph"] == glyph]})
        if resolution["status"] != "resolved": limitations.append("symbol-" + resolution["status"])
    conflicts, gaps, bases = [], [], []
    for entry in scoped:
        package = entry["package"]; catalog = entry["catalog"]
        assurance = copy.deepcopy(catalog.get("policy", {}).get("assurance"))
        bases.append({"package_fingerprint": package["fingerprint"], "legacy_assurance": assurance,
                      "capabilities": copy.deepcopy(package["manifest"].get("capabilities", {})),
                      "science_state": "candidate" if entry["sidecar"] else "unavailable"})
        if assurance:
            warnings.extend(assurance.get("warnings", []))
        for name, output in (("conflicts", conflicts), ("gaps", gaps)):
            for row in package.get(name, []):
                output.append({"package_fingerprint": package["fingerprint"], "sha256": sc.sha256_json(row), "body": copy.deepcopy(row)})
    if conflicts: limitations.append("unresolved-package-conflicts")
    if gaps: limitations.append("known-package-gaps")
    result = {"schema_version": SCIENCE_CONSULTATION_SCHEMA, "protocol": SCIENCE_REFERENCE_PROTOCOL,
              "request_id": request["request_id"], "request_sha256": sc.sha256_json(request),
              "index_sha256": index["index_sha256"], "package_fingerprints": copy.deepcopy(request["source_scope"]),
              "tool_sha256s": cc.tool_hashes(), "behavior": "evidence-candidates" if matches else "abstain",
              "matches": matches, **closed, "condition_results": results, "conflicts": conflicts, "gaps": gaps,
              "symbol_bindings": [row["record_ref"] for row in symbols], "symbol_resolutions": resolutions,
              "required_conditions": [row["record_ref"] for row in closed["science_records"] if row["record"]["kind"] == "ConditionSet"],
              "property_refs": [row["record_ref"] for row in closed["science_records"] if row["record"]["kind"] == "PropertyObservation"],
              "missing_inputs": sorted(missing), "legacy_responses": legacy,
              "assurance": {"base_packages": bases, "knowledge_verified": False, "production_eligible": False,
                            "execution_authorized": False, "semantic_support": "unavailable", "domain_accuracy": "blocked"},
              "limitations": sorted(set(limitations)), "warnings": sorted(set(warnings))}
    # Large package-level disclosures also count toward the byte bound. Fail closed
    # with an explicit truncation summary rather than dropping them silently.
    def size(value):
        return len(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) + 100
    if size(result) > result["limits"]["max_bytes"]:
        omitted_hash = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        count = sum(len(result[k]) for k in ("selected_objects", "science_records", "conflicts", "gaps"))
        for key in ("selected_objects", "science_records", "typed_links", "condition_results", "conflicts", "gaps", "matches", "missing_inputs", "legacy_responses", "symbol_bindings", "symbol_resolutions", "required_conditions", "property_refs"):
            result[key] = []
        result.update(behavior="abstain", closure_status="incomplete", closure_truncated=True,
                      unresolved_dependencies=[{"reason_code": "closure_truncated", "owner_sha256": omitted_hash,
                          "field": "full_bundle_byte_limit", "task": "split-query-and-retrieve-again"}], unresolved_omitted_count=count)
        result["warnings"] = sorted(set(result["warnings"] + ["closure-truncated"]))
        result["limitations"] = sorted(set(result["limitations"] + ["evidence-closure-incomplete", "disclosures-truncated-do-not-answer"]))
    result["bundle_sha256"] = sc.sha256_json(result)
    if len(sc.canonical_json(result)) > result["limits"]["max_bytes"]:
        raise ValueError("science_limit_too_small_for_assurance")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("query", "object", "locator", "source", "validate-answer", "prepare-support-review", "validate-support-review"))
    parser.add_argument("--library", type=Path, required=True)
    for flag in ("index", "request", "reference", "source-map", "source-root", "answer", "bundle", "review-plan", "reviews", "output", "limits"):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--context-chars", type=int, default=0)
    parser.add_argument("--proposer-instance")
    parser.add_argument("--reviewer-instance", action="append")
    parser.add_argument("--review-session-id")
    args = parser.parse_args(argv)
    try:
        library, tokenizer = si.load_library(args.library)
        if args.command in {"object", "locator", "source"}:
            reference = sc.load_json(args.reference)
            if args.command == "object": result = read_object(library, reference)
            elif args.command == "locator": result = read_locator(library, reference)
            else: result = read_source(library, reference, sc.load_json(args.source_map) if args.source_map else None,
                                       args.source_root, context_chars=args.context_chars)
        else:
            index = sc.load_json(args.index); request = sc.load_json(args.request)
            if args.command == "query":
                result = query(request, library, index, tokenizer, limits=sc.load_json(args.limits) if args.limits else None)
            else:
                answer, bundle = sc.load_json(args.answer), sc.load_json(args.bundle)
                inputs = (answer, bundle, request, library, index, tokenizer)
                if args.command == "validate-answer": result = cc.validate_answer(*inputs)
                elif args.command == "prepare-support-review":
                    result = cc.prepare_support_review(*inputs, proposer_instance=args.proposer_instance,
                        reviewer_instances=args.reviewer_instance or [], review_session_id=args.review_session_id)
                else:
                    result = cc.validate_support_reviews(sc.load_json(args.reviews) if args.reviews else [],
                        sc.load_json(args.review_plan) if args.review_plan else None, *inputs)
        if args.output:
            si.check_output(args.output, args.library)
            sw._write_new(args.output, result)
        print(sc.canonical_json(result).decode())
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError, StopIteration):
        # Paths, document text and raw parser errors stay out of diagnostics.
        print(json.dumps({"status": "blocked", "reason_codes": ["science_reference_input_or_output_invalid"],
                          "execution_authorized": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
