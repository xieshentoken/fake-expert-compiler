# Authoring, figures, and search (native-text path)

This page covers the parts of the native-text path that turn a PDF into a
queryable `fake-*` Reference Draft: automatic extraction, the explicit
authoring step, figure facts, the optional figure index, and the search
runtime. Everything here stays candidate-only: no output is reviewed, Gold,
ready, decision-capable, or executable.

## 1. End-to-end flow

```bash
python3 scripts/fake_expert.py plan --source /abs/book.pdf --workspace /abs/job \
  --profile native-text --name heat-ref --display-name "Heat Reference" \
  --domain heat-transfer --version 0.1.0 [--figures none|index|index+images] --json
python3 scripts/fake_expert.py compile --job /abs/job/fake-expert-job.json --json
# -> paused: semantic_authoring_required (extraction written to /abs/job/extraction/)
python3 scripts/semantic_authoring.py run --workpack /abs/job/extraction/workpack \
  --command "<model CLI that reads the prompt on stdin>" \
  --proposer-instance my-model-1 --source /abs/book.pdf --json
python3 scripts/fake_expert.py compile --job /abs/job/fake-expert-job.json --json
# -> completed: draft_compiled; package at /abs/job/fake-heat-ref
python3 -B /abs/job/fake-heat-ref/scripts/query_reference.py --query "..."
```

The first `compile` without a bound `--workpack` builds:

- `extraction/ir/`: the PDF intermediate representation;
- `extraction/workpack/`, which holds:
  - units, visual tasks, and visual objects;
  - page renders and `crops/` (one PNG per figure/table candidate);
- `extraction/workpack/authoring/packets/<unit>/{packet.json,prompt.md}`.

`--dry-run` writes nothing and lists these paths. When every unit has a
draft, the next `compile` builds the Draft from that workpack. A bound
`--workpack` keeps the original behaviour.

Extraction now publishes a frozen `extraction-receipt.json` after the IR,
workpack and authoring packets are complete. Resume/status recheck source,
extraction tools, renderer, optional structure selections and initial output
hashes. Drafts and external Review remain editable through their own validators.
Incomplete and failed outputs are retained in `extraction-incomplete-*` or
`extraction-failed-*`; they are never treated as a complete checkpoint.

A native document without detectable headings pauses as `structure_review_required`.
An operator may select real source-text boundaries and plan a fresh job with
`--structure-selections /abs/boundaries.json`. The JSON has exactly
`schema_version=tkc.native-structure-selections/v0.1`, `source_sha256`, and
`boundaries`; each boundary has `page`, `start`, `end`, `span_sha256` over the
normalized native page text. Boundaries must be ordered, nonempty and source-bound;
the selected text is retained as an explicit operator boundary, never an inferred
chapter. Ambiguous locators pause. This is not independent Review.

## 2. Authoring step (`semantic_authoring.py`)

The compiler never selects or calls a model. `run` executes the command you
name, once per unit:

- the prompt goes to stdin;
- `FAKE_EXPERT_PACKET`, `FAKE_EXPERT_PROMPT`, and `FAKE_EXPERT_UNIT_ID` are set
  in the environment;
- stdout must contain one response JSON (a fenced block or a CLI `{"result": ...}`
  wrapper is accepted).

Receipts record the command hash, prompt/packet bindings, exit code, and response hash. Nonzero exit, timeout, malformed JSON or invalid response shape returns failure. Explicit reruns preserve prior responses in `response-history/`; existing drafts require `--replace-drafts` before replacement. You can also
write responses yourself into `authoring/responses/<unit_id>.json` and run
`ingest`.

Response format (`tkc.semantic-authoring-response/v0.1`):

| Field | Meaning |
|---|---|
| `disposition` | `promote`, `context-only`, `non-knowledge` or `reject` |
| `objects[].type` | Definition, Concept, Claim, Equation, Assumption, Exception, Method, Procedure, DecisionRule, **Figure, Table, DataPoint** |
| `objects[].statement` | Paraphrase. Twenty or more copied source words are rejected later by the builder |
| `objects[].evidence` | Exact quotes from the unit's FOCUS TEXT |
| `objects[].figures` | Figure/table `vo-…` IDs from the packet, when the statement was read from an image |
| `objects[].formula` | `{source_notation, normalized_notation}` for equations |
| `relations[]`, `gaps[]` | Local-id relations; gaps with evidence quotes |

`ingest` resolves each quote to an exact `[start,end)` span plus SHA-256. The
match tolerates whitespace, dash/quote variants, and CJK line-break spaces.
It then writes canonical, quote-free `tkc.semantic-draft/v0.1` drafts and
`authoring/ingest-report.json`.

Problems drop the proposal and are reported; they do not fail the source:

