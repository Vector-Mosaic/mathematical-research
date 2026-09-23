# Jacobi example: recorded validation

On September 23, 2026, the exact checker and the real-store walkthrough passed
from committed source
[`a4606ae4c4a19e23ff7afcd2706af5007f029dea`](https://github.com/Vector-Mosaic/mathematical-research/commit/a4606ae4c4a19e23ff7afcd2706af5007f029dea).
Execution used Python 3.12.3 and SymPy 1.14.0 on Ubuntu 24.04.

## Mathematical checks

The [full output](checker-output.json) records:

- The short-gap weighted-square identity and its impossible target `-1/6`.
- The wide solution `(3,-2/3,-16/3,2)`, zero residual, exact Jacobian determinant
  `-1/6`, inverse and limiting uniqueness calculation.
- The quotient recursion, coefficient factorization and telescoping identity.
- General symbolic differential-operator and differentiated recurrence identities.
- A rational degree-five comparison with positive within-factor gaps `8,40`
  and negative cross-junction offset `-4`. Both factors and their convolution
  have five simple positive roots, isolated with exact rational endpoints.

For the comparison polynomial, those five intervals are:

| Lower endpoint | Upper endpoint |
| --- | --- |
| `257/66` | `74/19` |
| `634/93` | `75/11` |
| `51/5` | `3458/339` |
| `679/48` | `580/41` |
| `322/17` | `1307/69` |

The intervals are disjoint and positive. Exact evaluation gives a sign change
across each interval; five such roots exhaust degree five. Coefficients and the
two factors' intervals are included in the output. This concrete comparison
was not fitted to actual RH coefficients.

The mathematical note also gives a complete conditional contraction argument
for finite-`n` matching under its explicit uniform `C1` hypothesis. Coding-agent
review found the scoped derivation consistent and clarified the required
positivity of the target sequence before execution. It was not an independent
human review or proof-assistant verification. The analytic hypothesis, a larger
Jensen hyperbolicity region and RH are not established by these checks.

## Durable research behavior

The actual commands were the equivalent of:

```sh
python scripts/research_example.py run --example jacobi-extension --output .research/jacobi
python scripts/research_example.py inspect --output .research/jacobi --details
```

The operator used a separate runtime output directory. Inspection ran in a
new process. The [selected observed records](store-summary.json) show:

- Six raw artifacts retained with exact byte hashes: problem, derivation,
  checker, authored judgments, dependency pin and fresh checker output.
- Five Candidate revisions: the restricted route changes from open to
  `refuted_at_scope`; the limiting construction, analytic premise and conditional
  application retain distinct records.
- The conditional application refers to the exact evidence and Candidate
  revisions, including the analytic premise that this package leaves open.
- Checkpoint project commit 12 survives reopening; canonical effect is `none`.
- An explicit wrong example selector and an attempt to reuse the output path
  are rejected, without another mathematical run or an overwrite.

The limiting algebra's Candidate status remains `open` because this authored
walkthrough makes no canonical admission. Its basis explicitly records the
successful exact check. The analytic premise is separately open because this
package does not establish it; these are different mathematical situations.

The checker ran once. Its raw output was read back byte-for-byte through the
store's capture API, and its four source hashes match the committed files.
Output SHA-256:
`1afcdfe53dceec5ad916404e7caba164a736a8203307d4159ab799e0993d87dc`.

The new round-trip unit test is available for future runs. Acceptance here used
the retained direct execution and assertions above. Shared command logic and
the two existing examples were unchanged except for adding the closed selector;
their prior evidence was reused. No broad suite or second mathematical run was
required to publish these results.

## Execution and provenance boundaries

The run used the existing separate unprivileged research account, immutable
committed source and fresh state, limited to two CPU cores and 4 GiB RAM with
no GPU. Unchanged JavaScript build outputs were reused. Existing research
installations were not modified. Publication adds these evidence files and
documentation without changing the executed checker or walkthrough.

This is a fresh computation and an authored store reconstruction. It supplies
no new live-model or autonomous-discovery result. The first example's separate
live-executive evidence remains applicable to that earlier observed execution.
The [provenance](provenance.md) distinguishes the failed historical script,
later research interpretations and this public certificate.
