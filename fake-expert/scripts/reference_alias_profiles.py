"""Compiler-side alias configuration for the existing portable search catalog."""
from __future__ import annotations

import json
import re
import unicodedata

import reference_search as search

PROTOCOL = "reference-alias-profile-v0.1"
_PROTECTED = (
    ("silicon_element", ("silicon", "elemental silicon")),
    ("silicone_polymer", ("silicone", "silicones")),
)


def _phrase_key(phrase):
    return unicodedata.normalize("NFKC", phrase).casefold().replace("-", " ")


def merged_aliases(extra=()):
    rows = [*search.load_term_aliases(),
            *({"term": term, "aliases": list(phrases)} for term, phrases in _PROTECTED),
            *extra]
    terms = set()
    assigned = {}
    if len(rows) > 256:
        raise ValueError("reference_alias_profile_too_large")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"term", "aliases"}
                or not isinstance(row["term"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", row["term"])
                or not isinstance(row["aliases"], list) or not 2 <= len(row["aliases"]) <= 24):
            raise ValueError("reference_alias_profile_invalid")
        term = row["term"]
        if term in terms:
            raise ValueError("reference_alias_profile_conflict")
        terms.add(term)
        for phrase in row["aliases"]:
            if not isinstance(phrase, str) or not 0 < len(phrase) <= 120:
                raise ValueError("reference_alias_profile_invalid")
            key = _phrase_key(phrase)
            if not phrase.isprintable() or not phrase.strip() or (key in assigned and assigned[key] != term):
                raise ValueError("reference_alias_profile_conflict")
            assigned[key] = term
    return rows


def load_extensions(path):
    if path.stat().st_size > 64_000:
        raise ValueError("reference_alias_profile_too_large")
    extra = search.load_term_aliases(path)
    merged_aliases(extra)
    return extra


def saved_extensions(catalog):
    policy = catalog["policy"]
    profile = policy.get("alias_profile")
    if profile is None:
        return []
    if not isinstance(profile, dict) or set(profile) != {"protocol", "extensions", "sha256"}:
        raise ValueError("reference_alias_profile_binding_invalid")
    rows = profile.get("extensions")
    if (profile.get("protocol") != PROTOCOL or not isinstance(rows, list)
            or profile.get("sha256") != search.sha256_text(search.canonical_json(rows).decode("utf-8"))
            or policy.get("term_aliases") != merged_aliases(rows)):
        raise ValueError("reference_alias_profile_binding_invalid")
    return rows


def apply_profile(root, catalog, extra):
    """Reindex frozen catalog fields; the shipped query runtime stays byte-identical."""
    aliases = merged_aliases(extra)
    catalog["policy"].update(term_aliases=aliases, alias_profile={
        "protocol": PROTOCOL,
        "extensions": extra,
        "sha256": search.sha256_text(search.canonical_json(extra).decode("utf-8")),
    })
    for row in catalog["objects"]:
        fields = dict(row)
        formula = row.get("formula") or {}
        fields["formula"] = [formula.get(k) for k in ("source_notation", "normalized_notation", "derived_notation") if formula.get(k)]
        row["terms"], row["length"] = search._weighted_terms(fields, search.OBJECT_FIELD_WEIGHTS, aliases)
    index_path = root / "references/figures/index.json"
    figures = {row["id"]: row for row in json.loads(index_path.read_text())["figures"]} if index_path.is_file() else {}
    for row in catalog["figures"]:
        row["terms"], row["length"] = search._weighted_terms(figures[row["id"]], search.FIGURE_FIELD_WEIGHTS, aliases)
    for key, weight in (("conflicts", 3.0), ("gaps", 1.0)):
        for row in catalog[key]:
            row["terms"], row["length"] = search._weighted_terms(row, {"title": weight}, aliases)