| Code | Meaning |
|---|---|
| `evidence_quote_unresolved` | A quote is not in the focus text (other quotes may still support the object) |
| `object_dropped_no_evidence` | No quote resolved |
| `object_pending_numeric_evidence` | Untraced numeric proposal retained in `authoring/pending-numeric/`, outside answerable objects |
| `object_dropped_figure_missing` | Figure/Table/DataPoint without `figures` |
| `object_dropped_copies_source` | Title/statement copies 21+ source words (24 CJK characters) |
| `figure_not_on_evidence_page` | Named figure is not on an evidence page; binding removed |
| `equation_downgraded_to_claim` | No equation visual task on the evidence page, or no formula; kept as Claim |

A unit with no surviving proposal is written as `reject`, so coverage stays
explicit.

## 3. Figures

Detection:

- Pages are routed for visual review when any of these is true:
  - they carry raster images, including inline images and images nested in
    Form XObjects;
  - they have vector graphics (Form XObjects, or 12 or more path-paint operators);
  - their text mentions a figure, table, or equation label in English or
    Chinese (`Figure 3`, `Fig. 1.2`, `图1.4`, `表 2-1`, `(1.1)`, `（2-3）`).
- Body references such as `Figure 1.2 shows …` or `图1.4给出了 …` are not captions.
- Vector figures take the cluster of drawings directly above their caption. Uncaptioned image/vector regions may form bounded candidates; ambiguous or unlocalised graphics create explicit conflicts. Decorations are not promoted to page-sized figures. Lowercase caption titles and up to two short aligned continuation lines are supported.

Content (candidate-only, never independent review evidence):

Crops use `figure-crop-mediabox-v0.2`: canonical MediaBox-local coordinates, intrinsic page rotation, pixel-edge rounding and clipped padding. CropBox offsets do not shift Poppler's default MediaBox viewport. Regenerate crops and independent Review after geometry changes.

| Source | Where |
|---|---|
| Caption text | visual object attribute `caption_text_candidate` |
| Native text inside the figure (axis labels, Form-XObject text) | `figure_text_candidates` |
| Crop image | `workpack/crops/<vo-id>.png`, rendered by `pdftoppm -x -y -W -H` |
| OCR of raster / text-less figures (opt-in) | `workpack/figure-ocr.jsonl` |

OCR runs only with `semantic_workpack.py build --figure-ocr tesseract
[--tesseract PATH --figure-ocr-lang eng+chi_sim]`. Missing engines fail closed
(`figure_ocr_backend_missing`); there is no fallback. OCR lives outside visual
objects, so it never changes visual object hashes.

Figure facts (numeric-evidence-v0.1):

- `figure_object_ids` must name figure/table candidates belonging to the object's visual tasks and evidence page.
- Numbers in returned titles, statements, conditions and formula notations need text evidence. A `compiler-derived` label is not a derivation receipt.
- Untraced values without a figure are retained in private `authoring/pending-numeric/<unit>.json` records and the ingest report, outside the answerable draft.
- Untraced values with a figure remain in the proposal draft with `numeric_review_required`. `compile` pauses and the direct builder refuses to create a queryable package.
- Use the existing `semantic_workpack.py prepare-review`, external semantic/visual fragment, `merge-review`, and reviewed validation routes. Two independent semantic reviewers and two visual reviewers must accept the exact bound values. The compiler never authors their conclusions.
- Accepted figure values carry `value_basis.source=figure-reviewed`, the numeric protocol and a review-binding hash. This does not verify the whole package or grant promotion/execution.
- Older packages remain readable under their original runtimes. Upgrading old numeric facts requires replay against the matching native source; untraced values or unavailable source evidence pause migration. Rebuild from a reviewed workpack to migrate those facts.

New native-text `plan` jobs and `direct_reference_skill.py build` CLI calls default to `index+images`. The direct Python API and legacy `extend` flags retain their explicit option behavior. Existing jobs without `package.figures` stay text-only; replan into a fresh job/version to change the selection. A requested image with a missing crop fails `figure_crop_unavailable`; the compiler does not silently create an imageless package.

Build options (`plan --figures` or `direct_reference_skill.py build --figures`; legacy figure flags remain available):

| Option | Effect |
|---|---|
| `--figures none` | Pure-text source-free Draft, with no figure index or source image assets |
| `--figure-index` / `--figures index` | `references/figures/index.json`: label, kind, page, bbox, caption, in-figure text, OCR lines, linked object IDs |
| `--figure-crops` / `--figures index+images` | Also copies crops to `assets/figures/<vo-id>.png` |

Both are source-derived. The manifest declares
`distribution.source_derived_content`, and `package_skill_release.py` /
`verify_skill_release.py` refuse such packages
(`candidate_source_derived_content_not_distributable`). Use them for private,
local Skills only.

