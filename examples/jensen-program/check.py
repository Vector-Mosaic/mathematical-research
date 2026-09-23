"""Finite illustrative bin/phase certificate; no actual theta-source computation.

Exact rational geometry determines every cell and complementary interval. Arb
encloses their logarithmic masses and the finite Fourier sums. The general
finite theorem is proved separately in mathematics.md; sampled CDF checks do
not replace that proof or establish the larger retained Jensen candidate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction as F
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import time

import flint
from flint import arb, ctx
import sympy as s

ctx.dps = 60
ctx.threads = 1


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def ball(x):
    x = F(x)
    return arb(x.numerator) / x.denominator


def interval(x):
    def endpoint(y):
        m, e = y.man_exp()
        return {"mantissa": str(m), "exponent2": int(e)}
    require(x.is_finite(), "nonfinite interval")
    return {"display": x.str(22), "lower_bound": endpoint(x.lower()),
            "upper_bound": endpoint(x.upper())}


def mass(lo, hi):
    """Integral of z/(4-2z), with exact rational endpoints."""
    if hi <= lo:
        return arb(0)
    return ball((lo-hi)/2) + ((2-ball(lo))/(2-ball(hi))).log()


def intersection(*intervals):
    lo, hi = max(x[0] for x in intervals), min(x[1] for x in intervals)
    return lo, max(lo, hi)


def amplitude(weights, phases, n):
    re, im = arb(0), arb(0)
    for p, phase in zip(weights, phases):
        angle = 2 * arb.pi() * n * ball(phase)
        re += p * angle.cos()
        im += p * angle.sin()
    return re*re + im*im


def fejer(u, K):
    return amplitude([arb(1)/K]*K, [r*u for r in range(K)], 1)


def finite_fixture():
    t, B, a = F(10), F(12), F(1, 10)
    V, U = list(map(F, [12, 18, 28])), list(map(F, [15, 22, 36]))
    K, N = 4, 3
    require(t < V[0] <= 2*t, "invalid base geometry")
    require(all(V[i] < U[i] for i in range(3)) and
            all(U[i] < V[i+1] for i in range(2)), "noninterlacing geometry")
    S = [sum((U[j]-V[j] for j in range(i+1)), F(0)) for i in range(3)]
    velocity = [x/t for x in S]
    z = [B/x for x in U] + [F(0)]
    cells = [intersection((a, 1-a), (z[i+1], z[i])) for i in range(3)]
    bins = [(F(j, 10), F(j+1, 10)) for j in range(1, 8)]
    require(bins[0][0] == a and bins[-1][1] == z[0] and
            all(x[1] == y[0] for x, y in zip(bins, bins[1:])),
            "physical bins do not partition covered clip")
    parts = [[intersection(I, C) for C in cells] for I in bins]
    for i, C in enumerate(cells):
        nonempty = sorted(p[i] for p in parts if p[i][1] > p[i][0])
        require(nonempty[0][0] == C[0] and nonempty[-1][1] == C[1] and
                all(x[1] == y[0] for x, y in zip(nonempty, nonempty[1:])),
                "a cell was lost or counted twice")
    p = [[mass(*part) for part in row] for row in parts]
    M = [sum(row, arb(0)) for row in p]
    total = sum(M, arb(0))
    require(total < arb(1)/2, "mass bound failed")
    require((total-mass(a, z[0])).contains(0), "mass enclosure mismatch")
    H = [max(hi-lo for lo, hi in row) for row in parts]
    # Complementary intervals in reciprocal coordinates; last is terminal.
    complement = [(B/V[i+1], B/U[i]) for i in range(2)] + [(F(0), B/U[-1])]
    atom_parts = [[intersection(I, C, (a, z[0])) for C in complement] for I in bins]
    atoms = [[mass(*part) for part in row] for row in atom_parts]
    d = [sum(row, arb(0)) for row in atoms]
    d_total = sum(d, arb(0))
    for j, C in enumerate(complement):
        clipped = intersection(C, (a, z[0]))
        pieces = sorted(row[j] for row in atom_parts if row[j][1] > row[j][0])
        require(pieces[0][0] == clipped[0] and pieces[-1][1] == clipped[1] and
                all(x[1] == y[0] for x, y in zip(pieces, pieces[1:])),
                "complementary plateau mass lost or duplicated")
    terminal_bins = [i for i, row in enumerate(atom_parts) if row[-1][1] > row[-1][0]]
    require(len(terminal_bins) >= 2, "fixture must split one terminal atom across bins")
    terminal = sum((row[-1] for row in atoms), arb(0))
    require(terminal > 0, "terminal plateau disappeared")
    m0 = 1-t*sum((1/V[i]-1/U[i] for i in range(3)), F(0))
    c = B/t
    require(d_total < ball(c*m0/2), "complementary source mass bound failed")
    local_mass = sum((x*y for x, y in zip(M, d)), arb(0))
    coarse_mass = total*d_total
    hbin = max(hi-lo for lo, hi in bins)
    mass_cap = ball(hbin*(1-a)/(2*(1+a))) * d_total
    require(local_mass < mass_cap and local_mass < coarse_mass,
            "local mass gain was not certified")

    # Direct pushforward CDF probes, distinct from the all-v analytic proof.
    # On (V_j,U_j), A(X)=S_{j-1}/t+(X-V_j)/t; on a complement it is S_j/t.
    probes = sorted(set([F(0), *velocity, F(2)] +
                        [(velocity[i]+velocity[i+1])/2 for i in range(2)]))
    cdf_count = 0
    for bindex, I in enumerate(bins):
        for v in probes:
            nu = sum((p[bindex][i] for i in range(3) if velocity[i] <= v), arb(0))
            lam = arb(0)
            for j in range(3):
                if velocity[j] <= v:
                    lam += atoms[bindex][j]
                left_velocity = F(0) if j == 0 else velocity[j-1]
                endX = min(U[j], max(V[j], V[j]+t*(v-left_velocity)))
                lam += mass(*intersection(I, (a, z[0]), (B/endX, B/V[j])))
            error = nu-lam
            # Construct (5) from its unique possible contributing cell.
            # Empty intersections establish exact zero without asking an
            # interval containing zero to certify a sign.
            positive_cdf = arb(0)
            for i in range(2):
                if velocity[i] <= v < velocity[i+1]:
                    threshold = B/(V[i+1]+t*(v-velocity[i]))
                    piece = intersection(I, cells[i], (F(0),threshold))
                    positive_cdf = mass(*piece)
                    if piece[1] > piece[0]:
                        require(positive_cdf > 0, "positive CDF part not enclosed positively")
            require((error-positive_cdf).contains(0), "independent CDF formulas disagree")
            psi = min((1-a)/(1+a), c/(c+2*v))
            rhs = ball(H[bindex]*psi/2)
            require(positive_cdf < rhs,
                    "CDF probe exceeds local-cell envelope")
            cdf_count += 1

    weights = [F(min(h+1, 2*K-1-h), K*K) for h in range(2*K-1)]
    require(sum(weights) == 1, "triangular weights not normalized")
    mu2 = sum((w*h*h for h, w in enumerate(weights)), F(0))
    require(mu2 == F((K-1)*(7*K-5), 6), "second moment failed")
    Z = N*(N+1)*(2*N+1)//6
    CK = 2*arb.pi()/arb(3).sqrt() * ball(1-F(1, K*K)).sqrt()
    Vstar = c*(1/a-1)
    Texact = sum((ball(min((1-a)/(1+a), c/(c+2*F(k,n))))
                  for n in range(1,N+1)
                  for k in range(math.ceil(n*Vstar))), arb(0))
    Tbound = N + ball(c*N*(N+1)/4) * ball(2/a-1).log()
    require(Texact < Tbound, "frequency sum upper bound failed")
    A0 = N*total/K
    geometric = CK*Tbound*sum((Mi*ball(Hi) for Mi,Hi in zip(M,H)),arb(0))
    Q = A0 + N*local_mass + geometric
    pair_energy = sum((pI[i]*pI[j]*fejer(n*(velocity[i]-velocity[j]),K)
                       for pI in p for i in range(3) for j in range(3)
                       for n in range(1,N+1)),arb(0))
    affine, frozen, moving, D2, Wbar = (arb(0) for _ in range(5))
    for h, alpha in enumerate(weights):
        phases0 = [S[i]+h*velocity[i] for i in range(3)]
        drift = [F((i+1)*h*h, 100000) for i in range(3)]
        phases = [x+y for x,y in zip(phases0,drift)]
        factor = 1+F(h,200)
        require(ball(factor)*total < arb(1)/2, "moving weights too large")
        Wbar += ball(alpha*(factor-1))*total
        for pI in p:
            DI = sum((pI[i]*ball(abs(drift[i])) for i in range(3)),arb(0))
            D2 += ball(alpha)*DI*DI
            for n in range(1,N+1):
                affine += ball(alpha)*amplitude(pI,phases0,n)
                frozen += ball(alpha)*amplitude(pI,phases,n)
                moving += ball(alpha)*amplitude([ball(factor)*x for x in pI],phases,n)
    Dphase = 4*arb.pi()**2*Z*D2
    cross = 2*(Q*Dphase).sqrt()
    full_bound = N*Wbar + Q + Dphase + cross
    require(affine < pair_energy, "triangular modulus comparison failed")
    require(pair_energy < Q and affine < Q, "Fejer affine bound failed")
    require(frozen < Q + Dphase + cross, "frozen Hilbert bound failed")
    require(abs(moving-frozen) < N*Wbar, "moving-weight telescoping bound failed")
    require(moving < full_bound, "full Hilbert bound failed")
    require(cross > 0 and Dphase > 0, "nonzero perturbation/cross term required")
    # Non-real triangular characteristic: square, not absolute square.
    u = F(1,7)
    rr = sum(((2*arb.pi()*ball(r*u)).cos()/K for r in range(K)),arb(0))
    ii = sum(((2*arb.pi()*ball(r*u)).sin()/K for r in range(K)),arb(0))
    require(not (2*rr*ii).contains(0), "fixture must expose complex characteristic")
    return {
        "input": {"kind":"authored finite geometry; not actual theta data", "t":str(t),
                  "V":[str(x) for x in V],"U":[str(x) for x in U],
                  "full_prefixes":[str(x) for x in S], "clip":[str(a),str(1-a)],
                  "bins":[[str(x),str(y)] for x,y in bins],"K":K,"N":N},
        "finite_geometry": {"mass_conserved":True,"shared_plateau_atom_retained":True,
             "terminal_mass_retained":True,"cdf_bound_checked":True,
             "cdf_check_scope":"finite rational probes; all-v result is proved in mathematics.md",
             "cdf_probe_count":cdf_count,"terminal_atom_bin_indices":terminal_bins,
             "total_mass":interval(total),"complementary_mass":interval(d_total),
             "terminal_mass":interval(terminal),"local_mass_charge":interval(local_mass),
             "coarse_mass_charge":interval(coarse_mass),"local_mass_cap":interval(mass_cap)},
        "fejer_hilbert": {"affine_energy_bound":True,"full_mixing_bound":True,
             "cross_term_retained":True,"complex_triangular_characteristic_exercised":True,
             "affine_to_positive_pair_comparison":True,"frozen_phase_bound":True,
             "moving_weight_bound":True,
             "bin_normalization_divisor":None,"affine_energy":interval(affine),
             "positive_pair_energy":interval(pair_energy),"Q_bin":interval(Q),
             "frozen_nonlinear_energy":interval(frozen),"moving_energy":interval(moving),
             "D_phase":interval(Dphase),"cross_term":interval(cross),
             "N_Wbar":interval(N*Wbar),"full_bound":interval(full_bound),
             "mu2":str(mu2),"Z_N":Z}}


def exact_identities():
    K=s.symbols('K',integer=True,positive=True)
    r=s.symbols('r',integer=True,nonnegative=True)
    mean=s.summation(r,(r,0,K-1))/K
    second=s.summation(r*r,(r,0,K-1))/K
    require(s.simplify(2*second+2*mean**2-(K-1)*(7*K-5)/6)==0,
            "general triangular second moment identity failed")
    q,A,p=s.symbols('q A p',real=True)
    exponents=[7*q-2,4*q-1,19*q-4]
    boundary=[s.simplify(x.subs(q,s.Rational(4,19))) for x in exponents]
    require(boundary==[-s.Rational(10,19),-s.Rational(3,19),0], "range exponents failed")
    require(10+6+3==19, "phase scaling factors failed")
    require(2-38*s.Rational(1,19)==0, "logarithmic boundary failed")
    # Arbitrary coefficient symbols: exact integer derivative identity, not RH values.
    x=s.symbols('x'); d,k=5,3
    coefficients=s.symbols('g0:'+str(d+k+1))
    predecessor=sum(s.binomial(d+k,j)*coefficients[j]*x**j for j in range(d+k+1))
    target=sum(s.binomial(d,j)*coefficients[j+k]*x**j for j in range(d+1))
    require(s.expand(s.diff(predecessor,x,k)-s.factorial(d+k)/s.factorial(d)*target)==0,
            "Jensen derivative coefficient identity failed")
    R,b,L,kk=s.symbols('R b L k',real=True)
    require(s.expand(R*(b-kk)-(R*b-(R+1)*L+kk)-(R+1)*(L-kk))==0,
            "degree headroom identity failed")
    require(F(3,16)+2*F(3,16)==F(9,16), "historical incremental margin failed")
    require(33<36 and F(1,2)+F(3,64)+F(12,64)==F(47,64)<1,
            "stronger sufficient total margin failed")
    return {"triangular_moments":True,"budget_exponents":True,"log_boundary":True,
            "rolle_identity":True,"degree_headroom":True,
            "rolle_check_scope":"symbolic arbitrary coefficients at d=5,k=3; general proof in mathematics.md",
            "boundary_ell_exponents":[str(x) for x in boundary],
            "phase_log_exponent":str(2-38*p),
            "historical_margin":"increment beyond A0 <= 9/16 delta0; total <= 17/16 delta0",
            "stronger_total_sufficient_margin":"corrections <= delta0/64 imply total < 47/64 delta0"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(); started=time.perf_counter()
    result={"schema_version":"mathematical-research.jensen-finite-certificate.v1",
            "example":"jensen-program","created_at":datetime.now(timezone.utc).isoformat(),
            "dependencies":{"python":platform.python_version(),"python_flint":flint.__version__,
                            "sympy":s.__version__,"arb_decimal_precision":ctx.dps,"threads":ctx.threads}}
    try:
        result.update(finite_fixture());result['exact_identities']=exact_identities()
        result['status']='certified'
        result['scope']={"verified":["authored finite interval fixture","exact algebraic identities"],
             "conditional":["asymptotic budget uses separately stated source estimates"],
             "not_established":["actual theta applicability","full Jensen candidate","RH",
                                "effective onset","optimality","novelty"]}
        result['source_sha256']={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ['check.py','mathematics.md','requirements.txt','problem.json']}
        code=0
    except (RuntimeError,ValueError,ArithmeticError) as exc:
        result.update(status='not_certified',error=str(exc));code=1
    result['elapsed_seconds']=round(time.perf_counter()-started,6)
    rendered=json.dumps(result,indent=2,sort_keys=True)+'\n'
    if args.output:
        args.output.write_text(rendered,encoding='utf-8')
    sys.stdout.write(rendered)
    return code


if __name__=='__main__':
    raise SystemExit(main())
