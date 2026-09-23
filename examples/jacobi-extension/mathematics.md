# From a positive-gap obstruction to a wider Jacobi construction

The result is a precise change in research direction. A fixed finite collection
of positive **short-gap** factors cannot satisfy the stated limiting equations.
Two **wide** factors do have a nonsingular solution, although their cross-junction
offset is negative. Those are different gaps. Exact coefficient identities and a
rational example show that the wider construction is mathematically meaningful.

The calculation below establishes algebra, a conditional local-existence theorem
and a finite positive-rooted comparison. It does not establish an actual
Riemann-xi coefficient match, an enlarged Jensen hyperbolicity region or RH.

## 1. Quotients determine the matched coefficients

Let $R_0=1$ and let all the relevant coefficients $R_j$ be strictly positive.
Define

$$
q_k=\frac{R_{k+1}^2}{R_kR_{k+2}},\qquad
Q_k(U,V)=\frac{(U+k)(V+k+1)}{(V+k)(U+k+1)}.
$$

For $(U)_j=U(U+1)\cdots(U+j-1)$, set

$$
\widehat R_j=S^j
\frac{(U_1)_j(U_2)_jV_2^j}
{U_1^jU_2^j(V_1)_j(V_2)_j},\qquad S=V_1R_1.
$$

Successive ratios immediately give

$$
\frac{\widehat R_{k+1}^2}{\widehat R_k\widehat R_{k+2}}
=Q_k(U_1,V_1)Q_k(U_2,V_2).
$$

The recurrence $R_{k+2}=R_{k+1}^2/(q_kR_k)$ has the explicit solution

$$
R_j=\frac{R_1^j}{\prod_{k=0}^{j-2}q_k^{j-1-k}}\quad(j\ge2).
$$

