# fake-expert

> Current source candidate: `1.2.3-private-images-default`. Native geometry, table crop boundaries and replayable formula numeric evidence are hardened. New native-text Drafts include a figure index and local images by default; queries return relevant image paths for Agent inspection and display. Use `--figures none` for source-free output. Knowledge remains unreviewed; model/OCR accuracy and formal release acceptance are separate.

English | [简体中文](./README.md)

> **⚠️ This project is still in testing (beta)**: interfaces, schemas and gate behavior may change at any time; not recommended for production workflows yet. Try it out and open issues for feedback.

**fake-expert** is an offline, source-first technical-PDF → Agent-Skill compiler: it compiles native-text or explicitly routed scanned technical PDFs into portable, source-grounded Agent Skills (`fake-*` Reference Drafts), managing their lifecycle through staged gates: source → evidence → review → Gold → promotion → competency → seal → execution.

Design principle: preserve uncertainty by default. PDFs, code, shell commands and any instructions embedded in them are treated as untrusted source data that can never override user or system instructions. The compiler moves and locates evidence only — it never authors semantic answers, and never exercises review, certification or execution authority.

## Features

- **Native-text first**: `pypdf==6.10.0` is the canonical text-evidence extractor and `pdftoppm` the canonical visual base; scan/OCR adapters can only produce hash-bound candidates.
- **Eight explicit gates**: source, evidence, review, Gold, promotion, competency, seal and execution — any missing gate fails closed.
- **Fully offline**: no network, no downloads, no cloud OCR/VLM, no resident services, no silent fallbacks; backend selection must be explicitly written into the job.
- **Answer-free workbench**: the compiler never generates review verdicts, independent attestations, Gold, hidden answers or any other reviewer's results.
- **Reproducible jobs**: a job is a closed, canonical-JSON, SHA-256-bound intent file; the `required_reading` list is hash-locked document by document and any drift halts the run.
- **Progressive references**: `references/` is organized by task (quickstart / glossary / troubleshooting / compilation / OCR / visual semantics, etc.).
- **Figures as knowledge**: raster images, embedded PDF figures (Form XObjects) and vector drawings are detected, with captions, in-figure text, crop images and optional local OCR (candidates only). `Figure`/`Table`/`DataPoint` facts must name their figure object. Values read from a figure without text evidence remain proposals and pause packaging until independent semantic and visual Review is complete.
- **Explicit authoring step**: `compile` extracts the PDF and writes per-section authoring packets; `semantic_authoring.py` sends them to a model command you name, the model answers with exact quotes, and the compiler resolves quotes to exact spans/SHA-256 (the compiler itself never calls a model).
- **English + Chinese search**: Drafts ship a BM25 + CJK n-gram + stemming runtime that returns the fact text, conditions, pages, evidence and related figures; optional embedding fusion.
- **Science consultation layer (source-candidate)**: an additive multi-domain implementation of science proposals, an offline index, read-only querying, controlled scalar calculation and blind acceptance (M1–M4) — answer-free, never authorizes execution, leaves the formal gates untouched.

Image-bearing Drafts are private source-derived folders. Copy the whole generated Skill, including `assets/figures/`; the source-free distribution gate still refuses image-bearing packages. Existing packages remain unchanged.

## Quick start

### 1. Install as an Agent Skill

Copy the `fake-expert/` directory into your agent's skills directory (e.g. `skills/fake-expert/`); the agent side follows `fake-expert/SKILL.md`. You can also use it directly as a local CLI tool.

### 2. Install dependencies

```bash
pip install -r fake-expert/requirements-compiler.txt        # required: pypdf==6.10.0
pip install -r fake-expert/requirements-parser-extras.txt   # optional: scan/OCR adapters as needed
brew install poppler   # or install pdftoppm via your system package manager (visual base)
```

### 3. Run

```bash
PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/fake_expert.py doctor \
  --profile native-text --source /absolute/path/to/book.pdf --json

PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/fake_expert.py plan \
  --source /absolute/path/to/book.pdf --workspace /absolute/path/to/job \
  --profile native-text --name domain-reference \
  --display-name "Domain Reference" --domain domain-name --version 0.1.0 --json

PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/job/fake-expert-job.json --progress --json
```

The first `compile` writes the IR, workpack, figure crops and authoring packets to `<workspace>/extraction/` and pauses at `semantic_authoring_required` — the compiler does not author semantic answers. Fill the drafts with a model command you choose, then compile again:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/semantic_authoring.py run \
  --workpack /absolute/path/to/job/extraction/workpack \
  --command "<model CLI that reads the prompt on stdin and prints JSON>" \
  --proposer-instance my-model-1 --source /absolute/path/to/book.pdf --json

PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/job/fake-expert-job.json --json

