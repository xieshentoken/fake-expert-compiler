# Controlled scalar calculation (M3 source candidate)

`scripts/science_calculation.py` implements `science-calculation-v0.1` under
`1.2.0-science-m3`. It is an additive, offline source entry point. It does not
change a sealed package, issue Review/Gold, promote knowledge, package a release,
install dependencies, or contact a model. M2 reference requests/answers keep their
closed protocol; return an M3 receipt separately, not as an accepted M2 answer.

## Required inputs and AND gates

The original package must pass `science_workpack.load_package`: full sealed file
inventory plus the existing publish/semantic/AST/independent execution-test gates.
It must already be ready with reference/executable true and decision_support false.
M3 additionally requires the canonical generated module and sandbox runner bytes.
It cannot make an unreviewed Draft executable.

The package, science sidecar and profile are read only. `specification` binds their
versions/hashes, the exact M2 request hash, a FormulaCard, every input/output
SymbolBinding, request fact or PropertyObservation selection, and ConditionSet
input mappings. Quantities, scope, frames, definitions and numeric domains must be
known and consistent. Request conditions use the same fact name; a differently
named fact cannot replace the value under test. User assumptions are disclosed,
never used to fill facts or satisfy unknown conditions.

Before calling the original `scripts/run_formula_sandbox.py`, all these gates
must pass together:

1. Original package execution qualification, checked on every invocation.
2. Source-backed science proposal, with independent external M1 science reviews.
3. Independent review of the exact M3 request plan, including unit mappings,
   property selections, curve axis semantics, conditions, assumptions and tools.
4. Every required condition satisfied. Unknown/violated is blocking.
5. Current host permission for this plan plus explicit `--allow-execution`.
6. Numeric domain/unit/bounds preflight using the original AST interpreter.

`freeze` only creates answer-free inputs for the existing attestation validator.
At least two explicit reviewers distinct from the proposer and an external session
are required to bind review. Absent identities leave the freeze paused. The
compiler never creates accepted fragments or asserts reviewer independence. Hash
and identity bindings are host-orchestrator-recorded, not cryptographic proof.

## Shapes and commands

All fixed local definitions live in
`assets/schemas/science-calculation-receipt.schema.json`: `$defs/specification`,
`selection`, `permission`, and `receipt`. References reuse M1/M2 shapes without
network resolution. A record reference is `{id, sha256}`; an object reference is
the full M1 package/version/object/hash reference. `plan` is validated by exact
reconstruction, and `freeze` by reconstruction from the current plan.

Each command requires the following operator-supplied files:

```text
python -B scripts/science_calculation.py COMMAND
  --package QUALIFIED_PACKAGE --specification SPEC_JSON --request REQUEST_JSON
  --sidecar SIDECAR_JSON --profile PROFILE_JSON [--output NEW_PRIVATE_JSON]
```

Use the repository's existing Python environment. `COMMAND` is one of:

| Command | Additional arguments / behavior |
|---|---|
| `plan` | Reconstruct the exact calculation proposal; no process execution |
| `freeze` | `--proposer-instance ID [--reviewer-instance ID ... --review-session-id ID]`; creates review inputs only |
| `check` | Review/permission arguments below; read-only gates, no sandbox invocation |
| `run` | Same arguments; invokes only the qualified original sealed runner after all gates pass |
| `validate-receipt` | `--receipt JSON` plus the same review/permission arguments; current gate and numerical replay checks, no child process |

For `check`, `run`, and `validate-receipt`, external gate inputs are:

```text
--science-frozen M1_FROZEN_JSON --science-attestations EXTERNAL_M1_ARRAY_JSON
--calculation-frozen M3_FROZEN_JSON --calculation-attestations EXTERNAL_M3_ARRAY_JSON
--permission HOST_PERMISSION_JSON --allow-execution
```

Permission fields are `schema_version=tkc.science-execution-permission/v0.1`,
`plan_sha256`, explicit `host_instance`, `session_id`, timezone-bearing ISO
`issued_at` and `expires_at`, `allow_execution=true`, `network=false`,
`write_package=false`, and
`attestation_level=host-orchestrator-recorded-not-cryptographic`. Lifetime is at
most one hour and the current instant must be within it. There are no default
identities, generated permissions, or reusable wildcard grants. This document
does not supply a permission or reviewer artifact.

New output files use exclusive creation and 0600 permissions outside the base.
Existing files, symlinks and missing parent directories are rejected before a
process call. Errors emit stable blocked codes and no receipt or private paths.
`--output` is optional; stdout contains the same bounded JSON result. Exit 2 means
blocked. A successfully emitted paused freeze is not execution permission.

