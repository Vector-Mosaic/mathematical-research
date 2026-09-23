# C90: four points rule out a proposed research route

A function can be positive and increasing while failing a matrix positivity
condition needed by a proposed proof strategy. This example checks one specific
interpolation built from the theta kernel: a four-by-four Loewner matrix has a
strictly negative quadratic form.

The witness rules out this function's **Pick / complete-Bernstein route**. It
does not decide ordinary Bernstein status, other interpolations, or RH. The
[derivation](mathematics.md) defines the function, explains the implication and
accounts for every omitted integration region.

| Witness | Exact input |
| --- | --- |
| Nodes | `1, 2, 4, 6` |
| Integer vector | `28336, -66314, 64141, -26181` |
| Decision | Certify that the upper endpoint of `qᵀLq` is below zero. |

## Run the rigorous checker

Using Python 3.12 in your own virtual environment:

```sh
python -m pip install -r examples/loewner-obstruction/requirements.txt
python examples/loewner-obstruction/check.py
```

The checker uses python-flint's Arb ball arithmetic at 70 decimal digits. It
encloses the finite integrals and adds analytic bounds for the left endpoint,
right endpoint and omitted theta components. The sign decision uses interval
bounds, rather than a floating-point eigenvalue or determinant. JSON output
includes the matrix, moments, error bounds, exact interval endpoints and runtime
versions. No model, account or server is needed.

The [recorded output](checker-output.json) and [validation notes](validation.md)
identify the fresh public execution. The [provenance](provenance.md) explains
what survives from the historical research and what was newly checked.

## Inspect the retained research

The existing real-core walkthrough also accepts this example. On Linux, install
the repository's Python dependencies and use a fresh output directory:

```sh
python -m pip install -r requirements-dev.txt
python scripts/research_example.py run --example loewner-obstruction --output .research/c90
python scripts/research_example.py inspect --output .research/c90 --details
```

This executes the checker and retains its raw output separately from supplied
scientific judgments. The exact proposed route moves from an open candidate to
a scoped refutation. Ordinary Bernstein status remains unresolved. Source
dependencies and a checkpoint survive reopening the actual SQLite/CAS store.

These are authored explanatory records, not a new autonomous discovery or a
replay of the historical run. The [first example](../finite-free-localization/README.md)
contains the separate real-model and fresh-successor demonstration.

## What the system contributes

The useful engineering behavior is preserving the result at the right scope:
reject the precise failed claim, keep its evidence inspectable, and leave
unsettled alternatives open. The
[shared walkthrough](../../scripts/research_example.py) calls the actual
[research operations](../../packages/research-core/research_core/mission_interface.py),
[capture custody](../../packages/research-core/research_core/mission_evidence.py)
and [durable store](../../packages/research-core/research_core/workspace_store.py).

Justin Sublette directed the research system's design and iterative development
with AI assistance. The numerical witness and research judgments are presented
with their actual provenance, rather than as wholly human-authored mathematics.
