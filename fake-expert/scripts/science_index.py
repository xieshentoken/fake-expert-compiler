#!/usr/bin/env python3
"""Rebuildable offline natural-language/math candidate index; no answer generation."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import unicodedata

import science_contract as sc
import science_workpack as sw
import consultation_contract as cc
from compiler_version import SCIENCE_INDEX_SCHEMA, SCIENCE_REFERENCE_PROTOCOL, SCIENCE_REFERENCE_COMPILER_VERSION


def object_ref(package, obj):
    fp = package["fingerprint"]
    return {"package_id": fp["package_id"], "package_version": fp["package_version"],
            "object_id": obj["id"], "object_sha256": sc.sha256_json(obj)}


def evidence_refs(package, obj):
    reference = object_ref(package, obj)
    return [{"object_ref": reference, "evidence_id": anchor["id"], "evidence_sha256": sc.sha256_json(anchor)}
            for anchor in package["anchors"] if anchor["id"] in obj["evidence_ids"] and obj["id"] in anchor["supports"]]


def load_library(path):
    config = sc.load_json(path)
    cc.shape(config, "library")
    library = []
    for row in config["packages"]:
        # Paths are operator-local inputs; none enter the portable index or bundle.
        root = Path(row["package"])
        if not root.is_absolute() or not Path(row["profile"]).is_absolute() or (
                row["sidecar"] is not None and not Path(row["sidecar"]).is_absolute()):
            raise ValueError("science_library_absolute_operator_paths_required")
        package = sw.load_package(root)
        profile = sc.load_json(Path(row["profile"]))
        sidecar = sc.load_json(Path(row["sidecar"])) if row["sidecar"] is not None else None
        sc.validate_sidecar(sidecar, package, profile)
        def jsonl(relative):
            file = sw._safe_path(root, relative)
            if file.stat().st_size > sc.MAX_BYTES:
                raise ValueError("science_library_row_limit")
            rows = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]
            sc.canonical_json(rows)
            return rows
        # Every file below has already passed the original package's inventory gate.
        package["relations"] = jsonl("references/knowledge/relations.jsonl")
        package["conflicts"] = jsonl("references/conflicts.jsonl")
        package["gaps"] = jsonl("references/knowledge-gaps.jsonl")
        catalog = sc.load_json(sw._safe_path(root, "references/runtime/catalog.json"))
        library.append({"package": package, "sidecar": sidecar, "profile": profile, "catalog": catalog})
    validate_library(library)
    return library, config["tokenizer"]


def validate_library(library):
    if not isinstance(library, list) or not 1 <= len(library) <= 16:
        raise ValueError("science_library_size_invalid")
    seen = set()
    for entry in library:
        package = entry["package"]; fp = package["fingerprint"]
        cc.shape(fp, "fingerprint")
        key = (fp["package_id"], fp["package_version"])
        if key in seen:
            raise ValueError("science_library_duplicate_package_version")
        seen.add(key)
        sc.validate_shape(entry["profile"], "profile")
        sc.validate_sidecar(entry["sidecar"], package, entry["profile"])


def tokens(text, tokenizer):
    cc.shape(tokenizer, "tokenizer")
    if len({r["phrase"] for r in tokenizer["aliases"]}) != len(tokenizer["aliases"]):
        raise ValueError("science_duplicate_alias")
    # NFC intentionally does not fold mathematical bold or subscript characters.
    text = unicodedata.normalize("NFC", text)
    def natural(value):
        result = set(re.findall(r"[A-Za-z][A-Za-z0-9-]+", value.casefold()))
        for run in re.findall(r"[\u3400-\u9fff]+", value):
            for size in (2, 3):
                result.update(run[i:i + size] for i in range(len(run) - size + 1))
        return result
    words = natural(text)
    # Preserve TeX presentation commands, case, primes, Greek and Unicode math glyphs.
    math = set(re.findall(r"\\(?:mathbf|boldsymbol|vec)\{[^{}\s]{1,64}\}|\\[A-Za-z]+|(?<!\w)(?:[A-Za-z]|[\u0370-\u03ff]|[\U0001d400-\U0001d7ff])(?:_\{[A-Za-z0-9,+-]{1,64}\}|_[A-Za-z0-9]+|[₀-₉]|[\u0300-\u036f\u20d0-\u20ff])*['′″]*(?!\w)", text))
    for row in tokenizer["aliases"]:
        phrase = unicodedata.normalize("NFC", row["phrase"]).casefold()
        haystack = text.casefold()
        matched = (phrase in haystack if re.search(r"[\u3400-\u9fff]", phrase)
                   else re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", haystack) is not None)
        if matched:
            for term in row["terms"]:
                words.update(natural(term))
    return {"natural": sorted(words), "math": sorted(math)}


def build_index(library, tokenizer):
    validate_library(library)
    cc.shape(tokenizer, "tokenizer")
    packages, entries = [], []
    for item in sorted(library, key=lambda e: sc.sha256_json(e["package"]["fingerprint"])):
        package, sidecar, profile = item["package"], item["sidecar"], item["profile"]
        packages.append({"fingerprint": package["fingerprint"], "sidecar_sha256": sidecar["sidecar_sha256"] if sidecar else None,
                         "profile_sha256": sc.sha256_json(profile)})
        records = sidecar["records"] if sidecar else []
        for obj in sorted(package["objects"], key=lambda o: o["id"]):
            reference = object_ref(package, obj)
            attached = [r for r in records if r["source_object_ref"] == reference]
            text = " ".join(str(obj.get(k, "")) for k in ("id", "title", "statement", "description"))
            for record in attached:
                text += " " + " ".join(str(record.get(k, "")) for k in
                    ("original_glyph", "display_latex", "canonical_quantity_id", "quantity_kind", "property_quantity_id"))
                text += " " + " ".join(row["latex"] for row in record.get("notation_refs", []))
            terms = tokens(text, tokenizer)
            entries.append({"object_ref": reference, "profile_id": profile["profile_id"],
                            "natural_tokens": terms["natural"], "math_tokens": terms["math"],
                            "quantity_kinds": sorted({r["quantity_kind"] for r in attached if isinstance(r.get("quantity_kind"), str)}),
                            "record_ids": sorted(r["id"] for r in attached)})
    result = {"schema_version": SCIENCE_INDEX_SCHEMA, "protocol": SCIENCE_REFERENCE_PROTOCOL,
              "compiler_version": SCIENCE_REFERENCE_COMPILER_VERSION, "packages": packages,
              "tokenizer_sha256": sc.sha256_json(tokenizer), "tool_sha256s": cc.tool_hashes(),
              "entries": entries, "score_meaning": "candidate-recall-only-not-confidence", "execution_authorized": False}
    result["index_sha256"] = sc.sha256_json(result)
    return copy.deepcopy(result)


def validate_index(index, library, tokenizer):
    if index != build_index(library, tokenizer):
        raise ValueError("science_index_stale_or_modified")


def search(index, query, tokenizer, *, scope, domains=None, quantity_kind=None, limit=8):
    if not isinstance(query, str) or not 0 < len(query) <= 2048 or type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("science_query_limit_invalid")
    if index["tokenizer_sha256"] != sc.sha256_json(tokenizer):
        raise ValueError("science_tokenizer_drift")
    query_terms = tokens(query, tokenizer)
    scoped = {(fp["package_id"], fp["package_version"]) for fp in scope}
    rows = []
    for entry in index["entries"]:
        ref = entry["object_ref"]
        if (ref["package_id"], ref["package_version"]) not in scoped:
            continue
        if domains and entry["profile_id"] not in domains:
            continue
        if quantity_kind and quantity_kind not in entry["quantity_kinds"]:
            continue
        natural = sorted(set(query_terms["natural"]) & set(entry["natural_tokens"]))
        math = sorted(set(query_terms["math"]) & set(entry["math_tokens"]))
        exact = query == ref["object_id"]
        score = len(natural) + 4 * len(math) + 100 * exact
        if score:
            rows.append({"object_ref": copy.deepcopy(ref), "score": score,
                         "matched_natural": natural, "matched_math": math})
    return sorted(rows, key=lambda r: (-r["score"], sc.sha256_json(r["object_ref"])))[:limit]


def check_output(path, config_path):
    config = sc.load_json(config_path); cc.shape(config, "library")
    if any(path.resolve().is_relative_to(Path(r["package"]).resolve()) for r in config["packages"]):
        raise ValueError("science_output_inside_base_forbidden")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        library, tokenizer = load_library(args.library)
        index = build_index(library, tokenizer)
        check_output(args.output, args.library)
        sw._write_new(args.output, index)
        print(json.dumps({"status": "index-built", "index_sha256": index["index_sha256"], "object_count": len(index["entries"]),
                          "execution_authorized": False}))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "blocked", "reason_codes": ["science_index_input_or_output_invalid"]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
