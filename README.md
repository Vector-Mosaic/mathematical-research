# Mathematical Research

A durable AI-assisted mathematical research system. A Codex executive chooses
research questions, interprets results and directs further work. The software
preserves research state, checks authority and dependencies, and manages execution
and recovery. Its first implemented theorem project is the Riemann Hypothesis.

This repository contains the research core, Mission Host and execution adapters.
It is not an automated theorem prover. The fresh public starting state contains
no admitted mathematical results and no claimed proof or disproof of RH. The
synthetic test fixtures are software inputs, not observed research outcomes.

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

## Get started

Start with the [missing-assumption research example](examples/finite-free-localization/README.md):
read the mathematical correction, run its account-free checker, and inspect how
the real research store retains a failed claim and useful corrected result.
Its [recorded live run](examples/finite-free-localization/validation.md) shows a
fresh executive retrieving that correction and tightening the bound. The example
also provides inputs for an optional real Host reproduction.

The shorter [C90 matrix obstruction](examples/loewner-obstruction/README.md)
adds a rigorous numerical checker and a store walkthrough showing how one failed
research route is recorded without discarding unresolved alternatives.

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