## Units, selection and numeric limits

`science_units.py` uses quantity-kind before unit conversion. Current mappings:

| Quantity kind | Accepted units (explicit aliases in source) |
|---|---|
| length / area | m, mm, cm, km / m^2, mm^2, cm^2; superscript ² aliases |
| pressure / time | Pa, kPa, MPa / s, ms, min, h |
| temperature | K, degC, °C; absolute zero checked, affine 273.15 offset |
| temperature-difference | K, delta_K, delta_degC, ΔK, Δ°C; no absolute offset |
| thermal-conductivity / resistance | W/m/K, W/(m·K), W/(m*K) / K/W |
| power / speed / extinction-coefficient | W, mW / m/s, cm/s / 1 |
| mass-, volume-, mole-fraction | 1, %, mass% / vol% / mol% with matching explicit basis |

Absolute and delta temperature are distinct even when both use K. Mass/volume/mole
fractions never interconvert. Fractions in ConditionSet evaluation remain
unsupported. There is no general unit parser or automatic quantity inference.
Empirical numerical-value equations require complete native-unit conventions;
only conventions matching the legacy engine's coherent units are supported.
Celsius-native coefficients or mm-native numeric laws remain blocked, never
silently converted to a different law.

Property selection pins one observation and material state by content hash,
plus method, sample, direction and every required measurement/state condition.
Scalar selection cannot substitute a different temperature, method or direction.
Linear selection requires an explicit named axis/kind/frame, coordinate, ordered
two-or-more-point curve and enclosing segment. A known fixed axis condition must
still match. There is no extrapolation, cross-sample averaging, tensor selection,
multiaxis fitting or axis inference from units. The observation quantity ID must
match a supported quantity kind. Computed/fitted/assumed/illustrative observations
cannot clear the production provenance gate.

Nonzero numbers are bounded to [1e-100, 1e100] in magnitude. Numeric input strings
are only for the controlled legacy runner; M1 quantities require JSON numbers.
The Decimal context is local and deterministic. Exact condition conversions and
selected property decimals that cannot round-trip through the current JSON-number
contract are explicitly rejected with `precision_loss`; repeating-decimal
interpolation is therefore currently unsupported. Do not silently round to pass.

Original AST restrictions still apply: fixed arithmetic, integer powers and
sqrt on scalar quantities; no source-string eval/exec, LaTeX evaluation, arbitrary
Python input, PDE/integral solvers, complex/tensor execution or multistep formula
composition. The new preflight closes legacy generated-runtime discrepancies for
out-of-range unit exponents, zero negative powers and nonfinite/unbounded results.
Historical interpreter/generator/UNIT_TABLE files remain unchanged.

## Receipt and evidence limits

A completed receipt binds package/source/formula/AST, sidecar, request and plan,
input origins and conversions, property selection/conditions, review and
permission hashes, tools, engine module and runner, and the exact numerical result.
The observed child response must equal the original interpreter's expected result.
The isolation label is honestly `process-resource-limits-not-kernel-sandbox`.

The result is conditional calculation, not a measurement or truth certificate.
Typical values retain a non-guarantee warning. Source uncertainty may be reported
by hash for scalar observations; output uncertainty stays unavailable. No
covariance, independence assumption, propagated error or confidence is fabricated.
`knowledge_verified=false`, production publication and domain accuracy stay
blocked. Receipt replay proves current bindings and arithmetic only; it cannot
cryptographically prove that an external process ran, and expired permission
blocks current authorization even if an old receipt was once valid.

M3's synthetic tests cover the original child runner separately from qualification.
Their positive orchestration mocks external gates explicitly; they do not create
accepted Review/Gold or qualify a source package. Real production acceptance still
requires actual sources, original execution qualification and independent reviews.

## Migration and rollback

No old package or protocol migration. Store all new private inputs and receipts
outside sealed packages. M3 version registration changes M1/M2 tool hashes: retain
old sidecars, freezes, indexes and receipts; explicitly rebuild/freeze/review new
artifacts and obtain fresh plan-bound permission. Never repair stale hashes.
Disable the new entry point to roll back. Restore only batch-listed modified files
from the pre-M3 backup after checking their current hashes, and move added files to
a recoverable audit directory. Batch evidence and the actual patch are in
`docs/science/m3-implementation.md`; M4 packaging/DAG/domain profiles are separate.
