# Architecture

This is a durable agent research system whose first implemented theorem project
is the Riemann Hypothesis. A Codex executive pursues the Mission, selects
questions and interprets results. Deterministic software preserves the resulting
state, checks authority and reference integrity, and controls execution.

The system is not an automated theorem prover. A completed process, agreement
between agents, a numerical experiment, or an admitted record does not replace a
mathematical argument. The public starting data contains no claimed proof or
disproof of RH. Generalizing these mechanisms to other theorem projects remains
separate work; this release does not claim a general proof engine.

## Owners and storage

| Component | Responsibility |
| --- | --- |
| [Research core](../packages/research-core/README.md), Python | Typed research operations, single-writer SQLite workspace, content-addressed capture custody, exact revisions, dependencies, checkpoints, recovery and canonical admission. |
| [Mission Host](../services/rh-mission-host/README.md), TypeScript | Executive lifecycle, authenticated caller and child grants, tool projection, raw capture, stop and restart reconciliation. It does not choose the mathematics. |
| [Codex boundary](../packages/codex-thread-core/README.md), TypeScript | Official Codex App Server transport, exact model configuration, native identity and delegation lineage, permissions and observable execution. |
| [Attempt adapter](../packages/research-attempt-adapter/README.md), Python | Optional formal execution: immutable inputs, journal-before-effect dispatch, cancellation, reconciliation and sealed outputs. |

The immutable installed source, mutable canonical Git record, research workspace,
per-Goal scratch, Host runtime state and protected Codex authentication are
distinct surfaces. The workspace's noncanonical research records can contain
tentative, conditional, failed and unresolved work. The canonical mathematical
record has a separate admission owner; a routine workspace write cannot change
it. See [setup](setup.md) for the actual paths and lifecycle commands, and
[security](security.md) for trust boundaries.

## Research meaning

A **Mission** preserves the target, proof standard and operating authority.
An **executive epoch** is one executive's span of work; the Mission survives it.
**Scientific Context** preserves reusable understanding and unresolved questions
independently of the currently selected **Strategy**. A **Branch** tracks a
particular mathematical line of inquiry; **Candidates** record definite
proposals with their actual assumptions and supporting references.

A **Raw Capture** preserves source or output bytes and provenance. An
**Evidence interpretation** records the executive's scoped meaning of already
preserved material. These are separate: one output can support several qualified
interpretations, and an uninterpreted capture is not an established result.
Dependencies refer to exact owners and revisions. Correcting an argument must
address its actual dependents while retaining valid partial results and
unaffected work. A failed approach is not a refutation of its target theorem.

The executive has nine semantic operations:

| Operation | Meaning |
| --- | --- |
| `orient` | Read the current Mission, scientific Context, Strategy and relevant attention. |
| `retrieve` | Find and read authorized exact current or historical material. |
| `record_context` | Preserve useful scientific context, qualifications and dependencies. |
| `interpret_material` | Attach scoped meaning to material already in custody. |
| `record_candidate` | Preserve a definite mathematical proposal. |
| `record_branch` | Create or revise a line of mathematical inquiry. |
| `synthesize` | Relate interpreted results and record explicit consequences. |
| `record_strategy` | Record the executive's causal research direction. |
| `checkpoint` | End the epoch with a durable, proof-neutral handoff. |

`usage` describes the current contract without effects; it is not a tenth
semantic operation. Operational execution and restricted review have separate
interfaces and grants. Generated tool definitions come from the Python
[operation contract](../packages/research-core/research_core/mission_operation_contract.py),
not an independently maintained copy of these descriptions.

## Preservation, review and admission

A purported complete RH proof or disproof is preserved immediately as an
unverified complete-target Candidate, including available exact supporting
references. That opens **A1**, the independent claim-triage path. Preservation
does not certify the claim or permit disclosure. Independent triage, frozen
Admission review, an admission decision and the operator-owned canonical change
remain separate acts. Caller identity and frozen case scope matter; relabelling
the author as a reviewer does not make a review independent.

An actual integrity or authority problem invokes **A2**, a freeze on affected
effects and their demonstrated dependents. An ordinary missing artifact or
provider failure is not, by itself, mathematical refutation or evidence that all
research is contaminated. The [scientific instructions](instructions/AGENTS.md)
and [restricted review instructions](instructions/Restricted_Review.md) preserve
these distinctions for research participants.

## Continuation and formal execution

Writes bind exact expected state and operation identity. A lost reply can replay
the persisted result instead of duplicating the work. A checkpoint captures the
handoff at an exact state cut; a successor obtains current owner state and exact
historical retrieval rather than treating a previous transcript as authority.
Host restart reconciles recorded execution before continuing. Deleting state or
blindly relaunching an uncertain effect is not recovery.

A formal Session is optional and explicitly selected by the executive's current
Strategy. The core persists its immutable binding before the Attempt adapter
dispatches a provider effect. The adapter's durable worker and append-only
journal distinguish running, terminal, fenced and genuinely unknown execution.
Graceful cancellation and explicit force-stop are separate operations.
Verified output becomes Raw Capture; it does not automatically become Evidence
or an admitted claim. A corrected retry needs a known eligible outcome, a new
Attempt identity and a material correction. Unknown execution cannot be retried
as though nothing happened.

The system adds no universal research token, time, output or worker-count
budget. Provider capacity and operator-supplied computing resources remain real
limits. This document describes the implementation's boundaries; it is not a
report of a successful research run or a qualification result.
