# A missing scale assumption, and a repaired root bound

The displayed coarse assumptions do **not** imply a uniform root-localization
bound. Positive, simple roots survive. Adding `B/D <= 4` gives an explicit
all-degree repair. This document proves those statements; `check.py` supplies
exact computational witnesses and finite consistency checks.

## The question

For a positive integer `d` and positive real parameters `A,B,C,D`, set

\[
p_F(y)=\sum_{j=0}^d(-1)^j\binom dj
 \frac{(A)_j(C)_jD^j}{A^jC^j(B)_j(D)_j}y^j,
\]

where `(x)_j` is the rising factorial. Fix `K_r>=32` and consider

\[
A\ge8B,\qquad B,D\ge K_r d,\qquad 4d\le C-D\le D/4. \tag{1}
\]

Does a fixed constant `C_loc` bound every root by
`|y-B| <= C_loc sqrt(Bd)` under these conditions alone?

The model and the question come from equation (69), conditions (57) and Lemma
7.3 of [Holland, arXiv:2608.08682v1](https://arxiv.org/html/2608.08682v1#S7).
The proof there additionally invokes comparability of `D` and `B`. We examine
the displayed assumptions as a standalone statement. We make no claim here
about the validity of the paper's main theorem or about novelty of this issue.

## 1. An exact counterfamily

Fix any `K_r >= 32`, and set

\[
d=2,\quad D=2K_r,\quad C=D+8,\quad A=8B,\quad B\ge2K_r.
\]

All of (1) hold, while `B/D` can grow without bound. Direct expansion gives

\[
p_F(Bz)=1-2z+q_Bz^2,
\qquad
q_B=\frac{8B+1}{8B+8}\,q_0,
\qquad
q_0=\frac{D(C+1)}{C(D+1)}
    =1-\frac8{C(D+1)}.
\]

Thus `0 < q_B < q_0 < 1`. Write `s_B = sqrt(1-q_B)` and
`s_0 = sqrt(1-q_0) > 0`. The two distinct positive roots are

\[
y_-=\frac{B}{1+s_B},\qquad y_+=\frac{B}{1-s_B}.
\]

In particular,

\[
\frac{B-y_-}{\sqrt{2B}}
=\sqrt{\frac B2}\frac{s_B}{1+s_B}
\ge \sqrt{\frac B2}\frac{s_0}{1+s_0}
\longrightarrow\infty.
\]

This disproves the existence of **any** absolute localization constant under
(1), even after fixing any arbitrarily large `K_r` once and for all. Trying a
larger constant without controlling `B/D` cannot repair the statement.

### A small exact computation

Take `K_r=144`, so `D=288`, `C=296` and `1-q_0=1/10693`.
Since `s_0>1/104`, the lower root satisfies `B-y_->B/105`.
For an integer `L>=1`, choose `t=106L`, `B=2t^2`, `A=8B`.
Then `sqrt(Bd)=2t` and

\[
\frac{B-y_-}{\sqrt{Bd}}>\frac{t}{105}>L.
\]

The checker instantiates `L=1,10,100,1000`. It builds the polynomial using its
coefficient recurrence and uses exact rational signs and Sturm counts to locate
a root below `B-L sqrt(Bd)`. It does not approximate a root with floating point.

## 2. What remains valid

Define

\[
q_{U,V}(y)=\sum_{j=0}^d(-1)^j\binom dj
 \frac{(U)_j}{U^j(V)_j}y^j.
\]

Then

\[
p_1(y)=q_{A,B}(y),\qquad p_2(y)=q_{C,D}(Dy),
\qquad p_F=p_1\boxtimes_dp_2. \tag{2}
\]

Here `boxtimes_d` multiplies the normalized coefficients: coefficients
`(-1)^j binom(d,j) a_j` and `(-1)^j binom(d,j) b_j` produce
`(-1)^j binom(d,j) a_j b_j`. Equation (2) follows coefficient by coefficient.

The standard [Jacobi hypergeometric identity](https://dlmf.nist.gov/18.5#E7)
identifies `q_(U,V)` with a nonzero multiple of

\[
P_d^{(V-1,U-V-d)}(1-2y/U).
\]

If `U>=V+d` and `V>0`, both Jacobi parameters exceed `-1`. Its `d` zeros are
distinct and in `0<y<U`. These hypotheses hold for both factors in (2) under
(1). Finite-free convolution preserves nonnegative roots, and its logarithmic
mesh preserves simplicity. The constant and leading coefficients are nonzero,
so there is no zero root or degree loss. For `d>=2`, this proves that `p_F`
has `d` distinct positive roots under the coarse assumptions. The remaining
case is direct: when `d=1`, `p_F(y)=1-y/B` has its single positive root at `B`;
no logarithmic-mesh statement is needed.

The external results used here are Propositions 2.7(iii) and 2.17 in
[Martinez-Finkelshtein, Morales and Perales, arXiv:2309.10970v3](https://arxiv.org/html/2309.10970v3#S2.SS5).
Our constant-term normalization converts to their monic normalization by
`p^vee(x)=x^d p(1/x)`, and reversal commutes with convolution.

## 3. A general repaired theorem

**Theorem.** Suppose (1) holds with `K_r=144`, and also `B/D<=4`. Every root
and every critical point of `p_F` satisfies

\[
\boxed{|y-B|<10\sqrt{Bd},\qquad
       \frac9{16}B\le y\le\frac{25}{16}B.} \tag{3}
\]

All roots are distinct and positive. When `d=1`, there are no critical points.
The constant `10` is a deliberately simple bound derived for this public
example. The historical output proposed a sharper constant `9`; we do not
attribute this new proof to that run or rely on its sharper estimate.

### A ratio-independent Jacobi estimate

We first show that if `U>=V+d` and `V>=32d`, then

\[
\operatorname{roots}(q_{U,V})
 \subset [V-3\sqrt{Vd},\ V+3\sqrt{Vd}]. \tag{4}
\]

Put `H=U-d-1`, `alpha=V-1`, `beta=U-V-d`. The symmetric Jacobi matrix,
transported from its usual variable by `y=U(1-x)/2` and with alternating signs
conjugated away, has diagonal entries, for `0<=k<d`,

\[
h_k=U\frac{H(V+2k)+2k(k+1)}{(H+2k)(H+2k+2)},
\]

and off-diagonal entries, for `1<=k<d`,

\[
e_k=\frac{U}{H+2k}
 \sqrt{\frac{k(k+\alpha)(k+\beta)(k+H)}
 {(H+2k-1)(H+2k+1)}}.
\]

These follow from the [Jacobi recurrence](https://dlmf.nist.gov/18.9).
The matrix eigenvalues are exactly the zeros in (4). The checker separately
compares the determinant recurrence against the hypergeometric coefficients
in the stated finite cases; the recurrence identity itself is standard.

Subtracting `V` from `h_k` yields the exact numerator

\[
2k(k+H+1)(U-2V)+VH(d-1)
\]

over the same denominator. Now `H>=31d`, `|U-2V|<=U`,
`U/H<=33/31`, `V/H<=32/31` and `(k+H+1)/H<=32/31`. Hence

\[
|h_k-V|\le\left(2\frac{32}{31}\frac{33}{31}
                     +\frac{32}{31}\right)d
=\frac{3104}{961}d<\frac{13}{4}d.
\]

For the off-diagonal entries, `0<=beta<=H`. Because `k>=1`,

\[
\frac{(k+\beta)(k+H)}{(H+2k-1)(H+2k+1)}\le1,
\qquad k+\alpha\le V+d\le\frac{33}{32}V.
\]

Together with `k<=d` and `U/(H+2k)<=33/31`, this gives

\[
e_k\le\frac{33}{31}\sqrt{\frac{33}{32}}\sqrt{Vd}
<\frac98\sqrt{Vd}.
\]

Each row has at most two off-diagonal entries. Since
`sqrt(d/V)<=1/sqrt(32)<3/16`, its Gershgorin interval is within

\[
\frac{13}{4}d+\frac94\sqrt{Vd}
<\frac{183}{64}\sqrt{Vd}<3\sqrt{Vd}
\]

of `V`. This proves (4) for all the indicated real parameters and degrees.

### Transfer to the convolution

For positive-rooted factors, finite-free convolution places every root in
`[u_- v_-, u_+ v_+]` when the factors' root intervals are `[u_-,u_+]` and
`[v_-,v_+]`. One way to obtain this fact is to compare the second monic factor
with `(x-v_-)^d` and `(x-v_+)^d` in coordinatewise root order. Moving roots one
at a time gives weakly interlacing pairs. Proposition 2.11 of the same
[version-3 finite-free paper](https://arxiv.org/html/2309.10970v3#S2.SS5)
preserves their interlacing direction under convolution, while convolution
with `(x-v)^d` scales all roots by `v`. Reversing polynomials gives the stated
interval for the constant-term normalization too.

Apply (4) to both factors. Define

\[
a=3\sqrt{d/B},\qquad b=3\sqrt{d/D}.
\]

Their roots lie respectively in `B[1-a,1+a]` and `[1-b,1+b]`.
Both `a,b<=1/4`, so the product interval gives
`9B/16<=y<=25B/16` immediately. Moreover,

\[
|y-B|\le B(a+b+ab).
\]

After division by `sqrt(Bd)`, the three terms are bounded by

\[
3,\qquad3\sqrt{B/D}\le6,\qquad
9\sqrt{d/D}\le\frac34.
\]

Their sum is `39/4<10`, proving the root part of (3). The distinct positive
roots proved above have exactly one derivative root between each consecutive
pair by Rolle's theorem. Those `d-1` points exhaust the derivative's degree,
so they satisfy the same interval and localization bounds.

This proof actually needs only `A>=B+d`, `C>=D+d`, `B,D>=144d` and `B/D<=4`.
We stated the repair with the original coarse conditions to make the single
additional hypothesis visible. No optimal constant is claimed.

## What running the checker establishes

From the repository root, run:

```sh
python examples/finite-free-localization/check.py
```

It emits JSON and fails with a nonzero exit if a check fails. It uses only the
Python standard library. The checks cover four explicit counterexamples,
rational inequalities used for the proof constants, and six corrected cases
of degrees `1,2,3,5,8,12`. The finite cases compare two coefficient
constructions and use Sturm counts for every root and critical point in the
claimed intervals.

Those computations establish the listed finite facts. The all-degree repair
rests on the argument above and its identified Jacobi and finite-free
theorems. This example establishes no result about all Jensen polynomials,
the full source paper, RH, or a general research-system success rate.
