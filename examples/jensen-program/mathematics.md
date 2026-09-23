# A finite local-mass and phase-mixing theorem

This note reconstructs one finite mechanism from the retained Jensen research.
It proves a statement about arbitrary finite interval geometry and Fourier
amplitudes. Applying it to the actual theta source requires additional analytic
estimates. The accompanying checker exercises an explicitly authored finite
example; its successful computation does not certify those estimates or the
larger Jensen candidate.

## 1. Geometry and the statement

Let

\[
0<t<V_1<U_1<\cdots<V_s<U_s,\qquad B=V_1\le2t,
\quad c=B/t.
\]

The occupied set is \(\mathcal O=\bigcup_{i=1}^s(V_i,U_i)\).
Its complement \(\mathcal C\) is taken in \([t,\infty)\). Use **full** prefixes

\[
S_i=\sum_{j\le i}(U_j-V_j),\quad v_i=S_i/t,\quad
z_i=B/U_i,\quad z_{s+1}=0.
\]

For \(0<a<1/2\), set

\[
C_i=[a,1-a]\cap(z_{i+1},z_i],\quad
w(z)=\frac z{4-2z},\quad D=\bigcup_i C_i.
\]

Take a finite partition of \(D\), up to Lebesgue-null sets, by physical
interval bins \(I\) of width at most \(h_{\rm bin}\). Partition **before**
pushforward: different bins may subsequently put mass at the same point.
Write

\[
p_{Ii}=\int_{I\cap C_i}w(z)\,dz,\quad M_I=\sum_i p_{Ii},
\quad M=\sum_I M_I\le\tfrac12,\quad
H_I=\max_i|I\cap C_i|.
\]

Zero-mass bins can be omitted. The top part of \([a,1-a]\) above \(z_1\)
is uncovered and is **outside this measure**. A separate top-omission estimate
is necessary in the research application. All partial cells and the terminal
cell remain inside the present measure.

For positive integers \(K,N\), define

\[
H_K(u)=\left|K^{-1}\sum_{r=0}^{K-1}e^{2\pi iru}\right|^2,
\qquad C_K=\frac{2\pi}{\sqrt3}\sqrt{1-K^{-2}},
\]
\[
V_*=c(a^{-1}-1),\qquad
\psi(v)=\min\left\{\frac{1-a}{1+a},\frac c{c+2v}\right\},
\]

where \(\psi\) is used only for \(v\ge0\).
\[
T_{N,c}=\sum_{n=1}^N\sum_{0\le k<\lceil nV_*\rceil}\psi(k/n)
\le N+\frac{cN(N+1)}4\log(2/a-1).
\]

The complementary pushforward mass in bin \(I\), defined precisely below,
is \(d_{C,I}\). The finite affine-energy bound is

\[
Q_{\rm bin}=\frac{NM}{K}+N\sum_I M_I d_{C,I}
  +C_KT_{N,c}\sum_I M_IH_I. \tag{1}
\]

In particular, **without dropping any atoms**,

\[
\sum_I M_I d_{C,I}\le\max_I M_I\,d_C
\le h_{\rm bin}\frac{1-a}{2(1+a)}d_C,
\qquad d_C=\sum_I d_{C,I}. \tag{2}
\]

The proof of (1), including its interface to perturbed phases, follows.

## 2. Keep the continuous source and its complementary atoms

Define occupied cumulative length

\[
A(X)=t^{-1}\int_B^X\mathbf1_{\mathcal O}(x)\,dx.
\]

Then \(A(U_i)=v_i\). In particular, restricting to a late bin does not erase
earlier occupied intervals from its phase. Under \(z=B/X\),

\[
w(z)|dz|=q(X)\,dX,\qquad
q(X)=\frac{B^2}{2X^2(2X-B)}.
\]

For \(D_I=I\cap D\), compare two measures of the same mass \(M_I\):

\[
\nu_I=\sum_i p_{Ii}\delta_{v_i},\qquad
\Lambda_I=A_*\bigl(q(X)\mathbf1_{\{B/X\in D_I\}}\,dX\bigr).
\]