## 4. Search runtime (`reference-search-bm25-v0.3`)

New packages ship `scripts/query_reference.py`, a copy of
`scripts/reference_search.py` (stdlib only).

Tokens:

The compiler freezes the versioned `assets/reference-terms.json` term aliases in each catalog policy. This controlled bilingual vocabulary changes retrieval only, never facts or evidence. Unknown properties abstain; this is not general translation. The alias file and runtime have explicit SHA-256 pins.

- NFKC + casefold Latin words with light stemming (plurals, -ing/-ed/-ion/-ive);
- numbers;
- CJK 2-grams and 3-grams, with question words such as 什么, 如何, and 吗 removed.

Ranking:

- Field-weighted BM25 over facts and figure entries (title ×3, formula ×1.5,
  statement ×1, conditions ×0.7).
- Field-weighted BM25 retains the Latin/CJK coverage gate. With controlled aliases, every requested known concept must match and each remaining script must meet its coverage threshold. Requested design/rated/critical/minimum/maximum qualifiers must also be supported; background laws do not supply missing design values.
- Entries scoring below 30% of the top score are cut, unless they contain the
  query's rarest matched term.

Each result carries:

- statement, conditions, and formula;
- physical pages and printed page labels;
- evidence and figure IDs;
- warnings.

Related indexed or explicitly linked figures are returned in `figures`, with `image` relative to the generated Skill directory. Resolve it against that directory, inspect the image, then show it inline using an absolute local Markdown image path and its label, caption, physical page and ID. Do not invent a fact-to-image link from page co-occurrence. No related image means an empty image result; source-free packages still work. For figure-only queries, `figure_candidates_only` permits showing the returned source image while `behavior=abstain` still applies to unsupported factual claims. Showing a candidate image does not approve its numeric values or run Review/Gold.

The decision guard matches intent, not single words. "design a heat
exchanger", "is it safe", "should we", "recommend", "批准", and "推荐" abstain.
"design heat flux" and "safety factor in Table 2" answer.

Optional hybrid retrieval: build with `--embedding-command "<cmd>"`, where the
command reads JSONL `{id,text}` and prints JSONL `{id,vector}`, and query with the
same `--embedding-command`. Lexical and vector ranks are fused (RRF). A
different command is ignored with the note `embedding_command_mismatch`.
Packages stay offline unless you pass the command.

## 5. Compatibility

- `1.1.0-direct-reference` and Opus `1.3.0-direct-reference` packages retain their pinned runtimes and compatibility validation. New builds use `1.4.0-direct-reference` / runtime catalog and response v0.3. Upgrades enforce numeric revalidation, and unsupported legacy values pause rather than being grandfathered into the new generation.
- `portable_reference_runtime.py` and `direct_reference_runtime.py` are
  unchanged.
- After editing `reference_search.py` or the controlled term aliases, update `REFERENCE_SEARCH_RUNTIME_SHA256` in
  both `compiler_version.py` and `verify_skill_release.py`;
  `tests/test_end_to_end.py` checks this.

## Native equation normalization evidence

Numeric matching v0.2 treats complete number tokens, scalar values, powers and equation labels separately. A flattened `L2` or `L 2`, an unrelated `(2)`, or an origin/boolean cannot justify `L**2` or a coefficient. Literal `L²`, `L^2` and `L**2` are matching views only; canonical source offsets and hashes never change.

With an explicit native source, ingestion can bind one narrowly supported fraction: a symbolic numerator product over a raised integer power. `formula.notation_symbols` declares spelling correspondences such as `{"Fo":"t*","alpha":"α"}`. The compiler checks the native label, equals sign, numerator/denominator placement, smaller raised glyph and painted fraction rule against that exact notation. The generated `formula.native_numeric_evidence` binds the source, committed spans, source/normalized notation, geometry/rule/glyph hashes, tool hashes and a real Poppler render. A model-supplied receipt is discarded and recomputed. Unsupported layouts stay pending.

Workpack validation, materialization and `verify_source_anchors.py` replay the binding. Changes to source, spans, notation, tools, renderer or receipt invalidate it; no fallback silently repairs an accepted Draft. Packaged receipts are source-free candidate provenance; archive verification without the original PDF proves integrity only. This route does not execute formulas, derive arbitrary values, perform independent Review/Gold, or authorize numbers read only from figures.

Native caption routing also rejects panel references followed by prose verbs and wrapped `Consider / See / Refer to` references. Actual standalone labels and panel titles remain candidates. Open-sided tables with native top/header/closing rules use those rules and bounded native header/body rows (`native-horizontal-table-rules-v0.1`); a missing closing rule stays unsupported. Header rules stop caption continuation into table columns. No fixed page-height percentage supplies a table's closing boundary.
