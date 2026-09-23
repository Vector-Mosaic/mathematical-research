# Jacobi: a failed restriction leads to a wider construction

The research was trying to build a positive-rooted comparison polynomial that
matches more coefficients of a target sequence. Adding small positive Jacobi
corrections looked natural, but the matching equations forced a positive
weighted sum of squares to equal `-1/6`.

The useful next step was to change the factor arrangement. Two wider factors
have an exact solution to the limiting matching equations. A negative offset
between adjacent factors is compatible with positive gaps inside both factors.
That distinction preserves a constructive route after the restricted one fails.

| Question | Result exposed by this example |
| --- | --- |
| Can a fixed finite collection of short positive-gap corrections satisfy the specified limiting target? | No: the sum-of-squares identity is contradictory. |
| Can the wider two-factor system satisfy that target? | Yes, at `(a,b,mu,t) = (3,-2/3,-16/3,2)`, with Jacobian determinant `-1/6`. |
| Does this prove exact matching for the actual RH coefficients at finite `n`? | The implication requires the stated analytic convergence estimates; the public checker does not establish those estimates. |
| Does it prove a larger Jensen hyperbolicity range or RH? | No. Those conclusions require additional analytic and stability arguments. |

The [mathematical note](mathematics.md) gives the definitions, proofs and precise
conditions. The [provenance](provenance.md) follows the actual research, including
the historical symbolic script that failed to start and subsequent work on
the remaining dependencies.

## Run the exact checker

With Python 3.12 in your own virtual environment:

```sh
python -m pip install -r examples/jacobi-extension/requirements.txt
python examples/jacobi-extension/check.py
```

The checker uses exact symbolic and rational arithmetic. No model, account or
server is required. Read the [recorded result](checker-output.json) and
[validation notes](validation.md) for the executed scope and source version.

## Inspect the research records

The shared walkthrough uses the actual research core and durable store. On
Linux, with the example dependency above installed:

```sh
python -m pip install -r requirements-dev.txt
python scripts/research_example.py run --example jacobi-extension --output .research/jacobi
python scripts/research_example.py inspect --output .research/jacobi --details
```

It retains the failed restricted claim, the supported constructive algebra and
the unresolved analytic premises as distinct records. The checker output is
raw material; the accompanying scientific judgments are explicitly authored
inputs. A checkpoint and exact dependencies survive reopening. The
[recorded summary](store-summary.json) is readable without installing anything.

This is a worked reconstruction, not fresh autonomous discovery. The
[finite-free example](../finite-free-localization/README.md) supplies the
separate live-executive and successor demonstration.

## Connection to the system

The example makes a research decision inspectable: a scoped failure changes
the next approach, while useful results and unresolved dependencies remain
available. The [shared walkthrough](../../scripts/research_example.py) uses
the real [research operations](../../packages/research-core/research_core/mission_interface.py),
[evidence custody](../../packages/research-core/research_core/mission_evidence.py)
and [durable store](../../packages/research-core/research_core/workspace_store.py).

Justin Sublette directed the system's design, development and research workflow
with AI assistance. The historical findings, fresh mathematical checks and
authored explanatory records are attributed separately in the provenance.
