# Where this example came from

This is a curated reproduction of an actual research episode, with a new
checkable exposition. It is not a raw research transcript or an untouched
historical output.

## Historical work: August 27, 2026

The private research system retained three relevant outputs:

| Time (UTC) | Recorded mathematical progression |
|---|---|
| 01:19:59 | An explicit degree-two family satisfied the coarse assumptions while its roots escaped every fixed `sqrt(Bd)` localization bound. The output identified missing control of `B/D`. |
| 01:35:41 | A longer synthesis retained the factorization and positive-root argument, and proposed a repaired bound after adding `B/D<=4`. Its proposed localization constant was `9`. |
| 02:02:29 | Work in a later executive epoch explicitly reused a degree-two regression and the added comparability condition in downstream reasoning. |

These facts come from retained captured outputs and their capture metadata,
reviewed privately during preparation. The public account is a paraphrase;
the private source store, unrelated research and account or machine details
are not included. The public repository does not let a reader independently
authenticate the complete private execution history.

Historical cross-epoch reuse shows that the correction appeared in later work.
It does not by itself establish that the later agent had no access to an old
transcript. Any fresh demonstration of retrieval from persistent state must
document and observe that separate execution condition.

The historical downstream output also contained broader mathematical claims.
Those are outside this example. Recording a claim in the research system did
not certify its truth.

## Public preparation: September 23, 2026

For this example we:

1. Read the three actual outputs and the cited version of the external paper.
2. Re-derived the exact degree-two obstruction.
3. Derived a simpler all-degree repair with constant **10**, using the added
   `B/D<=4` condition and `B,D>=144d`. This is distinct from the historical
   proposed constant 9 and does not depend on that sharper estimate.
4. Wrote a dependency-free exact checker for explicit witnesses, polynomial
   identities, proof constants and six finite corrected cases.

`problem.json` contains the research question and definitions. It supplies no
purported agent-generated answer. `mathematics.md` contains the prepared
derivation and limitations. `check.py` computes its own output; a credential-free
store walkthrough consuming those files is a supplied worked example, not a
fresh model discovery. Real model executions must be labeled separately.

The preparation was performed by coding agents with Justin directing the
selection and publication scope. It is not represented as an independent
human mathematical review, a formal proof-assistant verification or a novelty
claim.

## External sources and attribution

- Jonathan Holland,
  [*A new hyperbolicity wedge and a joint semicircle limit for Jensen polynomials of Riemann's xi-function*, arXiv:2608.08682v1](https://arxiv.org/html/2608.08682v1),
  submitted August 9, 2026: source model and the precise standalone localization
  question. The version is pinned because a later version can differ.
- Andrei Martinez-Finkelshtein, Rafael Morales and Daniel Perales,
  [*Real roots of hypergeometric polynomials via finite free convolution*, arXiv:2309.10970v3](https://arxiv.org/html/2309.10970v3):
  external root-preservation, interlacing and logarithmic-mesh results.
- [NIST Digital Library of Mathematical Functions, sections 18.5 and 18.9](https://dlmf.nist.gov/18.5):
  standard Jacobi identities and recurrences.

We provide links and an independently written derivation, not copies of the
papers. Their authors receive credit for the source mathematics; this
repository's software license does not relicense the linked works.
