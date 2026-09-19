# Science consultation: implemented M1–M4 interfaces

`1.2.0-science-m4` is a source-free **candidate compiler**, not the formal
v1.2.0 release, a verified knowledge library, or a domain-accuracy certificate.
Use the separate science entrypoints explicitly. Legacy reference abstentions,
job schemas, Review, Gold, promotion, seal, execution and host gates remain intact.
No command below invents sources, independent reviewers, accepted decisions or Gold.

## Data and boundaries

Keep operator-local library JSON, source maps, sidecars, indexes, requests,
snapshots, link proposals, evaluation metadata and receipts outside the compiler
and sealed base packages. New outputs are exclusive files: create their parent
directory first; existing files and symlink paths are rejected. Rebuilding uses a
fresh path. Never patch an old fingerprint to make an old review appear current.

The five M1 record kinds are SymbolBinding, ConditionSet, MaterialState,
PropertyObservation and FormulaCard. Unknown fields remain unknown. Evidence
references bind the exact package/version/object/anchor, not a book title or
matching glyph. A hash match establishes binding, not scientific truth.

## Thin domain configuration

Both shipped JSON files contain original **synthetic software questions**, not
book excerpts, answers or Gold. Heat transfer describes gated scalar use;
materials optics describes reference-only use. Their routing hints are data for
the host, not capabilities or an execution authorization. The existing M3 gate
is still required for every calculation. Complex optical execution is unsupported.

From the project root, export the nested contracts for an operator-local library:

```bash
python3 -B skills/fake-expert/scripts/science_workpack.py domain-config \
  --domain skills/fake-expert/assets/domain-profiles/heat-transfer.json \
  --part profile --output /absolute/private/heat-profile.json
python3 -B skills/fake-expert/scripts/science_workpack.py domain-config \
  --domain skills/fake-expert/assets/domain-profiles/heat-transfer.json \
  --part tokenizer --output /absolute/private/heat-tokenizer.json
```

The exported profile is `tkc.science-profile/v0.1`; the tokenizer is
`tkc.science-tokenizer/v0.1`. `--part routing` exports advisory hints separately.
Do not pass the enclosing domain profile as an M1 profile. In a multi-package
library each package retains its own profile; the operator supplies the combined
tokenizer with unique alias phrases. Aliases only retrieve candidates. They never
equate heat-transfer k with optical extinction k, change case or rewrite an equation.

## Reference and scalar calculation

These are existing commands, not the speculative spellings in the original plan:

```bash
python3 -B skills/fake-expert/scripts/science_index.py \
  --library /absolute/private/library.json --output /absolute/private/index.json
python3 -B skills/fake-expert/scripts/science_reference_runtime.py query \
  --library /absolute/private/library.json --index /absolute/private/index.json \
  --request /absolute/private/request.json --output /absolute/private/bundle.json
```

`science_reference_runtime.py object|locator|source` uses `--library` and
`--reference`. Source reading additionally requires the explicit `--source-map`
and `--source-root`; unavailable original text is reported, not reconstructed.
`validate-answer`, `prepare-support-review` and `validate-support-review` retain
the M2 claim/evidence and independent-support boundary. There is no `get-evidence`
alias, no `science_index.py build`, and no automatic natural-language answer writer.

See [scalar calculation](science-calculation.md) for the actual M3 CLI. It uses
the original qualified scalar AST/runner, explicit quantities/conditions, exact
request review and time-bound host permission. M2 answers still reject calculated
claims; use the separate M3 receipt. Complex numbers, exp/log, symbolic algebra,
tensor computation, PDE/FEM/CFD, extrapolation, automatic uncertainty propagation
and production knowledge publication remain unsupported.

## Cross-source candidates and review

`science_workpack.py links` accepts a closed proposal file with `proposal_id` and
`pairs`: each pair has full left/right package fingerprints and record IDs/hashes,
plus `related|equivalent|contradicts|alias`. Both endpoints must exist in the
supplied library. Different/unknown quantity, sample, temperature, direction,
material state or method context defers comparison. Method references must match
exactly; this version does not infer method equivalence across books.

