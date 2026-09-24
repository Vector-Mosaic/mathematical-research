# Mathematical Research

**Solve Riemann - make no mistakes**

An autonomous AI research system built to investigate the **Riemann Hypothesis
(RH)**. The research objective is a checkable proof or disproof of this
[open problem in number theory](https://www.claymath.org/millennium/riemann-hypothesis/).
This repository publishes the real research software and four inspectable
research episodes from that effort. RH remains unresolved.

A Codex research executive chooses questions, delegates investigations,
interprets results and changes strategy. The system gives that work a durable
home: source material, mathematical claims, assumptions, failed approaches and
corrections survive individual agent conversations. A successor can retrieve
the supporting records and continue the research.

The engineering problem grew directly out of that research. An argument can
fail while a useful lemma survives. A promising result may depend on an
unproved assumption. Continuing after an interruption requires knowing what
was established, what was rejected and what remains open. The research core,
Mission Host and execution adapters implement the storage, authority and
execution boundaries needed to support that process.

Created and directed by **Justin Sublette**, with extensive Codex assistance in
implementation, mathematical exploration and testing. The
[engineering account](docs/engineering.md) explains the design decisions,
tradeoffs, observed failures and contribution.

## Start with the research

Start with the [missing-assumption research example](examples/finite-free-localization/README.md):
an actual investigation exposed a false polynomial-root bound, preserved a
corrected result and carried its added assumption into later work. You can run
its checker without an account and inspect the saved research records.
The [recorded live run](examples/finite-free-localization/validation.md) follows
two executives: a fresh successor retrieved the correction and tightened the
bound from 10 to less than 9, with the supplied proof and checker available as
references.

The [four-example collection](examples/README.md) connects other parts of the
RH research effort to inspectable evidence:

| Research episode | What to inspect |
| --- | --- |
| [Missing assumption](examples/finite-free-localization/README.md) | A counterexample, repaired theorem, exact checker and live successor refinement. |
| [C90 matrix obstruction](examples/loewner-obstruction/README.md) | A rigorous numerical counterexample that rules out one proposed route while leaving a weaker question open. |
| [Jacobi construction](examples/jacobi-extension/README.md) | A failed restricted construction followed by a wider positive-rooted model, with its analytic dependencies retained. |
| [Jensen program](examples/jensen-program/README.md) | A sustained program of estimates, a checked finite improvement and the unresolved conditions needed for a larger root claim. |

These are bounded investigations within the larger RH effort. Their provenance
and validation notes distinguish historical research, newly authored checkers,
scripted store walkthroughs and fresh model execution. They do not establish a
complete RH argument or a general research success rate.

## What is included

| Component | Responsibility |
| --- | --- |
| [Research core](packages/research-core/README.md) | Python/SQLite research workspace, exact revisions and dependencies, evidence custody, checkpoints, recovery and canonical admission. |
| [Mission Host](services/rh-mission-host/README.md) | TypeScript executive lifecycle, scoped tools and delegated grants, capture, stop and restart reconciliation. |
| [Codex boundary](packages/codex-thread-core/README.md) | Transport and execution through the official Codex App Server, including native delegation and observable execution. |
| [Attempt adapter](packages/research-attempt-adapter/README.md) | Optional formal execution with immutable inputs, journal-before-effect dispatch, cancellation and sealed outputs. |

The executive uses nine research operations: `orient`, `retrieve`,
`record_context`, `interpret_material`, `record_candidate`, `record_branch`,
`synthesize`, `record_strategy` and `checkpoint`.

Raw output, interpreted evidence, candidate claims and canonical admission remain
distinct. An interpretation can be corrected without erasing its source. Failed
approaches can retain valid partial results. Checkpoints preserve work for a
successor executive; a conversation transcript is not the research database.
Successful execution or agent agreement does not establish a mathematical proof.
See the [architecture](docs/architecture.md) for these boundaries and the
[scientific instructions](docs/instructions/AGENTS.md) for participant roles.

## Run it

The first example's exact checker needs only Python 3.12 or later:

```sh
python examples/finite-free-localization/check.py
```

Its walkthrough also provides inputs for an optional real Host reproduction.
The public starting state contains no admitted mathematical results. The system
is not an automated theorem prover; synthetic test fixtures are software inputs,
not observed research outcomes.

Follow the [Linux setup guide](docs/setup.md). The source uses Python 3.12 or
later, Node.js 22 or later, and pnpm 10.14.0. The supported deployment separates a
root-owned immutable release from a dedicated unprivileged runtime account and
its mutable research state.

Live execution requires the official `@openai/codex@0.153.4` CLI and your own
authenticated account with access to `gpt-6-astra` at `ultra` reasoning effort.
The runtime has no silent model or provider fallback. The setup imports model
metadata from your own Codex installation; provider prompts and login state are
not distributed here.

No server, account access or private research data is supplied. Use independent
runtime directories and an isolated Codex home. Read the [security and deployment
boundaries](docs/security.md) before running the system or sharing runtime output.
The [validation notes](docs/validation.md) describe the software checks and their
limits.

## License

[MIT](LICENSE), copyright Justin Sublette.
