# Research contracts

`schemas/` contains the public system's structural contracts for canonical
research state, execution intents and results, evidence/context/synthesis
outcomes, candidate revisions, and admission. Historical schema versions remain
where the implementation explicitly supports them. The Python owners enforce
the corresponding semantic and reference constraints; JSON Schema success alone
does not establish mathematical correctness.

`rh_autonomous_mission_seed.v1.json` is a newly authored public Mission genesis.
It binds one initial Mission, opening branch, and continuing Strategy with exact
content references. Its purpose and provider policy match the research core's
Mission owner; editing a model name in this file alone is not a supported policy
change. The installed runtime must have access to the explicitly selected model
through the operator's own account. There is no silent fallback.

The seed contains no established research results and grants no access to the
author's hosts, accounts, or private work. Its authority identifiers are local
state bindings, not provider credentials. A newly initialized Mission has no
inherited scientific Context, captures, or private history. The executive must
author useful scientific Context through the supported interface when its
research requires it.

See `projects/riemann_hypothesis/README.md` for the canonical starting state's
explicit inherited moment-profile limitation. See the repository README for
initialization and runtime commands; those commands configure separate mutable
state rather than writing research outputs into these tracked templates.
