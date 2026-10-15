#!/usr/bin/env python3
"""Read-only retrieval runtime for a direct ``fake-*`` Reference Draft.

Standard library only; this file is copied into packages as
``scripts/query_reference.py``.  The catalog builder lives here too so the
index and the query always share one tokenizer.

Retrieval: NFKC/casefold tokens with light English stemming, numbers, and CJK
2/3-grams; field-weighted BM25 over knowledge objects and figure entries; a
coverage gate that abstains on weak partial matches; and an optional
reciprocal-rank fusion with embeddings when the caller supplies the same
explicit ``--embedding-command`` the package was built with.  Responses carry
the fact text, conditions, pages, evidence/figure references, and the
package's candidate assurance warnings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.dont_write_bytecode = True

CATALOG_SCHEMA = "tkc.runtime-catalog/v0.2"
RESPONSE_SCHEMA = "tkc.runtime-response/v0.2"
EMBEDDINGS_SCHEMA = "tkc.runtime-embeddings/v0.1"
RUNTIME_VERSION = "reference-search-bm25-v0.2"
BM25_K1 = 1.2
BM25_B = 0.75
RELATIVE_CUTOFF = 0.3
MIN_COVERAGE_LATIN = 0.5
MIN_COVERAGE_CJK = 0.4
RRF_K = 60
OBJECT_FIELD_WEIGHTS = {
    "title": 3.0,
    "statement": 1.0,
    "formula": 1.5,
    "assumptions": 0.7,
    "valid_when": 0.7,
    "fails_when": 0.7,
}
FIGURE_FIELD_WEIGHTS = {"label": 2.0, "caption": 2.0, "figure_text": 1.0, "ocr_text": 0.5}

STOPWORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "by", "can", "could", "do",
    "does", "for", "from", "give", "how", "i", "in", "into", "is", "it", "its", "me",
    "my", "of", "on", "or", "our", "please", "show", "tell", "that", "the", "their",
    "them", "then", "there", "these", "this", "those", "through", "to", "use", "used",
    "using", "was", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
}
CJK_FUNCTION_WORDS = re.compile(
    r"是什么|为什么|什么|怎么样|怎么|怎样|如何|哪些|哪个|是否|请问|多少|有关|吗|呢|吧"
)
TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[a-z][a-z0-9_]*|[㐀-䶿一-鿿]+")

# Decision/approval/design-for-me intent, not single words: "design heat
# flux" or "safety factor" are lookups, "design a heat exchanger" or "is it
# safe" are decisions this unreviewed package cannot support.
DECISION_PATTERNS = (
    r"\b(approve|approval|certify|certification|authori[sz]e|sign[- ]off)\b",
    r"\b(recommend|recommendation|advise)\b",
    r"\b(should|shall|must) (i|we)\b",
    r"\bis (it|this|that|\w+) (safe|acceptable|compliant)\b",
    r"\bsafe (to|for me|for us|enough)\b",
    r"\b(design|size|specify) (a|an|my|our|me)\b",
    r"\b(make|take) (a|the) decision\b|\bdecide (whether|if|on)\b",
    r"\b(execute|run|perform) (the|this|a|an) (procedure|command|test|operation)\b",
    r"批准|审批|认证|签字|推荐|建议我|应该选|应该用|应该采用|是否安全|安全吗|帮我设计|替我设计|为我设计|直接执行|批准执行|决策",
)
DECISION_RE = re.compile("|".join(DECISION_PATTERNS), re.IGNORECASE)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stem(word: str) -> str:
    """Light, deterministic English stemmer (plurals and common suffixes)."""

    if len(word) <= 3 or not word.isalpha():
        return word
    for suffix, replacement in (("ies", "y"), ("sses", "ss"), ("xes", "x"), ("ches", "ch"), ("shes", "sh")):
        if word.endswith(suffix):
            word = word[: -len(suffix)] + replacement
            break
    else:
        if word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
    for suffix, minimum in (("ing", 3), ("ed", 3), ("ion", 4), ("ive", 4)):
        if word.endswith(suffix) and len(word) - len(suffix) >= minimum:
            word = word[: -len(suffix)]
            break
    if word.endswith("e") and len(word) >= 5:
        word = word[:-1]
    return word


def tokenize(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [token for item in value for token in tokenize(item)]
    if isinstance(value, dict):
        return [token for item in value.values() for token in tokenize(item)]
    if not isinstance(value, str):
        return []
    text = unicodedata.normalize("NFKC", value).casefold()
    text = CJK_FUNCTION_WORDS.sub(" ", text)
    tokens: list[str] = []
    for token in TOKEN_RE.findall(text):
        if token[0].isdigit():
            if len(token) >= 2:
                tokens.append(token)
        elif "a" <= token[0] <= "z":
            if len(token) > 1 and token not in STOPWORDS:
                tokens.append(stem(token))
        elif len(token) == 1:
            tokens.append(token)
        else:
            tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
            tokens.extend(token[index : index + 3] for index in range(len(token) - 2))
    return tokens


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip(" ?？!！.。")


def _is_cjk(token: str) -> bool:
    return bool(token) and "㐀" <= token[0] <= "鿿"


def _weighted_terms(fields: Mapping[str, Any], weights: Mapping[str, float]) -> tuple[dict[str, float], float]:
    terms: dict[str, float] = {}
    for field, weight in weights.items():
        for token in tokenize(fields.get(field)):
            terms[token] = round(terms.get(token, 0.0) + weight, 4)
    return dict(sorted(terms.items())), round(sum(terms.values()), 4)


# --------------------------------------------------------------------------
# Catalog construction (run by the compiler at package build time)
# --------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_catalog(package: Path, policy: Mapping[str, Any]) -> dict[str, Any]:
    package = Path(package)
    manifest = _load_json(package / "manifest.json")
    index = _load_json(package / "references/knowledge/index.json")
    anchors = {row["id"]: row for row in _load_jsonl(package / "references/evidence/anchors.jsonl")}
    figures_path = package / "references/figures/index.json"
    figure_rows = _load_json(figures_path).get("figures", []) if figures_path.is_file() else []
    figure_by_id = {row["id"]: row for row in figure_rows}
    objects = []
    for record in index.get("objects", []):
        obj = _load_json(package / record["path"])
        pages: set[int] = set()
        labels: list[str] = []
        for evidence_id in obj.get("evidence_ids", []):
            anchor = anchors.get(evidence_id, {})
            for page, label in zip(anchor.get("pages", []), anchor.get("printed_page_labels", [])):
                if page not in pages:
                    pages.add(page)
                    labels.append(str(label))
        formula = obj.get("formula") if isinstance(obj.get("formula"), dict) else None
        terms, length = _weighted_terms(
            {
                "title": obj.get("title"),
                "statement": obj.get("statement"),
                "formula": [formula.get(key) for key in ("source_notation", "normalized_notation", "derived_notation") if formula and formula.get(key)] if formula else None,
                "assumptions": obj.get("assumptions"),
                "valid_when": obj.get("valid_when"),
                "fails_when": obj.get("fails_when"),
            },
            OBJECT_FIELD_WEIGHTS,
        )
        warnings = ["figure_value_unreviewed"] if obj.get("value_basis") else []
        entry = {
            "id": obj["id"],
            "kind": "object",
            "type": obj["type"],
            "title": obj["title"],
            "statement": obj["statement"],
            "assumptions": obj.get("assumptions", []),
            "valid_when": obj.get("valid_when", []),
            "fails_when": obj.get("fails_when", []),
            "formula": {key: formula[key] for key in ("source_notation", "normalized_notation", "derived_notation", "representation_relation") if key in formula} if formula else None,
            "physical_pages": sorted(pages),
            "printed_page_labels": labels,
            "evidence_ids": sorted(obj.get("evidence_ids", [])),
            "source_ids": sorted(obj.get("source_ids", [])),
            "figure_refs": sorted(ref for ref in obj.get("figure_refs", []) if not figure_by_id or ref in figure_by_id),
            "value_basis": obj.get("value_basis"),
            "warnings": warnings,
            "path": record["path"],
            "terms": terms,
            "length": length,
        }
        objects.append(entry)
    figures = []
    for row in figure_rows:
        terms, length = _weighted_terms(
            {"label": row.get("label"), "caption": row.get("caption"), "figure_text": row.get("figure_text"), "ocr_text": row.get("ocr_text")},
            FIGURE_FIELD_WEIGHTS,
        )
        figures.append({
            "id": row["id"],
            "kind": "figure",
            "figure_kind": row.get("kind"),
            "label": row.get("label"),
            "caption": row.get("caption"),
            "physical_page": row.get("physical_page"),
            "printed_page_label": row.get("printed_page_label"),
            "image": row.get("image"),
            "object_ids": sorted(row.get("object_ids", [])),
            "terms": terms,
            "length": length,
        })
    conflicts = []
    for conflict in _load_jsonl(package / "references/conflicts.jsonl"):
        terms, length = _weighted_terms({"title": conflict.get("topic")}, {"title": 3.0})
        conflicts.append({
            "id": conflict["id"],
            "title": conflict["topic"],
            "status": conflict["status"],
            "claim_ids": sorted(claim["claim_id"] for claim in conflict.get("claims", []) if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)),
            "terms": terms,
            "length": length,
        })
    gaps = []
    for gap in _load_jsonl(package / "references/knowledge-gaps.jsonl"):
        terms, length = _weighted_terms({"title": gap.get("statement")}, {"title": 1.0})
        gaps.append({"id": gap["id"], "title": gap["statement"], "status": gap["status"], "terms": terms, "length": length})
    modules = manifest.get("modules", [])
    return {
        "schema_version": CATALOG_SCHEMA,
        "runtime_version": RUNTIME_VERSION,
        "package_id": manifest["package"]["id"],
        "module_id": modules[0].get("module_id") if modules and isinstance(modules[0], dict) else None,
        "capabilities": manifest["capabilities"],
        "policy": dict(policy),
        "objects": sorted(objects, key=lambda row: row["id"]),
        "figures": sorted(figures, key=lambda row: row["id"]),
        "conflicts": sorted(conflicts, key=lambda row: row["id"]),
        "gaps": sorted(gaps, key=lambda row: row["id"]),
    }


def embedding_texts(catalog: Mapping[str, Any]) -> list[dict[str, str]]:
    rows = [
        {"id": entry["id"], "text": " ".join([entry["title"], entry["statement"], *entry.get("valid_when", []), *entry.get("fails_when", [])])}
        for entry in catalog.get("objects", [])
    ]
    rows += [
        {"id": entry["id"], "text": " ".join(str(value) for value in (entry.get("label"), entry.get("caption")) if value)}
        for entry in catalog.get("figures", [])
    ]
    return rows


def run_embedding_command(command: str, rows: list[dict[str, str]], *, timeout: int = 600) -> dict[str, list[float]]:
    """Explicit, caller-named embedding command: JSONL {id,text} in, JSONL {id,vector} out."""

    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    completed = subprocess.run(shlex.split(command), input=payload, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise ValueError(f"embedding_command_failed: exit {completed.returncode}")
    vectors: dict[str, list[float]] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        vector = row.get("vector")
        if not isinstance(row.get("id"), str) or not isinstance(vector, list) or not vector or not all(isinstance(value, (int, float)) for value in vector):
            raise ValueError("embedding_output_invalid")
        vectors[row["id"]] = [float(value) for value in vector]
    if set(vectors) != {row["id"] for row in rows} or len({len(value) for value in vectors.values()}) != 1:
        raise ValueError("embedding_output_incomplete")
    return vectors


def build_embeddings(catalog: Mapping[str, Any], command: str) -> dict[str, Any]:
    vectors = run_embedding_command(command, embedding_texts(catalog))
    return {
        "schema_version": EMBEDDINGS_SCHEMA,
        "command_sha256": sha256_text(command),
        "dimensions": len(next(iter(vectors.values()))) if vectors else 0,
        "catalog_entry_ids": sorted(vectors),
        "vectors": {key: [round(value, 6) for value in vectors[key]] for key in sorted(vectors)},
    }


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------


def _bm25_scores(entries: list[Mapping[str, Any]], query_terms: list[str]) -> tuple[list[float], dict[str, float]]:
    count = len(entries)
    if not count:
        return [], {}
    average = sum(float(entry.get("length", 0.0)) for entry in entries) / count or 1.0
    document_frequency = {term: sum(1 for entry in entries if term in entry.get("terms", {})) for term in query_terms}
    idf = {term: math.log(1.0 + (count - df + 0.5) / (df + 0.5)) for term, df in document_frequency.items()}
    scores = []
    for entry in entries:
        terms = entry.get("terms", {})
        length = float(entry.get("length", 0.0)) or 1.0
        score = 0.0
        for term in query_terms:
            frequency = float(terms.get(term, 0.0))
            if frequency:
                score += idf[term] * frequency * (BM25_K1 + 1) / (frequency + BM25_K1 * (1 - BM25_B + BM25_B * length / average))
        scores.append(score)
    return scores, idf


def _covered(entry: Mapping[str, Any], query_terms: list[str]) -> bool:
    """Enough of the query is present, judged per script.

    CJK queries contribute many overlapping n-grams, so Latin and CJK terms are
    measured separately; a mixed-language query passes when either part is
    well covered.
    """

    terms = entry.get("terms", {})
    for group, minimum in (
        ([term for term in query_terms if not _is_cjk(term)], MIN_COVERAGE_LATIN),
        ([term for term in query_terms if _is_cjk(term)], MIN_COVERAGE_CJK),
    ):
        if group and sum(1 for term in group if term in terms) / len(group) >= minimum:
            return True
    return False


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _abstain(reason: str, conflict_ids: Iterable[str] = (), gap_ids: Iterable[str] = (), query_terms: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "schema_version": RESPONSE_SCHEMA,
        "runtime_version": RUNTIME_VERSION,
        "behavior": "abstain",
        "reason_codes": [reason],
        "matched_object_ids": [],
        "evidence_ids": [],
        "conflict_ids": sorted(set(conflict_ids)),
        "gap_ids": sorted(set(gap_ids)),
        "results": [],
        "figures": [],
        "retrieval": {"method": "bm25", "query_terms": sorted(set(query_terms))},
    }


def _figure_result(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "kind": entry.get("figure_kind"),
        "label": entry.get("label"),
        "caption": entry.get("caption"),
        "physical_page": entry.get("physical_page"),
        "printed_page_label": entry.get("printed_page_label"),
        "image": entry.get("image"),
        "object_ids": entry.get("object_ids", []),
    }


def query_catalog(
    catalog: Mapping[str, Any],
    query: str,
    *,
    limit: int = 8,
    embeddings: Mapping[str, Any] | None = None,
    query_vector: list[float] | None = None,
    min_cosine: float = 0.3,
) -> dict[str, Any]:
    if catalog.get("schema_version") != CATALOG_SCHEMA:
        raise ValueError("runtime_catalog_schema_invalid")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("runtime_query_required")
    if not 1 <= limit <= 25:
        raise ValueError("runtime_limit_out_of_range")
    query_terms = sorted(set(tokenize(query)))
    capabilities = catalog.get("capabilities") if isinstance(catalog.get("capabilities"), dict) else {}
    policy = catalog.get("policy") if isinstance(catalog.get("policy"), dict) else {}
    normalized_query = unicodedata.normalize("NFKC", query)
    if capabilities.get("decision_support") is False and DECISION_RE.search(normalized_query):
        return _abstain("decision_capability_unavailable", query_terms=query_terms)
    out_of_scope = {stem(str(term).casefold()) for term in policy.get("out_of_scope_terms", []) if isinstance(term, str)}
    if out_of_scope & set(query_terms):
        return _abstain("outside_package_scope", query_terms=query_terms)

    objects = [row for row in catalog.get("objects", []) if isinstance(row, dict)]
    figures = [row for row in catalog.get("figures", []) if isinstance(row, dict)]
    entries = objects + figures
    scores, idf = _bm25_scores(entries, query_terms)
    lowered = query.casefold()
    exact_query = _plain(query)
    scored = []
    for entry, score in zip(entries, scores):
        title = str(entry.get("title") or entry.get("label") or "")
        if str(entry.get("id", "")).casefold() in lowered or (exact_query and _plain(title) == exact_query):
            score += 1000.0
        title_terms = tokenize(title)
        if len(title_terms) >= 2 and set(title_terms) <= set(query_terms):
            score *= 1.25
        scored.append((score, _covered(entry, query_terms), entry))
    top = max((score for score, _cov, _entry in scored), default=0.0)
    matched_terms = [term for term in query_terms if any(term in entry.get("terms", {}) for entry in entries)]
    rarest = max(matched_terms, key=lambda term: (idf.get(term, 0.0), term)) if matched_terms else None
    lexical = [
        (score, entry)
        for score, coverage, entry in scored
        if score > 0
        and (score >= 1000.0 or coverage)
        and (score >= RELATIVE_CUTOFF * top or (rarest is not None and rarest in entry.get("terms", {})))
    ]
    lexical.sort(key=lambda row: (-row[0], str(row[1]["id"])))
    method = "bm25"
    selected_entries = [entry for _score, entry in lexical]
    semantic_only = False
    if embeddings and query_vector:
        vectors = embeddings.get("vectors", {})
        by_id = {entry["id"]: entry for entry in entries}
        vector_ranked = sorted(
            ((cosine, identifier) for identifier, vector in vectors.items() if identifier in by_id for cosine in [_cosine(query_vector, vector)] if cosine >= min_cosine),
            key=lambda row: (-row[0], row[1]),
        )[: max(limit * 2, 10)]
        fused: dict[str, float] = {}
        for rank, entry in enumerate(selected_entries):
            fused[entry["id"]] = fused.get(entry["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
        for rank, (_cosine_value, identifier) in enumerate(vector_ranked):
            fused[identifier] = fused.get(identifier, 0.0) + 1.0 / (RRF_K + rank + 1)
        semantic_only = bool(fused) and not selected_entries
        selected_entries = [by_id[identifier] for identifier, _value in sorted(fused.items(), key=lambda row: (-row[1], row[0]))]
        method = "bm25+embedding-rrf"

    selected_objects = [entry for entry in selected_entries if entry.get("kind") == "object"][:limit]
    selected_figures = [entry for entry in selected_entries if entry.get("kind") == "figure"][:limit]
    object_by_id = {entry["id"]: entry for entry in objects}
    figure_by_id = {entry["id"]: entry for entry in figures}

    conflict_ids: list[str] = []
    for conflict in catalog.get("conflicts", []):
        claim_ids = [value for value in conflict.get("claim_ids", []) if isinstance(value, str)]
        named = any(value.casefold() in lowered for value in claim_ids)
        claim_hit = any(entry["id"] in claim_ids for entry in selected_objects[:3])
        topic_hit = _covered(conflict, query_terms)
        if named or claim_hit or topic_hit:
            conflict_ids.append(conflict["id"])
            for claim_id in claim_ids:
                if claim_id in object_by_id and object_by_id[claim_id] not in selected_objects:
                    selected_objects.append(object_by_id[claim_id])
    gap_ids = [gap["id"] for gap in catalog.get("gaps", []) if _covered(gap, query_terms)]

    # Figures linked from matched facts are returned with them.
    for entry in selected_objects:
        for figure_id in entry.get("figure_refs", []):
            figure = figure_by_id.get(figure_id)
            if figure is not None and figure not in selected_figures:
                selected_figures.append(figure)
    if not selected_objects:
        response = _abstain("known_package_gap" if gap_ids else "no_supported_reference", conflict_ids, gap_ids, query_terms)
        if selected_figures:
            response["figures"] = [_figure_result(entry) for entry in selected_figures]
            response["reason_codes"].append("figure_candidates_only")
        response["retrieval"]["method"] = method
        return response

    results = []
    for entry in selected_objects:
        results.append({
            "id": entry["id"],
            "type": entry["type"],
            "title": entry["title"],
            "statement": entry["statement"],
            "assumptions": entry.get("assumptions", []),
            "valid_when": entry.get("valid_when", []),
            "fails_when": entry.get("fails_when", []),
            "formula": entry.get("formula"),
            "physical_pages": entry.get("physical_pages", []),
            "printed_page_labels": entry.get("printed_page_labels", []),
            "evidence_ids": entry.get("evidence_ids", []),
            "figure_refs": entry.get("figure_refs", []),
            "value_basis": entry.get("value_basis"),
            "warnings": entry.get("warnings", []),
            "path": entry.get("path"),
        })
    reason_codes = ["supported_reference_found"]
    if conflict_ids:
        reason_codes.append("unresolved_conflict_present")
    if gap_ids:
        reason_codes.append("known_package_gap_present")
    if any(row["warnings"] for row in results):
        reason_codes.append("figure_value_unreviewed")
    if semantic_only:
        reason_codes.append("semantic_match_only")
    return {
        "schema_version": RESPONSE_SCHEMA,
        "runtime_version": RUNTIME_VERSION,
        "behavior": "answer",
        "reason_codes": reason_codes,
        "matched_object_ids": [row["id"] for row in results],
        "evidence_ids": sorted({value for row in results for value in row["evidence_ids"]}),
        "conflict_ids": sorted(set(conflict_ids)),
        "gap_ids": sorted(set(gap_ids)),
        "results": results,
        "figures": [_figure_result(entry) for entry in selected_figures],
        "retrieval": {"method": method, "query_terms": query_terms},
    }


def query_package(
    catalog: Mapping[str, Any],
    query: str,
    *,
    limit: int = 8,
    embeddings: Mapping[str, Any] | None = None,
    embedding_command: str | None = None,
) -> dict[str, Any]:
    """Query and attach the package's bound candidate assurance."""

    policy = catalog.get("policy") if isinstance(catalog.get("policy"), dict) else {}
    assurance = policy.get("assurance")
    if not isinstance(assurance, dict):
        raise ValueError("direct_reference_assurance_missing")
    query_vector = None
    notes: list[str] = []
    if embedding_command:
        if not embeddings:
            notes.append("embedding_index_absent")
        elif embeddings.get("command_sha256") != sha256_text(embedding_command):
            notes.append("embedding_command_mismatch")
        else:
            query_vector = run_embedding_command(embedding_command, [{"id": "query", "text": query}])["query"]
    response = query_catalog(catalog, query, limit=limit, embeddings=embeddings if query_vector else None, query_vector=query_vector)
    if notes:
        response["retrieval"]["notes"] = notes
    result = dict(response)
    result["package_id"] = catalog.get("package_id")
    result["module_id"] = catalog.get("module_id")
    result["assurance"] = assurance
    warnings = assurance.get("warnings")
    result["warnings"] = list(warnings) if isinstance(warnings, list) else []
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--catalog", type=Path, help="Defaults to references/runtime/catalog.json relative to the Skill root.")
    parser.add_argument(
        "--embedding-command",
        help="Optional: the same explicit embedding command the package was built with (JSONL {id,text} in, {id,vector} out).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parent.parent
    catalog_path = args.catalog or root / "references/runtime/catalog.json"
    embeddings_path = catalog_path.parent / "embeddings.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        embeddings = json.loads(embeddings_path.read_text(encoding="utf-8")) if embeddings_path.is_file() else None
        response = query_package(catalog, args.query, limit=args.limit, embeddings=embeddings, embedding_command=args.embedding_command)
    except (OSError, json.JSONDecodeError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