Split \(\Lambda_I=\rho_I+\alpha_I\) using occupied and complementary physical
intervals, respectively, and put \(d_{C,I}=\alpha_I(\mathbb R)\).
The occupied part has a density. Each complementary interval collapses to its
constant plateau value under \(A\). A physical bin boundary may split that
interval, placing portions of the **same atom** in several bins. These portions
add to the original mass because the bins partition before pushforward.
The terminal interval \([U_s,\infty)\) behaves in exactly this way.

Since \(q(X)\le B/(2X^2)\) for \(X\ge B\),

\[
d_C\le\frac B2\int_{\mathcal C}X^{-2}\,dX
=\frac c2m_0\le m_0,
\qquad m_0=t\int_{\mathcal C}X^{-2}\,dX. \tag{3}
\]

On occupied intervals, \(A'=1/t\) and their velocity images have disjoint
interiors. The inequality \(A(X)\le(X-B)/t\), and the decreasing function
\(q\), give

\[
0\le\frac{d\rho_I}{dv}(v)\le tq(B+tv)
=\frac1{2c(1+v/c)^2(1+2v/c)}=:h_c(v).
\]

Here \(h_c\) decreases, \(h_c(0)=1/(2c)\le1/2\), and substitution or
partial fractions gives
\(\int_0^\infty h_c(v)\,dv=\log2-1/2\).
For \(x\in[0,1)\), the decreasing-function sum bound yields

\[
\frac1n\sum_{k\ge0}h_c((x+k)/n)
\le\frac{h_c(0)}n+\int_0^\infty h_c(v)\,dv\le\log2<1.
\]

Thus the periodized occupied density is at most one. Because
\(\int_0^1H_K=1/K\), for any real \(y\),

\[
\int H_K(n(v-y))\,d\rho_I(v)\le1/K.
\]

For atoms use only \(0\le H_K\le1\), obtaining an additional
\(d_{C,I}\). Therefore, with
\(E_n(\mu)=\iint H_K(n(v-y))\,d\mu(v)d\mu(y)\),

\[
E_n(\Lambda_I)\le M_I/K+M_Id_{C,I}. \tag{4}
\]

There is no assumed density bound for atomic mass.

## 3. Exact local CDF and quadrature error

Let \(\tau_I=\nu_I-\Lambda_I\) and use the right-continuous CDF
\(F_I(v)=\tau_I(( -\infty,v])\). For \(z\in C_i\),
\(A(B/z)\ge v_i\). Consequently,

\[
F_I(v)=\sum_{i:v_i\le v}\int_{I\cap C_i\cap\{A(B/z)>v\}}w(z)\,dz. \tag{5}
\]

The strict inequality in (5) matters: at a plateau value both CDFs already
include that atom, so its contribution to their difference cancels. Its mass
has not vanished from \(\alpha_I\) or (4).

For \(i<s\), a nonzero contribution requires \(v_i\le v<v_{i+1}\).
The strictly increasing full prefixes mean that at most one original cell
contributes. On the terminal cell, \(A(B/z)=v_s\) and the CDF error is zero.
At a contributing point,

\[
v<A(B/z)\le c(z^{-1}-1),\qquad z\le1-a,
\]

so \(w(z)\le\psi(v)/2\). Thus

\[
0\le F_I(v)\le H_I\psi(v)/2\qquad(0\le v\le V_*). \tag{6}
\]

Both measures are supported in \([0,V_*]\), and their equal masses give
\(F_I(V_*)=0\). Endpoint atoms are included by extending the CDF by zero
outside its support. Signed-measure integration by parts then gives

\[
\int f\,d\tau_I=-\int_0^{V_*}F_I(v)f'(v)\,dv. \tag{7}
\]

To control variation, center the frequencies:

\[
D_K(u)=K^{-1}\sum_{r=0}^{K-1}e^{2\pi i(r-(K-1)/2)u},\quad H_K=|D_K|^2.
\]

Orthogonality of frequency differences gives
\(\|D_K\|_2^2=1/K\) and
\(\|D_K'\|_2^2=\pi^2(K^2-1)/(3K)\). Hence

\[
\int_0^1|H_K'|\le2\|D_K\|_2\|D_K'\|_2=C_K.
\]

This includes \(K=1\). Every interval of length \(1/n\) contributes at
most \(C_K\) to the variation of \(f(v)=H_K(n(v-y))\), uniformly in \(y\).
Using decreasing \(\psi\), (6) and (7), including the last partial interval,

\[
\sup_y\left|\int H_K(n(v-y))\,d\tau_I(v)\right|
\le\frac{H_IC_K}2\sum_{k<\lceil nV_*\rceil}\psi(k/n). \tag{8}
\]

Tensor telescoping is exact:

\[
\nu_I\otimes\nu_I-\Lambda_I\otimes\Lambda_I
=\tau_I\otimes\nu_I+\Lambda_I\otimes\tau_I.
\]

The symmetric kernel and masses \(M_I\) give a factor \(2M_I\), canceling
the factor \(1/2\) in (8). Combine with (4) and sum over \(I,n\) to get (1).
Finally, decreasing \(\psi\) gives

\[
\sum_{k<\lceil nV_*\rceil}\psi(k/n)
\le1+n\int_0^{V_*}\psi(v)\,dv
\le1+\frac{nc}2\log(2/a-1),
\]

which proves the displayed upper bound for \(T_{N,c}\).
The density maximum on the clip is \((1-a)/(2(1+a))\), proving (2).

The retained research calls this local-mesh expression \(Q_{\rm bin}^{\sharp}\)
or \(Q_{\rm sharp}\); \(Q_{\rm bin}\) throughout this note denotes that same
sharper expression, not the coarser global-mass alternative.

## 4. The complex triangular characteristic and Hilbert perturbation

Let \(\alpha_h=\#\{(r,u):0\le r,u<K,\ r+u=h\}/K^2\),
\(0\le h\le2K-2\). These are normalized nonnegative sample weights.
For any real initial phases \(s_i\), the triangular characteristic is

\[
\sum_h\alpha_he^{2\pi ihu}
=\left(K^{-1}\sum_{r=0}^{K-1}e^{2\pi iru}\right)^2. \tag{9}
\]

It is generally complex; its **modulus**, not its value, equals \(H_K(u)\).
Expand the squared affine amplitude within each bin and bound each pair by
this modulus. Initial phase factors have modulus one, giving

\[
\sum_h\alpha_h\sum_I\sum_{n=1}^N
\left|\sum_i p_{Ii}e^{2\pi in(s_i+hv_i)}\right|^2
\le\sum_{I,n}E_n(\nu_I)\le Q_{\rm bin}. \tag{10}
\]

There is no cross-bin tensor, normalization divisor or bin-count multiplier.

For arbitrary real sample phases \(S_i(h)\), write

\[
\Delta_i(h)=S_i(h)-s_i-hv_i,\quad
D_I(h)=\sum_i p_{Ii}|\Delta_i(h)|,\quad
\mathcal D^2=\sum_h\alpha_h\sum_ID_I(h)^2,\quad
Z_N=\frac{N(N+1)(2N+1)}6.
\]

For each amplitude the Lipschitz inequality
\(|e^{ix}-e^{iy}|\le|x-y|\) bounds its change by \(2\pi nD_I(h)\).
Use the Hilbert norm with measure \(\alpha_h\) and **unnormalized counting**
over bins and frequencies. Minkowski and (10) give the frozen-weight bound

\[
\|A\|^2\le\left(\sqrt{Q_{\rm bin}}+2\pi\sqrt{Z_N\mathcal D^2}\right)^2.
\]

Allow moving weights \(p_{Ii}(h)\ge0\) on the same indices, with each sample
total at most \(1/2\), and define

\[
\bar W=\sum_h\alpha_h\sum_{I,i}|p_{Ii}(h)-p_{Ii}|.
\]

Both fixed and moving bin amplitudes have modulus at most \(1/2\).
The difference of their squared moduli is bounded by the sum of the absolute
weight changes in that bin. Summing yields the complete result

\[
M_{\rm mix}\le N\bar W+
\left(\sqrt{Q_{\rm bin}}+\sqrt{D_{\rm phase}}\right)^2,
\qquad D_{\rm phase}=4\pi^2Z_N\mathcal D^2. \tag{11}
\]

In particular, the cross term \(2\sqrt{Q_{\rm bin}D_{\rm phase}}\) uses
**all** of (1), including its baseline and complementary mass.

## 5. How this segment feeds the retained candidate

This section is a conditional dependency calculation. It does not prove the
theta-source estimates stated as inputs here.

The retained fixed-order **proxy** motion argument supplies, with the initial weights
frozen, a full-prefix estimate

\[
\sum_i p_i|E_i(t)|\le4tH,\qquad E_i=S_i'-S_i/t,
\quad p_i=\sum_Ip_{Ii}.
\]

Assume this estimate on \([b_0,b_0+T]\), with \(2K-2\le T\), so the window
contains every triangular sample. The integrating-factor identity
\(S_i(b_0+h)-S_i(b_0)-hS_i(b_0)/b_0
=(b_0+h)\int_{b_0}^{b_0+h}E_i(t)/t\,dt\) gives

\[
\sum_i p_i|\Delta_i(h)|\le4(b_0+T)Hh.
\]

Since nonnegative bin quantities satisfy \(\sum_ID_I^2\le(\sum_ID_I)^2\),

\[
D_{\rm phase}\le64\pi^2(b_0+T)^2H^2\mu_2Z_N,
\qquad \mu_2=\sum_h\alpha_hh^2=\frac{(K-1)(7K-5)}6. \tag{12}
\]

The last identity follows by writing \(h\) as the sum of two independent
uniform integers from \(0\) to \(K-1\). Source motion, moving-weight control,
fixed-order covering and actual/proxy transfer remain external dependencies.
Nothing here differentiates actual theta endpoints or through a floor-order
jump; those transitions require their separately justified covering and transfer.

Let \(\ell\to\infty\) and \(\eta\to0\). In the historical application,
\(\ell=W(2b/\pi)\), where \(b=n+1/2\) and \(W\) is the positive inverse
of \(x\mapsto xe^x\). The budget calculation uses the following explicit
scales as hypotheses; it does not establish their actual-source applicability.

The research schedule sets \(a=\eta/8\), \(m=\lceil16/\eta\rceil\),
\(N=\lceil32/(\pi^2\eta^2)\rceil\),
\(K=\lceil128mN/\eta^2\rceil\), \(L=4K-4\),
and \(\delta_0=\eta^2/(128m)\). Thus
\(N\asymp\eta^{-2}\), \(K\asymp\eta^{-5}\),
\(\delta_0\asymp\eta^3\), and \(L\sim2^{18}\eta^{-5}/\pi^2\).
The ceiling defining \(K\) also gives \(A_0=NM/K\le M\delta_0\le\delta_0/2\).

Assume the separately stated source bounds
\(H_{\rm cell}=O(\ell^{-2})\), \(h_{\rm bin}=O(\eta)\),
\(d_C=O(\ell^{-1})\),
\((b_0+T)H=O(\ell^{-2}\log\ell)\), and the prescribed power/log schedules.
To conclude a small complete mixing budget, also require
\(N\bar W/\delta_0\to0\); the three terms below do not absorb weight motion
without this assumption. Any physical-source transfer errors need their own
compatible bounds. The constants in the assumed estimates are independent of
\(\ell\). At the critical logarithmic endpoint, after fixing \(\kappa\),
require them uniformly for \(0<c_R\le c_{\max}\) with fixed positive
\(c_{\max}\); otherwise choosing a small prefactor need not make the bound small.
Equations (1), (2), (12) then give normalized geometry, mass and phase costs

\[
O(\eta^{-7}\ell^{-2}\log\ell),\qquad
O(\eta^{-4}\ell^{-1}),\qquad
O(\eta^{-19}\ell^{-4}(\log\ell)^2). \tag{13}
\]

For phase, the powers are \(K^2\), \(N^3\), \(\delta_0^{-1}\), giving
\(\eta^{-10-6-3}=\eta^{-19}\). For local mass, the bin factor changes the
coarse normalized \(\eta^{-5}/\ell\) charge to \(\eta^{-4}/\ell\).

At \(\eta=\ell^{-q}\), with fixed \(q>0\), the powers of \(\ell\) in (13) are
\(7q-2,4q-1,19q-4\). They are all negative for \(q<4/19\).
Combining this with the separately required support condition \(q>2A\)
permits \(R=\ell^A\) only for \(A<2/19\) in this sufficient calculation.

At \(R=c_R\ell^{2/19}(\log\ell)^{-p}\), \(\eta=\kappa R^{-2}\),
the phase term is

\[
O\bigl(\kappa^{-19}c_R^{38}(\log\ell)^{2-38p}\bigr).
\]

Geometry and mass still have strictly negative \(\ell\)-powers
\(-10/19,-3/19\). At \(p=1/19\), choose \(\kappa\) first for the separate
support/Gamma conditions, then choose \(c_R\) small for a phase margin.
For fixed \(p>1/19\), the phase term vanishes for any fixed \(c_R\).
This does not give a bare endpoint, an effective onset or an optimality result.

### Incremental and total margins are different

Put \(A_0=NM/K\), \(G=Q_{\rm bin}-A_0\) and \(D=D_{\rm phase}\).
The retained sufficient conditions
\(A_0\le\delta_0/2\) and \(G,D,N\bar W\le\delta_0/16\) give

\[
M_{\rm mix}-A_0\le(3/16+2\sqrt{(9/16)(1/16)})\delta_0
=9\delta_0/16<\delta_0.
\]

This is the historical statement: an increment beyond the baseline. Its total
upper bound is \(17\delta_0/16\); it should not be relabeled as a total below
\(\delta_0\). This distinction does not refute the retained theorem.
For a simple stronger **total** sufficient condition, require
\(G,D,N\bar W\le\delta_0/64\). Then

\[
M_{\rm mix}\le\frac{35+2\sqrt{33}}{64}\delta_0
<\frac{47}{64}\delta_0<\delta_0.
\]

## 6. Exact algebra at the eventual Jensen consumer

For any coefficient sequence \(\gamma\), define
\(J_{d,n}(X)=\sum_{j=0}^d\binom dj\gamma(n+j)X^j\).
For nonnegative integer \(k\), direct coefficient differentiation gives

\[
\partial_X^kJ_{d+k,n-k}(X)=\frac{(d+k)!}{d!}J_{d,n}(X). \tag{14}
\]

Indeed, the coefficient at \(X^j\) on the left is
\(\binom{d+k}{j+k}(j+k)!\gamma(n+j)/j!\), exactly the right-hand coefficient.
If the predecessor polynomial has \(d+k\) distinct negative roots, repeated
Rolle's theorem supplies \(d\) distinct negative roots for (14).
This implication does **not** supply its predecessor premise.

For \(R>0\), \(0\le k\le L\) and
\(d\le Rb-(R+1)L\),

\[
R(b-k)-(d+k)\ge(R+1)(L-k)\ge0.
\]

These identities explain why predecessor selection must reserve both degree
and base headroom. They do not justify combining estimates from different
sources, replacing the original \(B\), or omitting the Gamma/residual error.
The larger candidate still needs its same-source comparator, support,
full Gamma action and analytic residual estimates, and critical-sign argument.

## 7. What the checker establishes

`check.py` uses the authored geometry \(t=10\),
\((V_i,U_i)=(12,15),(18,22),(28,36)\), \(a=1/10\), seven physical bins,
\(K=4,N=3\). Rational interval partitions prove structural mass conservation;
Arb encloses every mass using the primitive \(-z/2-\log(2-z)\).
The terminal atom is deliberately split across three bins. The checker retains
it, compares the local and coarse mass charges, probes both CDFs directly,
and encloses finite affine, perturbed and moving-weight Fourier energies with
the complete cross term. It also exercises a genuinely complex triangular
characteristic and checks the displayed exact algebra.

All comparisons that certify strict numerical inequalities use Arb interval
ordering. JSON endpoints are exact dyadics \(m2^e\); displayed decimals are
for reading. CDF probes and finite algebra examples supplement, rather than
replace, the general derivations above. There is no actual theta data,
large-degree root computation, new autonomous discovery, or full Jensen/RH
certification in this checker.
