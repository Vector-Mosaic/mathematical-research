# C90: recorded validation

On September 23, 2026, the public checker certified

```text
-374.57073357307433 <= qᵀLq <= -374.57073357307320 < 0.
```

These decimal endpoints are rounded outward from the exact binary endpoints in
[checker-output.json](checker-output.json). The sign excludes positive
semidefiniteness of the displayed four-point Loewner matrix. Together with the
[analytic derivation](mathematics.md), it excludes a Pick extension agreeing
with the exact function on `u>1/2`, hence the proposed complete-Bernstein route.
Ordinary Bernstein status, other interpolations and RH remain undecided.

## Source and environment

Execution used commit
[`e6565177126c76323831d3cf6a985ba2febbc535`](https://github.com/Vector-Mosaic/mathematical-research/commit/e6565177126c76323831d3cf6a985ba2febbc535)
on Ubuntu 24.04 with Python 3.12.3, python-flint 0.8.0 and FLINT 3.3.1.
Arb used 70 decimal digits (236 bits) and one thread. The cutoff, integration
segments, tolerances and all three omitted-region bounds are recorded in the
output and explained in the mathematical note.

The checker output's SHA-256 is
`a5cad149ddbc91f145b7c9a6c538bdfa8ce58d434d3690fcfc918626eb99cbe4`.
Its four source-file hashes match the committed inputs. The output was retrieved
byte-for-byte from the research store's raw capture. Later publication adds the
recorded evidence, documentation and a test-assertion correction; it does not
change the executed checker or walkthrough.

The run used a separate unprivileged account and research state, limited to two
CPU cores and 4 GiB RAM, with no GPU or access to existing research installations.
No Codex execution or account was needed. The unchanged JavaScript build and
the first example's live lifecycle evidence were reused.

## Store behavior checked

The following operations ran against the real research core:

```sh
python scripts/research_example.py run --example loewner-obstruction --output .research/c90
python scripts/research_example.py inspect --output .research/c90 --details
```

The operator used an isolated output path; the commands above show a portable
equivalent. Inspection ran in a new process. The retained
[store summary](store-summary.json) contains selected observed records:

- Six raw artifacts retain their exact bytes and the interpretation's source
  references: problem, derivation, checker, authored judgments, dependency pin
  and newly computed output.
- `loewner-pick-route@1` is open; revision 2 is `refuted_at_scope` and refers to
  the exact interpreted evidence revision and digest.
- `loewner-ordinary-bernstein@1` remains open.
- The checkpoint remains at project commit 10; canonical effect is `none`.
- An explicit mismatched example selector is rejected. Reusing the output path
  is rejected before another calculation or any overwrite.

An initial verification assertion expected the supplied short reference instead
of the core's normalized reference containing kind, identity, revision and
digest. The assertion was corrected and checked against the retained output;
the numerical calculation and store execution were not repeated.

Because the shared selector changed, the existing finite-free default-path
round-trip test also ran once and passed (one test, 8.360 seconds):

```sh
PYTHONPATH=packages/research-core/tests python -m unittest \
  test_public_research_example.PublicResearchExampleTests.test_correction_and_surviving_result_survive_process_reopening
```

The added C90 unit test exposes the same round-trip behavior for future runs.
Acceptance here used the retained direct execution and inspection above, without
a second numerical run merely to invoke that test.

## Scope of the evidence

The computation is a fresh certificate, not recovered historical stdout. The
store judgments are authored explanatory inputs, not autonomous model findings.
The core preserved those judgments and dependencies; it did not prove them.
This example adds no new live-model, successor, general productivity or novelty
claim. Historical provenance is documented [separately](provenance.md).
