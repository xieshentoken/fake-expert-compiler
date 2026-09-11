# Host adapters

## Canonical-core rule

Keep knowledge, evidence, procedures, decisions, tests, and package identity in the
canonical Expert Skill. A host adapter may change discovery metadata, invocation
syntax, or tool bindings; it must not fork or rewrite technical truth.

## Receiving a generated `fake-*` Reference Draft

First run the standalone verifier in the complete `draft-distributable` release
directory and safely extract its ZIP into an empty directory. The runtime root is
the extracted `<package-id>/`, not the outer release directory. Read that root's
`RECEIVER.md` and `SKILL.md`, then call the packaged
`scripts/query_reference.py` from the extracted package root with Python bytecode
writes disabled (`python3 -B`); do not copy knowledge into a host-specific store.
The release adapter declares `working_directory=package-root` and binds the
machine-readable response contract at
`references/runtime/response.schema.json`.
The same query must preserve returned object/evidence IDs and the package-level
assurance block. In particular, `unreviewed-candidate`, `review_state=deferred`,
and `knowledge_verified=false` are part of the runtime contract, not optional UI
text. A host adapter cannot attach Gold, mutate the package, or upgrade it to
ready/promoted/executable. `extend` and `bind-gold` are compiler operations that
produce a separately sealed version.

## Codex adapter

Preserve the generated package's existing `agents/openai.yaml`. Add a separate
host-side discovery wrapper only if the target Codex environment requires one;
do not edit the canonical `SKILL.md`, runtime, or knowledge files. Validate
the Skill folder with the host's Skill validator after canonical publication checks.
For a `draft-distributable` package, discovery may expose `$fake-*`, but the
default prompt must describe it as a candidate Reference Skill and route factual
answers through the packaged query runtime.

## Claude Code adapter

Add only the host-specific discovery/invocation files required by the current Claude
Code environment. Verify the deployed copy, available tools, permission boundaries,
and actual task behavior. Do not assume that a portable Skill has identical runtime
authority on every host.

## Hermes adapter

Record Hermes as the orchestration host and record its actual inference provider and
model separately. Use a new temporary `HERMES_HOME`, disable user configuration,
rules, memory, plugins, and MCP, and expose only the file toolset. On macOS, also use
a per-run Seatbelt profile that permits read-only access to the staged Skill and
permits writes only to separate home/control directories. A prompt asking Hermes to
stay inside the package is not a filesystem boundary.

Export a redacted full transcript from the same isolated home. Reject a run when the
system prompt contains ambient memory/profile sections, any successful tool falls
outside the read-only file set, any requested path escapes the staged package, the
package hash changes, or provider/model/session bindings disagree. Retain unavailable
tool attempts in the audit but distinguish them from successfully executed tools.

Never copy the provider credential file into the isolated home. Carry only the one
named credential required by the explicitly authorized provider. Record staged file
counts, accessed package paths, transcript/usage hashes, and the absence of the
original source and hidden expectations.

## Adapter acceptance

### Optional visual-parser licensing boundary

The Phase 7A core path is `pypdf==6.10.0` plus local `pdftoppm`. PyMuPDF/`fitz`
is an optional candidate adapter only; PyMuPDF is distributed under AGPL terms
with a separate commercial license option. A host that enables it must perform
its own AGPL/commercial-license review before deployment or redistribution. The
private fake-expert release does not bundle PyMuPDF, its binaries, or models, and
the pure mapping function does not import or execute it. Missing extras, model
roots, or license authorization are explicit unavailable/fail-closed states.

For current semantic promotion (v0.3.2+), a host adapter must create a separate review
session after proposal freeze and return the bound `review_session_id`,
`review_method`, and `review_checks` fields. A proposal task must not write its own
accepted semantic records or visual receipts, and review fragments must be written
outside the workpack and merged through `semantic_workpack.py merge-review`, which
records per-fragment SHA-256 provenance; direct central-file writes are rejected
with `review_fragments_manifest_missing`. A helper that loops over the review
queue and fills `accepted`, `verified`, or one generic rationale is an invalid
adapter workflow; the deterministic gate reports `review_protocol_invalid`,
`review_checks_missing`, or a `review_blanket_pattern` quarantine even if the item
hashes are correct. The protocol remains host-recorded rather than cryptographic
identity proof, so the adapter must not describe its receipts as human approval.

Treat calibration and certification as different datasets. Calibration may refine a
challenge or replace a brittle single expected behavior with an explicit allowed set,
but no calibration run may enter the certification bundle. Before formal runs, hash
the normalized hidden expectation bundle and record its challenge count and commit
time. Do not give tested instances the expectation bundle, scoring rules, prior
responses, or receipts.

Run the same leak-free challenges against each host while restricting answers to the
package. Require at least two attempts per challenge, one fresh Agent instance per
attempt, and a single declared host/version/model environment per certification.
Compare returned object, evidence, conflict, and gap IDs; evidence-to-object
coherence; required boundary language; abstention or escalation behavior; and
executable permission behavior. Reject invented identifiers and stale package,
adapter, runtime, challenge, expectation, response, or transcript hashes.

Store the result in a certification sidecar outside the sealed canonical Skill. The
sidecar must include challenges, released expectations, pre-run commitment, run
records, deterministic receipts, and a recomputable certification report. A host
failure is an adapter failure unless the same challenge exposes missing canonical
knowledge. A certificate for one exact host/model/package fingerprint does not apply
to another fingerprint.

Deterministic runtime receipts certify only the canonical package-only retrieval
layer. Do not relabel them as Codex, Claude, human, or domain-expert evaluations.
Host forward receipts add evidence about observed adapter behavior, but their
`host-orchestrator-recorded-not-cryptographic` label is not identity authentication or
human approval.
