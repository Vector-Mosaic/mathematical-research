"""Exact algebra for a failed short-gap ansatz and a wider Jacobi construction.

No Riemann-xi coefficients, saddle estimates or floating-point root searches
are used. The conditional finite-n implication is proved in mathematics.md;
this checker verifies its algebraic inputs and a separate rational witness.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

import sympy as s


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def zero(expression: s.Expr, message: str) -> None:
    require(s.cancel(s.expand(expression)) == 0, message)


def strings(matrix: s.Matrix) -> list[list[str]]:
    return [[str(value) for value in row] for row in matrix.tolist()]


def quotient(k: s.Expr, u: s.Expr, v: s.Expr) -> s.Expr:
    return (u + k) * (v + k + 1) / ((v + k) * (u + k + 1))


def short_gap_obstruction() -> dict:
    x, w = s.symbols("x w", positive=True)
    zero(w * (x - 1) ** 2 - (w * (x**2 - 1) - 2 * w * (x - 1)),
         "The weighted-square identity failed")
    required_square = s.Rational(1, 2) - 2 * s.Rational(1, 3)
    require(required_square == -s.Rational(1, 6), "Wrong obstruction constant")
    require(required_square < 0, "The required sum must be negative")
    return {
        "polynomial_identity_verified": True,
        "weighted_square_target": str(required_square),
        "excluded": "fixed finite positive short-gap family at the stated limiting targets",
        "assumptions": ["w_i > 0", "x_i > 0", "sum w_i*(x_i-1) = 1/3",
                        "sum w_i*(x_i**2-1) = 1/2"],
    }


def wide_r5() -> dict:
    a, b, mu, t = s.symbols("a b mu t", nonzero=True)
    variables = [a, b, mu, t]
    model = s.Matrix([1 - 1/a - b + mu/t**2, 2*b - 2*mu/t**3,
                      -6*b + 6*mu/t**4, 24*b - 24*mu/t**5])
    target = s.Matrix([0, 0, 2, -12])
    solution = {a: s.Integer(3), b: -s.Rational(2, 3),
                mu: -s.Rational(16, 3), t: s.Integer(2)}
    residual = (model - target).subs(solution)
    require(residual == s.zeros(4, 1), "The wider limiting equations failed")
    jacobian = model.jacobian(variables).subs(solution)
    expected_jacobian = s.Matrix([[s.Rational(1,9), -1, s.Rational(1,4), s.Rational(4,3)],
                                 [0, 2, -s.Rational(1,4), -2],
                                 [0, -6, s.Rational(3,8), 4],
                                 [0, 24, -s.Rational(3,4), -10]])
    require(jacobian == expected_jacobian, "Jacobian entries changed")
    determinant = s.factor(jacobian.det())
    require(determinant == -s.Rational(1,6), "Jacobian is not the claimed value")
    inverse = s.Matrix([[9, s.Rational(45,2), 12, s.Rational(3,2)],
                        [0, s.Rational(1,2), s.Rational(2,3), s.Rational(1,6)],
                        [0, -24, -s.Rational(56,3), -s.Rational(8,3)],
                        [0, 3, 3, s.Rational(1,2)]])
    require(jacobian * inverse == s.eye(4), "The displayed inverse failed")
    # On t>1: row 1 eliminates b, row 2 eliminates mu. The final row
    # forces t=2; then row 0 fixes a=3. No numerical solve is involved.
    mu_of_t = t**4 / (3 * (1 - t))
    remaining = (model[3] + 12).subs(b, mu/t**3).subs(mu, mu_of_t)
    zero(remaining - 4*(t-2)/t, "The admissible uniqueness elimination failed")
    next_model_row = s.cancel(120 * (mu/t**6 - b)).subs(solution)
    require(next_model_row == 70, "Wrong next formal model row")
    return {
        "solution": {str(key): str(value) for key, value in solution.items()},
        "target": [str(value) for value in target],
        "residual_zero": True,
        "jacobian": strings(jacobian),
        "jacobian_determinant": str(determinant),
        "jacobian_inverse": strings(inverse),
        "unique_in_limiting_region": "a>0, t>1",
        "local_finite_n_existence": "conditional_on_C1_target_asymptotics",
        "factor_gap_limits": {"(U1-V1)/n": "1", "(U2-V2)/(n*L)": "3"},
        "cross_junction_coefficient_mu": "-16/3",
        "next_formal_model_row": str(next_model_row),
        "next_target_row_if_assumed": "72",
        "next_target_minus_model_if_assumed": "2",
    }


def quotient_recursion() -> dict:
    u1, v1, u2, v2, scale, k = s.symbols("U1 V1 U2 V2 S k", positive=True)
    ratio = scale * (u1+k) * (u2+k) * v2 / (u1*u2*(v1+k)*(v2+k))
    zero(ratio / ratio.subs(k, k+1) - quotient(k, u1, v1)*quotient(k, u2, v2),
         "General quotient factorization failed")
    r1 = s.symbols("R1", positive=True)
    q = s.symbols("q0:4", positive=True)
    recovered = [s.Integer(1), r1]
    for j in range(4):
        recovered.append(s.cancel(recovered[-1]**2 / (q[j] * recovered[-2])))
    for j in range(2, 6):
        formula = r1**j / s.prod(q[i]**(j-1-i) for i in range(j-1))
        zero(recovered[j] - formula, "Coefficient reconstruction failed")
    # Exact telescoping at the zero-offset junction.
    for j in range(7):
        model = scale**j*s.rf(u1,j)*s.rf(u2,j)*v2**j / (
            u1**j*u2**j*s.rf(v1,j)*s.rf(v2,j))
        collapsed = scale**j*s.rf(u2,j)/(u2**j*s.rf(v1,j))
        zero(model.subs(u1, v2) - collapsed, "Pochhammer telescoping failed")
    return {"verified": True, "matched_indices": list(range(6)),
            "required_quotient_indices": list(range(4)),
            "reconstructed_coefficients": [str(value) for value in recovered],
            "zero_offset_telescoping_verified": True}


def positive_root_certificate(polynomial: s.Poly) -> dict:
    require(polynomial.get_domain() == s.QQ, "Root certificate requires rational coefficients")
    intervals = polynomial.intervals(eps=s.Rational(1,1000))
    require(sum(multiplicity for _, multiplicity in intervals) == polynomial.degree(),
            "Not all roots are real")
    require(all(left > 0 and multiplicity == 1 for (left, _), multiplicity in intervals),
            "Roots must be strictly positive and simple")
    # The exact interval isolator establishes completeness, while sign changes
    # supply an immediately inspectable independent certificate for each interval.
    encoded = []
    for (left, right), multiplicity in intervals:
        if left == right:
            require(polynomial.eval(left) == 0, "Incorrect exact root")
        else:
            require(polynomial.eval(left)*polynomial.eval(right) < 0,
                    "The reported interval has no sign change")
        encoded.append({"lower": str(left), "upper": str(right), "multiplicity": multiplicity})
    return {"positive_root_count": polynomial.degree(), "all_roots_simple": True,
            "isolating_intervals": encoded}


def finite_witness() -> dict:
    # Illustrative comparison parameters only: not fitted to xi coefficients.
    d = 5
    u1, v1, u2, v2 = map(s.Integer, (16, 8, 60, 20))
    y = s.symbols("y")
    left = [s.rf(u1,j)/(u1**j*s.rf(v1,j)) for j in range(d+1)]
    right = [s.rf(u2,j)*v2**j/(u2**j*s.rf(v2,j)) for j in range(d+1)]
    factor1 = s.Poly(sum((-1)**j*s.binomial(d,j)*left[j]*y**j for j in range(d+1)), y, domain=s.QQ)
    factor2 = s.Poly(sum((-1)**j*s.binomial(d,j)*right[j]*y**j for j in range(d+1)), y, domain=s.QQ)
    convolution = s.Poly(sum((-1)**j*factor1.nth(j)*factor2.nth(j)/s.binomial(d,j)*y**j
                             for j in range(d+1)), y, domain=s.QQ)
    hypergeom = s.Poly(sum(s.rf(-d,j)*s.rf(u1,j)*s.rf(u2,j) /
                          (s.factorial(j)*s.rf(v1,j)*s.rf(v2,j)) * (v2*y/(u1*u2))**j
                          for j in range(d+1)), y, domain=s.QQ)
    require(convolution == hypergeom, "Finite-free coefficient normalization failed")
    ratios = [s.cancel(s.rf(u1,j)*s.rf(u2,j)*v2**j /
                       (u1**j*u2**j*s.rf(v1,j)*s.rf(v2,j))) for j in range(7)]
    for k in range(5):
        zero(ratios[k+1]**2/(ratios[k]*ratios[k+2]) - quotient(k,u1,v1)*quotient(k,u2,v2),
             "Finite witness quotients failed")
    require(u1-v1 > d-1 and u2-v2 > d-1 and u1 < v2,
            "The witness must separate factor gaps from the negative junction")
    root_certificates = {name: positive_root_certificate(poly) for name, poly in
                         [("factor_1", factor1), ("factor_2", factor2), ("comparison", convolution)]}
    return {"degree": d, "kind": "illustrative_exact_comparison_not_RH_coefficient_fit",
            "parameters": {"U1": str(u1), "V1": str(v1), "U2": str(u2), "V2": str(v2),
                           "S": "1", "R1": "1/8"},
            "factor_gaps": [str(u1-v1), str(u2-v2)],
            "cross_junction_offset": str(u1-v2),
            "jacobi_beta_parameters": [str(u1-v1-d), str(u2-v2-d)],
            "finite_free_factorization_verified": True,
            "coefficients_ascending": {name: [str(poly.nth(j)) for j in range(d+1)] for name, poly in
                                       [("factor_1",factor1), ("factor_2",factor2), ("comparison",convolution)]},
            "root_certificates": root_certificates,
            "coefficient_ratios_R0_through_R6": [str(value) for value in ratios]}


def differential_identity() -> dict:
    # Verify the exact operator factorization on a general monomial y^j.
    # The two powers y^(j-1), y^j can be compared as scalar coefficients.
    j, d, u1, v1, u2, v2 = s.symbols("j d U1 V1 U2 V2")
    c = v2/(u1*u2)
    epsilon = (u1-v2)/u1
    jacobi_lower = j*(j-1+v1)
    jacobi_same = -(j-d)*(j+u2)/u2
    # H/y is the 3F2 differential operator divided on the left by y.
    hyper_lower = j*(j+v1-1)*(j+v2-1)
    hyper_same = -c*(j-d)*(j+u1)*(j+u2)
    perturbation = epsilon/u2*j*(j-d)*(j+u2)
    zero((j-1+v2)*jacobi_lower-hyper_lower, "Lower operator coefficient failed")
    zero((j+v2)*jacobi_same+perturbation-hyper_same, "Diagonal operator coefficient failed")
    # Differentiate E(E-d)(E+U2)p, using the falling-factorial basis.
    m, ell = s.symbols("m ell", integer=True, nonnegative=True)
    beta = u2*(2*m+1-d)-d*(2*m+1)+3*m**2+3*m+1
    gamma = m*(m-d)*(m+u2)
    scalar = ((ell-m)*(ell-m-1)*(ell-m-2)
              +(u2-d+3+3*m)*(ell-m)*(ell-m-1)+beta*(ell-m)+gamma)
    zero(scalar - ell*(ell-d)*(ell+u2), "Differentiated recurrence coefficients failed")
    return {"general_monomial_operator_identity_verified": True,
            "differentiated_recurrence_coefficients_verified": True,
            "epsilon": "(U1-V2)/U1",
            "scope": "exact identities only; no uniform derivative-ratio bound or Jensen wedge"}


def certificate() -> dict:
    return {"schema_version": "mathematical-research.jacobi-certificate.v1",
            "example": "jacobi-extension", "status": "certified",
            "short_gap_obstruction": short_gap_obstruction(), "wide_r5": wide_r5(),
            "quotient_recursion": quotient_recursion(), "finite_witness": finite_witness(),
            "differential_identity": differential_identity(),
            "scope": {
                "established": ["short-gap no-go at the explicit limiting targets",
                                "wide R5 limiting solution and nonsingular Jacobian",
                                "exact quotient/coefficient and operator identities",
                                "rational positive-rooted comparison witness"],
                "conditional": ["finite-n R5 matching under the explicit C1 convergence hypothesis"],
                "not_established": ["actual RH coefficient match at any specified n",
                                    "uniform analytic residual and recurrence estimates",
                                    "higher Jensen hyperbolicity wedge", "Riemann Hypothesis"]}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Also write the JSON certificate to this file")
    args = parser.parse_args()
    started = time.monotonic()
    metadata = {"created_at": datetime.now(timezone.utc).isoformat(),
                "dependencies": {"python": platform.python_version(), "sympy": s.__version__},
                "source_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                  for name in ("check.py", "mathematics.md", "problem.json", "requirements.txt")}}
    try:
        result = certificate()
        exit_code = 0
    except (RuntimeError, ValueError, ArithmeticError) as error:
        result = {"schema_version": "mathematical-research.jacobi-certificate.v1",
                  "example": "jacobi-extension", "status": "not_certified", "error": str(error)}
        exit_code = 1
    result.update(metadata)
    result["elapsed_seconds"] = round(time.monotonic()-started, 6)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
