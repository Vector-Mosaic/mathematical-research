# Engineering research that can outlive a conversation

Mathematical Research exists to pursue a checkable proof or disproof of the
Riemann Hypothesis with autonomous AI. The engineering problem is how to let
that work accumulate: preserve a useful lemma inside a failed approach, carry
its qualifications into later reasoning, and let a new executive change
direction without losing what the Mission has learned.

Justin Sublette originated the project and directed its problem definition,
system and authority design, integration, evaluation, correction and acceptance.
Codex contributed extensively to implementation, investigation, mathematical
exploration and testing. The research executive is itself an AI agent; choosing
mathematical questions and interpreting results are part of its job. This is
an AI-assisted engineering project, without a claim of unaided code authorship
or personal derivation of every mathematical result.

Three decisions connect that purpose to the implementation and the published
evidence.

## 1. Give the research a durable identity and the executive a temporary one

A Mission owns the objective and operating authority. An executive epoch owns
one agent's span of work. This lets the research continue across executives
while preserving the distinction between the current direction and the larger
objective.

The workspace separates **Strategy**, which describes what to pursue and why,
from **Scientific Context**, which preserves useful understanding beyond the
current bet. A technique can remain useful after its branch loses priority.
Conversely, a successor must be able to question the inherited Strategy without
discarding valid results.

The [orientation implementation](../packages/research-core/research_core/executive_orientation.py#L1271)
returns current Strategy and Mission-bound scientific Context as separate
fields. [Compact checkpoints](../packages/research-core/research_core/mission_executive.py#L575)
bind exact Mission and Strategy revisions, the state cut, executive identity,
predecessor and unresolved attention. Fuller material remains available through
deliberate retrieval; the checkpoint does not copy the entire research archive.

This choice requires the executive to maintain useful scientific meaning and
retrieve the right supporting material. It does not promise constant context
cost or automatic recognition of every historical connection. The executive is
also the single research-state writer: workers can investigate in parallel,
while their results pass through a coherent integration point. That trades
independent worker edits for explicit responsibility for the current research
position.

**Observed behavior:** in the [live finite-free example](../examples/finite-free-localization/validation.md#live-execution),
the first executive recorded a repaired claim with its additional assumption.
A distinct fresh executive retrieved the exact Evidence and Candidate
revisions, retained that assumption, and derived a
[tighter bound](../examples/finite-free-localization/successor-refinement.md).
The [selected actual requests and results](../examples/finite-free-localization/live-run.json)
connect both executives and their checkpoints. The proof and checker were
available as references; this demonstrates continued reasoning from retained
work under those conditions.

## 2. Preserve source material separately from conclusions about it

A worker's output can contain a correct calculation and an unsupported
generalization. Later investigation may change the interpretation while the
original output remains important for understanding the correction.

The system therefore distinguishes Raw Capture, Evidence interpretation,
Candidate claims and canonical admission. Captures retain exact bytes and
provenance. Evidence identifies what selected material supports, with scope and
limitations. Candidates give proposals revisioned identities. A separate
review and decision path controls changes to the canonical mathematical record.

[Raw capture and Evidence](../packages/research-core/research_core/mission_evidence.py)
have separate preparation and commit operations. Evidence revisions bind an
expected prior head and exact source references. The
[canonical writer](../packages/research-core/research_core/canonical_admission.py#L398)
applies an exact Admission Decision-derived change; an ordinary interpretation
or Candidate write does not perform that operation.

The tradeoff is additional structure and authoring work. The executive has to
state the useful conclusion, preserve its conditions and reconsider affected
reasoning when a premise changes. Reference validation protects identity and
consistency; it does not automatically discover every mathematical dependency
or propagate a correct scientific interpretation. Content hashes establish
which bytes were retained, not whether their argument is true.

**Inspectable consequences:** the [C90 example](../examples/loewner-obstruction/README.md)
excludes one representation route while leaving a weaker question open. The
[Jacobi example](../examples/jacobi-extension/README.md) preserves the failed
restricted construction, a wider constructive alternative and the analytic
premises needed to apply it. The [Jensen example](../examples/jensen-program/README.md)
shows a useful finite improvement with explicit dependencies on a larger
analytic program. Their runnable store walkthroughs use authored judgments;
the checkers, historical accounts and store results are separately labeled.

## 3. Keep execution authority separate from mathematical judgment

The executive chooses and interprets research. The Python core owns research
state and authority checks. The TypeScript Mission Host owns the executive's
lifecycle and execution bindings. The model receives scoped tools and a scratch
workspace; trusted runtime configuration and credentials have separate custody.
This separation makes a tool or process failure diagnosable without turning it
into a mathematical conclusion.

It also creates a real integration cost: a runtime can be correctly isolated
yet unable to perform an intended operation. The first public live attempt
exposed exactly that problem. Research-tool calls and checkpoints worked, but
the shell sandbox could not read the native Codex executable installed outside
standard system paths. That attempt was stopped and remains documented as a
failure.

The [repair](https://github.com/Vector-Mosaic/mathematical-research/commit/9021eb1df3ee38055715567928cc7c5273599e9c)
added read access to the exact resolved executable. It preserved the denial of
the protected Codex home and did not grant the executable's containing
directory. The [permission renderer](../packages/codex-thread-core/src/boundary.ts#L1517)
and [focused checks](../packages/codex-thread-core/src/boundary.test.ts#L870)
make that boundary inspectable. The subsequent live run executed the shell
successfully and completed the two-executive example. Its
[validation record](../examples/finite-free-localization/validation.md#live-execution)
keeps the failed attempt and the successful repair distinct.

Recovery likewise uses durable owner and execution identities. A process being
gone does not establish that its last effect never happened. The Host has
separate [active-Goal recovery and cancellation reconciliation](../services/rh-mission-host/src/mission-host.ts)
paths; the optional formal Attempt adapter additionally uses a
[persistent journal](../packages/research-attempt-adapter/README.md).
The published [live lifecycle evidence](validation.md#live-qualification)
establishes tool interaction, orderly stop/restart and retained state in the
tested configuration. It is not a claim that every possible interrupted
operation has been exercised.

## Read the evidence at the scope it supports

The [four examples](../examples/README.md) already connect actual research
episodes to checked mathematics, recorded dependencies and the implementation.
The first also supplies an observed successor and subsequent refinement. Their
provenance pages explain which history was inspected and which public material
was newly authored. The private research store is not part of the distribution.

The evidence establishes scoped mathematical results, durable research records,
real tool interaction, lifecycle recovery and one live successor refinement.
It does not measure a general success rate, comparative advantage or mathematical
novelty. RH remains unresolved. The engineering contribution is the connection
between that serious research purpose, the implemented choices above, and the
behavior a reader can inspect and reproduce.
