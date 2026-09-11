# Safe formula execution tier

`executable=true` is never a semantic-promotion capability. It is a separate
compiler action available only to a sealed, ready, source-required, reference-only
package.

## Required source and review bindings

An executable equation must be a source-explicit or source-paraphrase `Equation`
with nonempty evidence anchors, a full-support independently recorded review binding,
source and normalized notation, and an AST that was already part of the reviewed
proposal. Its `formula` record contains:

```json
{
  "ast_schema_version": "tkc.formula-ast/v0.1",
  "ast_sha256": "<canonical-json-sha256>",
  "ast": {
    "schema_version": "tkc.formula-ast/v0.1",
    "output": "speed",
    "symbols": {
      "distance": {"role": "input", "unit": "m", "dimension": {"L": 1}},
      "time": {"role": "input", "unit": "s", "dimension": {"T": 1}},
      "speed": {"role": "output", "unit": "m/s", "dimension": {"L": 1, "T": -1}}
    },
    "expression": {
      "kind": "divide",
      "left": {"kind": "symbol", "name": "distance"},
      "right": {"kind": "symbol", "name": "time"}
    }
  }
}
```

Only `constant`, `symbol`, `add`, `subtract`, `multiply`, `divide`, `negate`,
integer `power`, and dimensionally valid `sqrt` nodes exist. Formula strings and
generated Python are never accepted as input. A formula marked derived, conflicted,
dimensionally unassessed, or linked to an unresolved conflict cannot enter this tier.

## Independent test-suite input

The supplied JSON has `tkc.execution-test-suite/v0.1`, one host-recorded test author,
and at least two cases per selected formula. That author must differ from the
execution compiler instance, the semantic proposer, and every legacy lead or v0.5
risk-tiered semantic reviewer. Cases have exact input unit
records, expected output value/unit, and nonzero absolute or relative tolerance.
They are bundled for reproducibility but are not a substitute for source review.

## Isolation and limits

The generated evaluator is standard-library-only, has a frozen AST, and exposes no
file, network, or subprocess call. Each test runs in a new Python child with isolated
mode, a fresh temporary working directory, bounded CPU/wall time, AST/stdin size,
file-size, file-descriptor, core-dump, and (where supported) process limits. The
policy names this boundary `process-resource-limits-not-kernel-sandbox`; adapters
must retain that exact limitation. The parent must give the compiler no ambient
credentials, network authority, or writable destination beyond the explicit output.

## Publish gate

The execution compiler writes `references/execution/` with policy, independent
tests, receipts, generated module hashes, source-object/evidence IDs, AST hashes,
and review bindings. `validate_expert_skill.py` recomputes unit/dimension checks and
the expected test values, checks all IDs and hashes, validates the generated-code
surface, and requires one passing receipt per case. `seal_expert_skill.py --mark-ready`
is the only route from its unsealed draft to an executable ready package.
