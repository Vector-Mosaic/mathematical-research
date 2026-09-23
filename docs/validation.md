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

On September 23, 2026, runtime commit
[`cf989b13ab26c0861867bc79a378d1bb254fd202`](https://github.com/Vector-Mosaic/mathematical-research/commit/cf989b13ab26c0861867bc79a378d1bb254fd202)
completed a real Linux lifecycle check with Codex 0.153.4, `gpt-6-astra` / `ultra`,
Python 3.12.3, Node.js 22.23.2 and pnpm 10.14.0. The account, Codex home, research
workspace and release were separate from existing research installations. The
processes were limited to two CPU cores and 4 GiB RAM, with no GPU access.

- Prepared an immutable release from the exact source archive and imported model
  metadata from the authenticated operator's own Codex installation.
- Initialized the fresh public research workspace and inspected it successfully.
- Started the actual Mission Host and observed successful `rh_mission` tool
  responses from the real research owner.
- Requested an operator stop; the Goal paused, the Host exited successfully and
  the runtime lock was released.
- Restarted the same installation. The native thread resume succeeded, research
  interaction continued, and the retained workspace advanced from revision 5
  before restart to revision 18 after the second stop. Canonical authority stayed
  unchanged.
- Stopped the Host again: exit code 0, suspended Goal, released lock and no
  remaining test workload. Both runs closed their observation streams with zero
  dropped observations.

The focused software checks above passed across the extraction and relevant
targeted repairs. Unaffected results were reused; this was not a fresh run of
every repository test at the final documentation commit. Additional core growth
and storage diagnostics were not part of release acceptance.

This establishes initialization, real tool interaction and lifecycle recovery in
the tested configuration. It does not establish an RH result, mathematical
correctness of generated work, autonomous research productivity or every possible
provider failure path. The reproducible worked research example and comparative
evaluation remain separate work. Credentials, provider instructions and raw
runtime traces are not published.
