# A refinement produced by the live successor

During the September 23, 2026 live demonstration, the successor retrieved the
previous correction and proposed the following sharper bound using the supplied
[baseline proof](mathematics.md). During publication preparation, another coding
agent checked this narrow consequence against that proof and verified the
displayed constants with exact rational arithmetic.
This is a reference-assisted refinement, not an independent discovery of the
original issue or authentication of the historical proposed constant 9.

The original prepared proof and checker retain their constant 10. This note is
a subsequent result; it was not part of the successor's original input.

## The sharper estimate

The baseline Jacobi argument gives

\[
|h_k-V|<\frac{13}{4}d,
\qquad e_k<\frac98\sqrt{Vd}.
\]

Under `U>=V+d` and the stronger scale condition `V>=144d`, a Gershgorin row has
radius, relative to `V`, strictly less than

\[
\left(\frac{13}{4}\sqrt{\frac dV}+\frac94\right)\sqrt{Vd}
\le c\sqrt{Vd},\qquad c:=\frac{121}{48}.
\]

Thus every zero of `q_(U,V)` lies strictly between `V-c sqrt(Vd)` and
`V+c sqrt(Vd)`. As in the baseline, suppose

\[
A\ge B+d,\quad C\ge D+d,\quad B,D\ge144d.
\]

Put `a=c sqrt(d/B)` and `b=c sqrt(d/D)`. Both are at most
`121/576<1/4`. The factor roots lie strictly within `B(1-a,1+a)` and
`(1-b,1+b)`. The finite-free product bound, applied to their actual minimum and
maximum roots, therefore places every root of `p_F` strictly inside

\[
\bigl(B(1-a)(1-b),\ B(1+a)(1+b)\bigr).
\]

The endpoints remain strictly positive even when one of the parameter
inequalities is an equality. Consequently,

\[
|y-B|
<\sqrt{Bd}\left[c\left(1+\sqrt{\frac BD}\right)
                   +c^2\sqrt{\frac dD}\right].
\]

Adding `B/D<=4` bounds the bracket by

\[
3c+\frac{c^2}{12}
=\frac{223729}{27648}<9.
\]

This proves `|y-B|<9 sqrt(Bd)` under the corrected assumptions. The baseline's
positive, simple roots and Rolle argument also give this bound for every
critical point. For `d=1`, directly `p_F(y)=1-y/B`: its root is `B`, and it has
no critical point. No logarithmic-mesh assertion is needed in that case.

The stricter factor intervals also remain within `[9B/16,25B/16]`, so the
baseline's coarse positive interval is preserved.

## Scope

The argument uses the same Jacobi recurrence and finite-free interval and
root-preservation results identified in the baseline. Exact arithmetic confirms
the constants; it does not replace those external theorems or formalize the
proof. The refinement establishes no novelty claim, no general research-system
success rate, no conclusion about the full source paper, and no result on RH.
