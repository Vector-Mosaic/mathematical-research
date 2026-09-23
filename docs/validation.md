# Validation scope

The standalone extraction is checked on Linux with Python 3.12, Node.js 22 and
pnpm 10.14.0. Builds and checks run under a separate unprivileged account with
independent source and state. They do not use the author's private research data.

The software checks cover the Codex transport boundary, observation handling,
Mission Host, formal Attempt adapter, fresh canonical state, scientific operation
contracts, admission and the public command-line interface. Existing deterministic
tests use authored fixtures and test doubles. Cross-language integration tests
also execute the actual Python owner from the TypeScript Host.

During extraction, those checks found and corrected filesystem-dependent test
fixtures, stale fixture expectations and an invalid ordering in the fresh seed.
Provider-cache compatibility is checked separately against the operator's real
Codex installation. A test double does not establish provider access or native
sandbox behavior.

## Reproduce software checks

After the development environment in [setup](setup.md) is installed:

```bash
pnpm run build
pnpm run test:remote-core
pnpm run test:thread-core
pnpm run test:host
node --test services/rh-mission-host/scripts/import-model-catalog.test.mjs
python -m unittest discover \
  -s packages/research-attempt-adapter/research_attempt_adapter/tests
PYTHONPATH=packages/research-core/tests python -m unittest \
  test_validator test_validator_schema_parity test_mission_operation_contract \
  test_mission_owner test_canonical_admission test_rh_mission_cli \
  test_mission_attempt_runtime
```

The final command selects the core paths relevant to the standalone release.
Additional tests include growth and storage diagnostics; they are not a required
whole-system acceptance loop for a documentation change.

## Live qualification

The release's real initialization, interaction, stop and restart result will be
recorded here before publication. This is a software lifecycle check, not an RH
result, a research benchmark or evidence of autonomous mathematical productivity.
The reproducible worked research example and comparative evaluation are separate
work from this initial extraction.
