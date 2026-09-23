# C90: a four-point obstruction for a fixed Mellin interpolation

The result is a counterexample to one proposed route: the function defined
below cannot be a Pick or complete Bernstein function. The certificate is a
negative quadratic form of its four-point Loewner matrix, enclosed using Arb
arithmetic and explicit bounds for every omitted integration region.

This does not decide ordinary Bernstein status, rule out another interpolation,
or prove or disprove the Riemann Hypothesis (RH).

## 1. The function and the question

For real `t >= 0`, define

$$
 q_n(t)=\pi n^2e^{4t},\qquad
 \Phi(t)=\sum_{n=1}^{\infty}\pi n^2 e^{5t-q_n(t)}(2q_n(t)-3),
$$

$$
 U(s)=\int_0^\infty t^s\Phi(t)\,dt.
$$

Every source component is positive since `q_n(t) >= pi > 3/2`. The source
is bounded near zero and decays faster than every exponential at infinity.
Consequently `U` is holomorphic for `Re(s)>-1`, and differentiation under the
integral gives

$$
 U'(s)=\int_0^\infty t^s\log(t)\Phi(t)\,dt.
$$

The selected interpolation, on the domain needed by this certificate, is

$$
 f(u)=\left(u-\frac12\right)\frac{U(2u-2)}{U(2u)},\qquad u>\frac12.
\tag{1}
$$

Its denominator is strictly positive for real `u>1/2`, so it is real analytic
there. A normalizing constant multiplying `Phi` would cancel in this ratio.
The historical candidate also used a regularized formula for smaller `u`.
No choice of that extension can repair the obstruction below: every evaluation
and derivative used here lies in the common domain `u>1/2`.

The route under investigation required this fixed function to extend as a Pick
function on the positive half-line. A Pick function is holomorphic on the upper
half-plane and has nonnegative imaginary part there; here it must also extend
analytically across the real interval on which it agrees with (1).

At distinct real nodes its Loewner matrix is

$$
 L_{ii}=f'(u_i),\qquad
 L_{ij}=\frac{f(u_i)-f(u_j)}{u_i-u_j}\quad(i\ne j).
\tag{2}
$$

