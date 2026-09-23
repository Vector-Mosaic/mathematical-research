# Jensen example: recorded validation

On September 23, 2026, the checker and authored real-store walkthrough passed
from committed source
[`1232a53076f1188585241b0622cb35ea74932d62`](https://github.com/Vector-Mosaic/mathematical-research/commit/1232a53076f1188585241b0622cb35ea74932d62).
Execution used Python 3.12.3, python-flint 0.8.0 and SymPy 1.14.0 on Linux,
with Arb at 60 decimal digits and one arithmetic thread.

## Finite mathematics

The [checker output](checker-output.json) records the rational input geometry,
exact dyadic interval endpoints and successful comparisons. In this authored
configuration:

| Quantity | Approximate value for reading |
| --- | --- |
| Total retained mass | 0.1095323294 |
| Complementary mass | 0.0719055606 |
| Terminal atom, retained across three bins | 0.0143615957 |
| Coarse complementary-mass charge | 0.0078759835 |
| Bin-local charge | 0.0015221147 |
| Proven bin-local cap for this fixture | 0.0029415911 |

The strict comparisons use interval enclosures, not the rounded numbers above.
The checker also verified:

- Exact rational partitions retain every cell and complementary interval.
- Forty-nine rational CDF probes agree with the direct pushforward calculation;
  exact-zero cases use the structural crossing-cell expression. The general
  all-parameter CDF bound is proved in the mathematical note.
- Affine energy is bounded by the positive pair energy and complete Fejer
  budget. The triangular characteristic is genuinely complex, exercising the
  distinction between its value and its modulus.
- Nonlinear phase and moving-weight comparisons retain a nonzero phase cost,
  nonzero weight cost and the full cross term. The full energy bound passes.
- The general triangular second-moment identity, critical exponents and degree
  headroom identity hold. An arbitrary-coefficient symbolic check at `d=5,k=3`
  supplements the general Jensen derivative proof.

The [mathematical note](mathematics.md) supplies the general finite argument
and conditional scale calculation. A separate coding-agent review examined
the measure, endpoint, periodization, variation and Hilbert steps. Preparation
made the nonnegative CDF domain, vanishing-scale assumptions, weight-motion
condition and uniformity needed for the endpoint choice explicit.
This was not independent human review or proof-assistant verification.

These checks certify the supplied finite fixture and identities. They do not
establish the actual-source estimates or the larger Jensen theorem. The fixture
is deliberately small and does not claim that its numerical bound is sharp.

## Durable research behavior

The real core executed the equivalent of:

```sh
python scripts/research_example.py run --example jensen-program --output .research/jensen
python scripts/research_example.py inspect --output .research/jensen --details
```

Inspection ran in a new process. The [selected records](store-summary.json)
show six exact raw artifacts and four Candidate revisions: finite mixing
before and after its scoped support, the open analytic inputs, and the
conditional budget. The latter refers to the exact revised finite Candidate,
analytic-input Candidate and Evidence, with their stored content digests.
Checkpoint project commit 11 survived reopening; canonical effect remained
`none`.

The finite result remains `open` as a non-admitted Candidate with an explicit
supporting basis. The analytic input remains open because this package does not
prove it. Those statuses carry different mathematical meanings. All judgments
were authored fixture inputs; the store did not infer them from the checker.

The checker ran once. Its exact output was retrieved through the store's raw
capture API, and all retained source hashes matched the committed files.
Checker-output SHA-256:
`88a48f0f5200d920dc19e87efb3e7f2372ac3d3c18f050d826e4964109e9c4e2`.

The shared command changed only by adding this closed example selector. Prior
evidence for the unchanged runtime and overwrite/selector protections was
reused. There was no new live model run, broad test suite or CI build. Execution
used the existing separate unprivileged runtime, limited to two CPU cores and
4 GiB RAM with no GPU; the existing research and training installations were
preserved. Only the prepared public source and a selected evidence projection
are published.