```bash
python3 -B skills/fake-expert/scripts/science_workpack.py links \
  --library /absolute/private/library.json --proposals /absolute/private/pairs.json \
  --proposer-instance operator-supplied-proposer --created-at 2026-09-19T00:00:00Z \
  --output /absolute/private/links.json
```

Without externally supplied reviewer identities/session the output is paused.
To freeze review inputs also pass two distinct `--reviewer-instance` values and
`--review-session-id`. The original composer schema, attestation validator and
`preserve-separate-no-consensus` policy are reused; high-risk pairs require at
least two reviewers. `validate-links --library ... --proposals ... --links ...
--attestations ...` checks externally supplied fragments. Names alone do not
constitute Review. No accepted link is auto-merged into objects/indexes, no values
are averaged, and validation never creates promotion or execution authority.

## Incremental invalidation

```bash
python3 -B skills/fake-expert/scripts/pipeline_orchestrator.py science-snapshot \
  --library /absolute/private/library.json --tokenizer /absolute/private/tokenizer.json \
  --index /absolute/private/index.json --created-at 2026-09-19T00:00:00Z \
  --output /absolute/private/science-snapshot.json --json
python3 -B skills/fake-expert/scripts/pipeline_orchestrator.py incremental-plan \
  --job /absolute/private/incremental-job.json --old /absolute/private/old-snapshot.json \
  --new /absolute/private/science-snapshot.json --output /absolute/private/dag-comparison \
  --created-at 2026-09-19T00:00:00Z --json
```

Optional repeated `--receipt` arguments import M3 receipts as dependency data,
not as new qualification. The versioned wrapper uses existing DAG node kinds.
Source → base → records/sidecar → review/index/receipts dependencies are explicit.
Tokenizer changes invalidate the index and a **pending query-certification
target**, not source extraction. Profile/semantic changes propagate downstream.
M3 binds the entire sidecar; any same-sidecar edit conservatively invalidates its
receipts. This is not per-record receipt reuse. Existing incremental status/resume
commands still pause at external review/certification gates and never execute
science calculations. Snapshots do not claim a completed host certificate.

## Seven-layer evaluation

`science_acceptance.py` implements `validate-suite`, `blind-plan`, `freeze-review`
and `report`; there is no generic `run` command or internal answer grader.
Use the closed `evaluation_suite` and `evaluation_results` definitions in
`assets/schemas/science-lifecycle.schema.json`. Every case records its layer,
category, development/blind split, origin, source/chapter/formula/sample groups
and evidence references. Hidden answers and executable fields are forbidden.
Development/blind group overlap is rejected; synthetic cases cannot be blind.

```bash
python3 -B skills/fake-expert/scripts/science_acceptance.py report \
  --suite /absolute/private/suite.json --results /absolute/private/results.json \
  --output /absolute/private/evaluation-report.json
```

Reports retain planned/executed/passed/failed/skipped/blocked/not-run counts for
unit, parse, retrieval, support, applicability, execution and host layers, plus
every case status. These are imported observations; their hashes are bindings,
not independently replayed logs. No missing row is removed from the denominator.
Without real source bindings and independent blind review, `domain_accuracy` is
null/blocked (exit 2), even when all synthetic observations pass.

`freeze-review` binds an externally supplied `--gold-payload-sha256`, proposer,
reviewers/session and timestamp into answer-free existing-composer review inputs.
It does not read, generate or attest the hidden Gold payload. Actual external
attestations, a separate evaluator, validated source library and complete blind
results are required for an **externally reported**, per-layer result; the planned
minimum is 100 blind cases with at least 20 in each of five categories. This
interface never certifies accuracy or grants knowledge publication. Real external
blind evaluation has not been performed for this candidate.

## Migration and rollback

M4 changes tool fingerprints. Preserve M1–M3 inputs; explicitly rebuild new
sidecars/indexes/plans, freeze new review inputs, and obtain new external review
and host permission where required. Do not rewrite old evidence, receipts or jobs.
Package only through `package_fake_expert_skill.py --version 1.2.0-science-m4`
into a fresh candidate directory; standalone verification must say
`candidate-verified`. Formal-release certification is a separate requirement.
Rollback disables the new host route and selects the preserved old package and
matching artifacts; it never relabels candidate output as the old formal release.
