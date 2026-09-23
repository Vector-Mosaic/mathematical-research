# Provenance and mathematical scope

## Historical research: July 20, 2026

The retained C89 strategy selected a source-defined Mellin interpolation as a
possible route into the Bernstein–Pick class used by Konstantopoulos, Patie and
Sarkar. It required a small falsification test before pursuing a larger
construction. The subsequent C90 research receipt and checker retain the exact
four-node witness published here. CE081 identifies the corresponding scoped
route exclusion.

The receipt reports an Arb enclosure approximately
`[-374.57073357307481, -374.57073357307320]` for the displayed integer vector's
quadratic form. We recovered the receipt and source checker, but not the original
separate stdout. That historical interval is a reported result; the separately
dated public execution supplies fresh computational evidence.

The historical campaign preceded the current public Mission Host. Running its
checker or constructing records with today's core does not retroactively
establish which runtime behavior occurred in July. The private research store,
unrelated claims, transcripts and machine/account bindings are not distributed.

## Public preparation: September 23, 2026

We read the frozen checker and the relevant strategy/result receipts, checked
the analytic tail argument, and adapted the checker for standalone JSON output.
The witness, precision, finite integration region, component cutoff and
quadrature settings are retained. Exact interval endpoints accompany display
strings so readers can inspect the sign without relying on decimal rounding.

The public mathematical note explains the calculation and its consequences.
The store walkthrough supplies explicitly authored candidate revisions and
interpretations, uses the real core to retain them with source dependencies,
and reopens the resulting state. Its judgments are not purported model output.
The new checker execution is documented in [validation.md](validation.md).

Coding agents performed this preparation under Justin's direction. It is not
represented as an independent human mathematical review, formal proof-assistant
verification, a novelty claim, or a measurement of autonomous research success.

## External sources

- Konstantopoulos, Patie and Sarkar,
  [*A new class of solutions to the van Dantzig problem, the Lee–Yang property,
  and the Riemann hypothesis*](https://aif.centre-mersenne.org/item/10.5802/aif.3600.pdf#page=21),
  *Annales de l'Institut Fourier* 74 (2024), printed page 396, equation (4.14)
  and Theorem 4.4: the selected class requires the Pick property.
- Schilling, Song and Vondraček,
  [*Bernstein Functions: Theory and Applications*, second edition](https://doi.org/10.1515/9783110269338),
  Theorems 6.2, 6.9, 7.3 and 12.17: complete Bernstein, Pick, Stieltjes and
  operator-monotone relationships used by the route exclusion.
- [python-flint 0.8.0](https://pypi.org/project/python-flint/0.8.0/) and
  [Arb complex integration](https://python-flint.readthedocs.io/en/latest/acb.html#flint.acb.integral):
  the numerical library and its rigorous integration interface.

The example links these works and supplies its own explanation; the repository's
software license does not relicense the linked publications.