A necessary condition for a Pick extension is that every such matrix be
positive semidefinite. One way to see the condition is the Herglotz
representation: its affine term contributes a nonnegative constant kernel and
its measure contributes kernels `1/((t-u)(t-v))`. Each is positive semidefinite,
as is their positive integral. The diagonal follows by taking a derivative.
This is the Loewner positivity condition behind the operator-monotone/Pick
correspondence. See Schilling, Song and Vondracek, *Bernstein Functions*,
2nd edition, Chapter 6 and Theorem 12.17 ([book and corrections](https://www.motapa.de/bernstein_functions/)).

Entrywise positive numbers would not suffice: positive semidefiniteness requires
`v^T L v >= 0` for every real vector `v`. One vector with a rigorously negative
quadratic form therefore disproves the necessary condition.

## 2. Exact finite witness

The checker keeps the original nodes and integer vector:

$$
 (u_1,u_2,u_3,u_4)=(1,2,4,6),\qquad
 v=(28336,-66314,64141,-26181).
\tag{3}
$$

It evaluates moments at `s=0,2,4,6,8,10,12`. Differentiating (1) gives

$$
 f'(u)=f(u)\left[
 \frac1{u-1/2}
 +2\left(\frac{U'(2u-2)}{U(2u-2)}-\frac{U'(2u)}{U(2u)}\right)
 \right].
\tag{4}
$$

Only these fourteen moment integrals are required. No estimated eigenvalue,
floating-point determinant or high-order derivative-sign survey is used in the
certificate.

## 3. Finite integration

Substitute `t=exp(x)`. For logarithmic order `r=0,1`, the moment integrand is

$$
 e^{(s+1)x}x^r\Phi(e^x).
\tag{5}
$$

The checker integrates the first five theta components over `-60 <= x <= 1`
with `python-flint==0.8.0` at 70 decimal digits. It uses the consecutive endpoints:
steps of five from `-60` to `-10`, steps of one from `-10` to `0`, and quarter
steps from `0` to `1` (24 intervals in total; the endpoint list has 25 entries).
Both quadrature tolerance goals are `1e-52`; degree, evaluation and depth
limits are respectively 160, 200000 and 40 per interval.

The truncated integrand is entire in `x`: it contains exponentials, integer
powers, sums and products. Ignoring the integration callback's `analytic` flag
is therefore valid here. Arb's returned balls include quadrature error; a
tolerance is a goal, not a replacement for inspecting the enclosure. A
nonfinite result fails the checker. See the official
[integration API](https://python-flint.readthedocs.io/en/latest/acb.html#flint.acb.integral)
and [Arb integration implementation](https://flintlib.org/doc/acb_calc.html).

The finite integral is then enlarged symmetrically by the sum of three analytic
error bounds. These bounds cover the omitted theta terms on the finite interval,
the entire source to its left, and the entire source to its right.

## 4. Analytic completion of the integral

### Uniform source bound and omitted theta components

For `t>=0`,

$$
 0<\Phi_n(t)
 \le 2\pi^2n^4e^{9t-\pi n^2e^{4t}}
 \le 2\pi^2n^4e^{-\pi n^2}.
\tag{6}
$$

The first inequality drops the negative `-3` term. The second follows because
the logarithmic derivative of the exponential is `9-4*pi*n^2*exp(4t)<0`.

For `a>0`, the ratio of successive terms `n^4 exp(-a n^2)` is

$$
 R_n(a)=\left(\frac{n+1}{n}\right)^4e^{-a(2n+1)}.
$$

It decreases with `n`. Whenever `R_N(a)<1`, the geometric comparison gives

$$
 G_N(a):=\frac{N^4e^{-aN^2}}{1-R_N(a)}
 \;\ge\;\sum_{n=N}^{\infty}n^4e^{-an^2}.
\tag{7}
$$

The checker certifies each denominator positive. Set

$$
 M=2\pi^2G_1(\pi),\qquad M_6=2\pi^2G_6(\pi).
$$

Then `Phi(t)<=M`, and the omitted components `n>=6` sum to at most `M_6`,
uniformly for `t>=0`. For `a=s+1>0`, their omitted contribution on the finite
`x` interval is bounded in absolute value by

$$
 E_{\mathrm{theta}}(s,r)
 =M_6\,60^r\frac{e^a-e^{-60a}}a,
 \qquad r\in\{0,1\}.
\tag{8}
$$

Here `|x|<=60`; the absolute bound handles the negative logarithm on `x<0`.

### Left endpoint

The complete source on `x<-60` contributes at most

$$
 E_{\mathrm{left}}(s,0)=M\frac{e^{-60a}}a,
$$

$$
 E_{\mathrm{left}}(s,1)
 =M e^{-60a}\left(\frac{60}a+\frac1{a^2}\right),\qquad a=s+1.
\tag{9}
$$

These follow by integrating `e^(ax)` and `|x|e^(ax)` respectively.

### Right endpoint

For `t>=e`, let `q=pi*exp(4t)` and `q_0=pi*exp(4e)`. Using (7) with `N=1`
and now parameter `q`,

$$
 \Phi(t)\le\frac{2\pi^2e^{9t-q}}{1-16e^{-3q}}
 \le\frac{2\pi^2e^{9t-q}}{1-16e^{-3q_0}}.
$$

Also `log(t)<=t<=q`, `dt=dq/(4q)` and `e^(9t)=(q/pi)^(9/4)`. Thus for
`alpha=s+r+5/4`,

$$
 E_{\mathrm{right}}(s,r)
 =\frac{2\pi^2}{4\pi^{9/4}(1-16e^{-3q_0})}
   \frac{q_0^{\alpha}e^{-q_0}}{1-\alpha/q_0}
\tag{10}
$$

bounds the omitted moment tail. The checker verifies `alpha<q_0`. For
completeness, writing `q=q_0+y` and applying
`(1+y/q_0)^alpha <= exp(alpha*y/q_0)` proves the bound on the incomplete
gamma integral used in (10).

Equations (8)--(10) cover disjoint omitted regions. Their upper bounds are
added as a zero-centered error ball to the finite quadrature enclosure, and
all subsequent arithmetic remains in Arb balls.

## 5. Decision and its exact scope

The program requires every denominator moment positive, every matrix entry
radius below `1e-8`, and the **upper endpoint** of `v^T L v` strictly negative.
Only then does it emit `status: "certified"`. A failure to obtain a sufficiently
sharp enclosure is a checker failure, not proof that the matrix is positive.

Successful output establishes that this `L` is not positive semidefinite.
Therefore no Pick function can agree analytically with (1) on `u>1/2`.
In particular, the historical candidate cannot be complete Bernstein or belong
to KPS's `BP1` class. That class requires the Pick property as well as its other
conditions; its definition is equation (4.14), followed by Theorem 4.4, in
[Konstantopoulos, Patie and Sarkar (2024), p. 396](https://aif.centre-mersenne.org/item/10.5802/aif.3600.pdf#page=21).

It also excludes positive Stieltjes representations of `f(u)/u` and `1/f(u)`.
Indeed, if `f(u)/u` were Stieltjes, then
`f(u)=a+bu+integral u/(u+t) rho(dt)` with nonnegative data, whose Loewner
kernel is the sum of a nonnegative constant and the positive kernels
`t/((u+t)(v+t))`. If `1/f` were Stieltjes, its reciprocal would be Pick.
These are also the complete-Bernstein equivalences in Schilling, Song and
Vondracek, Theorems 6.2 and 7.3 ([2nd edition](https://doi.org/10.1515/9783110269338)).

The certificate does **not** establish a failure of ordinary Bernstein
derivative signs. Nor does it exclude a different interpolation with the same
integer values: (2) also uses derivatives. It leaves other van Dantzig
constructions, other Mellin methods, the broader research program and RH
undecided. It establishes a precise reason to stop pursuing this particular
Pick/complete-Bernstein route.

## 6. Reading the machine output

Every JSON bound is an exact dyadic number: the string `mantissa` multiplied
by `2**exponent2`. Lower endpoints are rounded toward minus infinity and upper
endpoints toward plus infinity before serialization. This avoids changing the
certificate through ordinary decimal rounding. The `display` strings are for
human reading and are not used in the decision. The documented operations
[`lower`, `upper` and `man_exp`](https://python-flint.readthedocs.io/en/latest/arb.html#flint.arb.man_exp)
also exist in the pinned
[python-flint 0.8.0 source](https://github.com/flintlib/python-flint/blob/0.8.0/src/flint/types/arb.pyx).

The output records moment enclosures, all three tail bounds for each moment,
function values and derivatives, the full matrix, the quadratic form, numerical
configuration, dependency versions, source hashes and execution time. It is a
new execution of the checker; it does not authenticate an unrecovered historical
stdout file. The evidence still depends on the written derivation and on the
correctness of the numerical software. It is not a proof-assistant artifact.