Thus matching $q_0,q_1,q_2,q_3$, after fixing $R_0,R_1$, matches
$R_0,\ldots,R_5$. The normalizations follow the source paper's
[quotient coordinates and comparison families](https://arxiv.org/html/2608.08682v1#S2).
The checker independently verifies these rational identities.

## 2. Why the short-gap ansatz fails

Consider a long first numerator $aA_J$, a first denominator
$B_J+bn/L$, and a fixed finite number of additional factors with

$$
V_i=t_i n+o(n),\qquad U_i-V_i=c_i n/L+o(n/L),
\qquad t_i,c_i>0.
$$

Here $A_J\sim nL$, $B_J\sim n$ and $L\to\infty$. These are a specific
perturbative architecture, not all possible Jacobi constructions.
For $f_k(U)=\log((U+k)/(U+k+1))$, forward differences give

$$
\Delta^r f_0(U)=(-1)^{r+1}r!U^{-r-1}+O(U^{-r-2}).
$$

Taylor expansion in the small gap consequently gives the contribution

$$
n^{r+1}L\,\Delta^r[f_k(U_i)-f_k(V_i)]_{k=0}
\longrightarrow(-1)^r(r+1)!c_it_i^{-r-2}.
$$

Write $x_i=1/t_i$ and $w_i=c_ix_i^3>0$. The last three target
rows $(0,2,-12)$ of the four-row target $(0,0,2,-12)$ require

$$
b=\sum_iw_i,\qquad
\sum_iw_i(x_i-1)=\frac13,\qquad
\sum_iw_i(x_i^2-1)=\frac12.
$$

Subtracting twice the middle equation from the last yields

$$
\boxed{\sum_iw_i(x_i-1)^2=-\frac16.}
$$

The left side is nonnegative; the right side is negative. This excludes the
stated limiting positive short-gap family, irrespective of the number of
factors provided that number is fixed and finite. The same contradiction holds
eventually if the two target equations have errors tending to zero. It does
not exclude changing the scales, allowing signed gaps or using wide factors.
The target asymptotics are inputs to this implication; the checker does not
derive them from a numerical sample of xi coefficients.

## 3. A wider pair has a full-rank solution

Repartition the long factor using

$$
U_1=tn+\mu n/L,\quad V_1=B_J+bn/L,
\qquad U_2=aA_J,\quad V_2=tn.
$$

At zero cross-junction offset $U_1=V_2$, their internal Pochhammer terms
cancel. A small signed change of that junction need not spoil either
within-factor gap.

Subtract the first-Jacobi quotient, defining

$$
H_{n,k}=f_k(U_1)-f_k(V_1)+f_k(U_2)-f_k(V_2)
-f_k(A_J)+f_k(B_J),
\qquad G_{n,r}=n^{r+1}L\Delta^rH_{n,0}.
$$

The limiting map is

$$
F(a,b,\mu,t)=
\begin{pmatrix}
1-a^{-1}-b+\mu t^{-2}\\
2b-2\mu t^{-3}\\
-6b+6\mu t^{-4}\\
24b-24\mu t^{-5}
\end{pmatrix},\qquad T=\begin{pmatrix}0\\0\\2\\-12\end{pmatrix}.
$$

Solving $F=T$ in $a>0,t>1$ gives

$$
\boxed{x_*=(a,b,\mu,t)=(3,-2/3,-16/3,2).}
$$

To see uniqueness in this limiting region, row 2 gives $b=\mu/t^3$;
row 3 then gives $\mu=t^4/[3(1-t)]$. Substitution in row 4 gives
$4(t-2)/t=0$, and row 1 fixes $a=3$. This is a statement about the
limiting equations, not global uniqueness of all finite-$n$ comparisons.

At $x_*$ the Jacobian and its inverse are

$$
J=\begin{pmatrix}
1/9&-1&1/4&4/3\\0&2&-1/4&-2\\
0&-6&3/8&4\\0&24&-3/4&-10
\end{pmatrix},\qquad\det J=-\frac16,
$$

$$
J^{-1}=\begin{pmatrix}
9&45/2&12&3/2\\0&1/2&2/3&1/6\\
0&-24&-56/3&-8/3\\0&3&3&1/2
\end{pmatrix}.
$$

These are exact rational computations. As a scope check, the next formal row
$120(\mu/t^6-b)$ equals $70$, rather than the proposed next target $72$.
Even assuming that extra target, this pair would leave a defect of $2$;
the example does not claim matching through $R_6$.

## 4. The finite-n conclusion and its explicit assumption

Define the exact target vector by

$$
T_{n,r}=n^{r+1}L\Delta^r
\log\!\frac{q_k}{Q_k(A_J,B_J)}\bigg|_{k=0},\qquad0\le r\le3.
$$

Let $K$ be a fixed small closed ball about $x_*$ in $a>0,t>1$ on which
the finite parameters are positive. **Assume** the exact maps satisfy

$$
\|[G_n-T_n]-[F-T]\|_{C^1(K)}\le\varepsilon_n,
\qquad\varepsilon_n\longrightarrow0.
$$

Also assume $A_J/(nL)\to1$, $B_J/n\to1$ and $L\to\infty$.
These are the analytic-to-algebraic interface conditions. Establishing them
for the intended RH source, on one common domain with controlled constants,
is outside this public certificate.

**Conditional theorem.** For all sufficiently large $n$, there is exactly one
solution in a sufficiently small fixed neighborhood of $x_*$, and
$x_n=x_*+O(\varepsilon_n)$. If $\varepsilon_n=O(L^{-1})$, the same rate
holds for $x_n-x_*$.

For completeness, take $K$ small enough that
$\|I-J^{-1}DF(x)\|\le1/4$ throughout it. The hypothesis makes
$N_n(x)=x-J^{-1}(G_n(x)-T_n)$ a contraction with Lipschitz constant at most
$1/2$ for large $n$. Its displacement at $x_*$ is $O(\varepsilon_n)$,
so it maps the ball into itself. The contraction theorem proves existence,
uniqueness there and the stated rate. The finite-difference change of
coordinates is invertible, so its solution matches all four original
quotients and therefore $R_0,\ldots,R_5$.

At these conditional solutions,

$$
\frac{U_1-V_1}{n}\to1,\qquad
\frac{U_2-V_2}{nL}\to3,\qquad
\frac{U_1-V_2}{n/L}\to-\frac{16}{3}.
$$

The first two are the **actual factor gaps**. The last is the
**cross-junction offset**. When $d=o(n)$, both inequalities
$U_i>V_i+d-1$ hold eventually. The negative junction offset therefore does
not disqualify either factor from the positive-rooted Jacobi range.

## 5. A finite rational construction, with exact roots certified

For any degree $d$ define

$$
p(y)={}_3F_2\!\left(\begin{matrix}-d,U_1,U_2\\V_1,V_2\end{matrix};
\frac{V_2y}{U_1U_2}\right).
$$

Its two factors are

$$
p_1(y)={}_2F_1(-d,U_1;V_1;y/U_1),\qquad
p_2(y)={}_2F_1(-d,U_2;V_2;V_2y/U_2).
$$

Use the constant-term normalization
$p_i(y)=\sum_{j=0}^d(-1)^j\binom dj a_{i,j}y^j$.
Finite-free multiplication gives
$p(y)=\sum_j(-1)^j\binom dj a_{1,j}a_{2,j}y^j$.
This convention agrees, up to the irrelevant common scalar $(-1)^d$,
with the descending-coefficient convention for multiplicative finite-free
convolution. Coefficient expansion verifies the displayed ${}_3F_2$ identity.

The Jacobi relation has parameters $\alpha=V-1$ and $\beta=U-V-d$.
If $V>0$ and $U>V+d-1$, then $\alpha,\beta>-1$, so the roots lie in
the positive transformed interval. Positive-root preservation under convolution
is [Proposition 2.7 in Martínez-Finkelshtein, Morales and Perales](https://arxiv.org/html/2309.10970v3#S2.SS5).

The separate exact witness is

$$
d=5,\qquad(U_1,V_1,U_2,V_2)=(16,8,60,20),\qquad S=1.
$$

Here $R_1=1/8$, the actual gaps are $8$ and $40$, and the junction offset
is $-4$. Jacobi parameters are $(7,3)$ and $(19,35)$. The checker forms
all three degree-five polynomials over $\mathbb Q$, verifies convolution
coefficient by coefficient, then isolates every root in positive rational
intervals. Each nontrivial interval has opposite polynomial signs at its
endpoints; five disjoint intervals exhaust the degree and establish simplicity.

This is an illustrative comparison witness. No claim is made that these
parameters match an actual RH moment sequence or solve its finite-$n$ target.

## 6. The exact differential identity also survives

Let $\mathcal E=y\,d/dy$ and use the cross-pair Jacobi operator

$$
\mathcal Jp=y(1-y/U_2)p''+
\{V_1-y+(d-1)y/U_2\}p'+dp.
$$

Put $\epsilon=(U_1-V_2)/U_1$. The hypergeometric coefficient recurrence gives

$$
\boxed{(\mathcal E+V_2)\mathcal Jp
=-\frac{\epsilon}{U_2}\mathcal E(\mathcal E-d)(\mathcal E+U_2)p.}
$$

Indeed, the ${}_3F_2$ differential operator is

$$
\mathcal H=\mathcal E(\mathcal E+V_1-1)(\mathcal E+V_2-1)
-\frac{V_2y}{U_1U_2}(\mathcal E-d)(\mathcal E+U_1)(\mathcal E+U_2).
$$

On a general monomial, comparing the two possible powers proves
$y^{-1}\mathcal H=(\mathcal E+V_2)\mathcal J+
(\epsilon/U_2)\mathcal E(\mathcal E-d)(\mathcal E+U_2)$.
Since $\mathcal Hp=0$, the boxed identity follows. Its perturbation depends
on the small junction offset, not the wide within-factor gap.

Where $p(y)\ne0$, write $T_m=y^mp^{(m)}(y)/p(y)$ and
$\mathcal U_m=y^m(\mathcal Jp)^{(m)}(y)/p(y)$. Differentiation yields

$$
\mathcal U_{m+1}+(V_2+m)\mathcal U_m=-\epsilon\mathcal V_m,
$$

$$
U_2\mathcal V_m=T_{m+3}+(U_2-d+3+3m)T_{m+2}
+\beta_mT_{m+1}+\gamma_mT_m,
$$

$$
\beta_m=U_2(2m+1-d)-d(2m+1)+3m^2+3m+1,
\qquad\gamma_m=m(m-d)(m+U_2).
$$

The checker verifies the operator and differentiated coefficients as symbolic
identities. These identities alone do not bound derivative ratios at critical
points. No old localization, recurrence or multiplier constants are imported.

## What the certificate settles

The positive short-gap obstruction is real at the explicit targets. The wider
pair has a full-rank limiting solution, positive factor margins, an exact
coefficient construction and a working finite rational instance. Finite-$n$
matching follows from the stated $C^1$ hypothesis by a complete conditional
argument. The remaining analytic hypotheses are neither measured nor certified
by this checker. Later research on larger constructions is outside this
example's audited scope; this note does not declare where the whole research
program currently stands.