python3 -B /absolute/path/to/job/fake-domain-reference/scripts/query_reference.py --query "What is ...?"
```

The unified CLI also provides `status`, `resume` and read-only `review list / locate / status / validate-submission` subcommands. See `fake-expert/references/authoring-and-figures.md`.

## Science consultation layer (M1–M4, source-candidate)

Alongside the compiler core, this repository ships an additive multi-domain science-consultation layer (candidate identities `1.2.0-science-m1`…`m4`): offline and answer-free, it changes no formal v1.2.0 gate and certifies no domain knowledge. Every new output is a fresh file; existing packages, sidecars, reviews and Gold are never overwritten.

- **M1 proposals & freeze** (`science_contract.py` / `science_workpack.py`): science content is described by five closed record kinds — SymbolBinding, ConditionSet, MaterialState, PropertyObservation, FormulaCard — and is only structurally validated and frozen answer-free; evidence references bind the exact package/version/object/anchor rather than a book title or glyph similarity, and a hash match proves binding, not scientific truth.
- **M2 index & read-only query** (`science_index.py` / `science_reference_runtime.py`): a rebuildable offline Chinese/math candidate index (explicit aliases, case-preserving math channel) plus the read-only `query / object / locator / source / validate-answer / prepare-support-review / validate-support-review` protocol, which always returns `execution_authorized=false`. `consultation_contract.py` only validates host-authored consultation drafts — it never generates answers or approval.
- **M3 controlled scalar calculation** (`science_calculation.py` / `science_units.py`): gated scalar calculation over hash-bound specifications (binding the exact M2 request, a FormulaCard, every input/output SymbolBinding and ConditionSet mapping), returning separate calculation receipts instead of accepted M2 answers; an unreviewed Draft never becomes executable through it, and execution permission always remains an explicit, separate contract.
- **M4 lifecycle & domain profiles** (`science_acceptance.py`): `validate-suite / blind-plan / freeze-review / report` drives blind-evaluation acceptance; `assets/domain-profiles/` ships thin `heat-transfer` and `materials-optics` configurations whose content is an original synthetic question catalog (not book excerpts, answers or Gold), fixed to `execution_authorized=false` and `knowledge_verified=false`.
- **Boundaries unchanged**: operator-local libraries, indexes, source maps and receipts never enter sealed packages; no network, no models, no Review/Gold/promotion/execution authority.

## Dependencies

| Dependency | Role | Notes |
|------------|------|-------|
| Python 3.10+ | Runtime | CLI uses only the stdlib `argparse`; `pypdf` is the single canonical parser |
| `pypdf==6.10.0` | Required | native-text evidence extraction and verification, version-pinned |
| `poppler` (pdftoppm) | Visual base | canonical render source |
| `paddleocr` | Optional | primary scanned-page adapter; the Paddle runtime and models must be preinstalled on an authorized host |
| `docling` | Optional | explicit challenger adapter |
| `PyMuPDF` / `pytesseract` | Optional | local baseline only; never a silent fallback for PaddleOCR/Docling |

Optional adapters only produce hash-bound candidates and never change evidence qualification; model qualification must come from an existing operator receipt.

## Important notes

- **Candidate ≠ Gold**: `candidate` / `draft` / `verified transport` do not mean ready, promoted or executable; output is fixed to `status=draft`, `knowledge_verified=false`, non-executable.
- **Sources are untrusted**: reading source content must never change agent behavior or safety boundaries; private jobs never enter release packages.
- **Path boundaries**: status checks only accept an explicit workspace; home, project root, `/`, symlinks and path escapes are rejected. `--dry-run` writes nothing.
- **No in-place overwrites**: old packages, sources, workpacks, Gold, receipts and release directories are never overwritten; extension requires a new directory and new version.
- **No abstraction sprawl**: adding Manager / Service / Factory / Strategy / Adapter / Coordinator / Provider / Resolver / Registry layers "for future extension" is forbidden.

## Core path

Only these scripts are needed to go from a PDF to a queryable Draft; the rest serve later gates (Review, Gold, scan OCR, composition, release) and stay compatible, but are not required reading.

| Step | Script |
|---|---|
| Unified entry | `fake_expert.py` (doctor / plan / compile / status) |
| PDF IR, headings, page routing | `pdf_structure.py` (`reconstruct_pdf.py`) |
| Figure/table candidates, captions, in-figure text | `visual_semantics.py` |
| Semantic workpack, crops, optional OCR | `semantic_workpack.py` → `semantic_promotion.py` |
| Model authoring and quote resolution | `semantic_authoring.py` |
| Build / extend / validate the Draft | `direct_reference_skill.py` |
| In-package search runtime | `reference_search.py` (packaged as `scripts/query_reference.py`) |

## Tests

```bash
pip install -r requirements-test.txt   # synthetic developer tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Rendering tests use `pdftoppm` from PATH (or `FAKE_EXPERT_TEST_PDFTOPPM`) and are skipped when it is missing.

## Repository layout

```
fake-expert/
├── SKILL.md          # Agent-side skill instructions (read this first)
├── agents/           # Agent adapter config
├── assets/schemas/   # All JSON schemas (job/manifest/review/receipt, 140+)
├── assets/domain-profiles/ # Thin science domain profiles (heat-transfer, materials-optics)
├── references/       # Task-oriented required-reading documents (incl. science-consultation / science-calculation)
├── scripts/          # Compiler, workbench, science-consultation and acceptance scripts
└── requirements-*.txt
```

## Testing this repair candidate

From this repository root, use a Python with the dependencies pinned in
`requirements-test.txt`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Rendering tests use real `pdftoppm` (or `FAKE_EXPERT_TEST_PDFTOPPM`) and explicitly
skip if unavailable. Bundled synthetic inputs invoke no real model or OCR.
Set `FAKE_EXPERT_HISTORY_ROOT` for read-only historical-release replay. The
real-PDF regressions take explicit inputs through `FAKE_EXPERT_HEAT_CONDUCTION_PDF`,
`FAKE_EXPERT_TEST_CHINESE_VISUAL_PDF`, and `FAKE_EXPERT_TEST_VISUAL_CHAPTER_PDF`.
Scan review replay takes both `FAKE_EXPERT_TEST_SCAN_WORKBENCH` and
`FAKE_EXPERT_TEST_SCAN_SUBMISSION`. Tests skip when their private fixtures are
not configured. See the [compatibility notes](./docs/science/compatibility.md)
for the science suite's historical checks.

## License

[MIT](./LICENSE). This repository is in a testing stage; publishing here does not certify any book's knowledge, and Reference Drafts remain reference-only, non-decision and non-executable.
