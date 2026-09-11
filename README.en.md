# fake-expert

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
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py doctor \
  --profile native-text --source /absolute/path/to/book.pdf --json

PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py plan \
  --source /absolute/path/to/book.pdf --workspace /absolute/path/to/job \
  --profile native-text --name domain-reference \
  --display-name "Domain Reference" --domain domain-name --version 0.1.0 --json

PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/fake_expert.py compile \
  --job /absolute/path/to/job/fake-expert-job.json --progress --json
```

The unified CLI also provides `status`, `resume` and read-only `review list / locate / status / validate-submission` subcommands. Without a complete semantic workpack the flow pauses at `semantic_authoring_required` — the compiler does not author semantic answers.

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

## Repository layout

```
fake-expert/
├── SKILL.md          # Agent-side skill instructions (read this first)
├── agents/           # Agent adapter config
├── assets/schemas/   # All JSON schemas (job/manifest/review/receipt, 140+)
├── references/       # Task-oriented required-reading documents
├── scripts/          # Compiler, workbench and acceptance scripts
└── requirements-*.txt
```

## License

[MIT](./LICENSE). This repository is in a testing stage; publishing here does not certify any book's knowledge, and Reference Drafts remain reference-only, non-decision and non-executable.
