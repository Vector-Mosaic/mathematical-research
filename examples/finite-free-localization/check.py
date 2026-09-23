#!/usr/bin/env python3
"""Exact, dependency-free computations accompanying mathematics.md.

This checks explicit witnesses, coefficient identities and selected finite cases.
The all-parameter arguments and their literature dependencies are in the prose
proof; finite cases do not prove that theorem. No model or research store runs.
"""

from fractions import Fraction as F
from math import comb, isqrt
import json


def require(condition, message):
    if not condition:
        raise ArithmeticError(message)


def trim(p):
    p = list(p)
    while len(p) > 1 and not p[-1]:
        p.pop()
    return p


def add(p, q):
    return trim([(p[i] if i < len(p) else F(0))
                 + (q[i] if i < len(q) else F(0))
                 for i in range(max(len(p), len(q)))])


def scale(p, a):
    return trim([a * c for c in p])


def at(p, x):
    value = F(0)
    for c in reversed(p):
        value = value * x + c
    return value


def derivative(p):
    return [i * p[i] for i in range(1, len(p))] or [F(0)]


def remainder(p, q):
    p = trim(p)
    require(q != [0], "polynomial division by zero")
    while p != [0] and len(p) >= len(q):
        shift, factor = len(p) - len(q), p[-1] / q[-1]
        for j, c in enumerate(q):
            p[j + shift] -= factor * c
        p = trim(p)
    return p


def sturm(p):
    sequence = [trim(p)]
    if len(p) <= 1:
        return sequence
    sequence.append(derivative(p))
    while True:
        rem = scale(remainder(sequence[-2], sequence[-1]), F(-1))
        if rem == [0]:
            return sequence
        # A positive rescaling controls coefficient size without changing signs.
        sequence.append(scale(rem, 1 / abs(rem[-1])))


def root_count(p, left, right):
    require(left < right, "empty root-count interval")
    require(at(p, left) != 0 and at(p, right) != 0,
            "Sturm endpoints must not be roots")
    sequence = sturm(p)

    def variations(x):
        signs = [1 if value > 0 else -1 for s in sequence
                 if (value := at(s, x)) != 0]
        return sum(a != b for a, b in zip(signs, signs[1:]))

    return variations(left) - variations(right)


def model_coefficients(d, A, B, C, D):
    """Direct terminating 3F2 recurrence, ascending powers of y."""
    A, B, C, D = map(F, (A, B, C, D))
    result = [F(1)]
    for j in range(1, d + 1):
        result.append(result[-1] * F(-(d - j + 1), j)
                      * (A + j - 1) * (C + j - 1) * D
                      / ((B + j - 1) * (D + j - 1) * A * C))
    return result


def jacobi_coefficients(d, U, V):
    U, V = F(U), F(V)
    result = [F(1)]
    for j in range(1, d + 1):
        result.append(result[-1] * F(-(d - j + 1), j)
                      * (U + j - 1) / (U * (V + j - 1)))
    return result


def jacobi_characteristic(d, U, V):
    """Independent tridiagonal determinant recurrence; no square roots needed."""
    U, V = F(U), F(V)
    H, alpha, beta = U - d - 1, V - 1, U - V - d
    previous, current = [F(0)], [F(1)]
    for k in range(d):
        diagonal = U * (H * (V + 2 * k) + 2 * k * (k + 1)) \
            / ((H + 2 * k) * (H + 2 * k + 2))
        shifted = add([F(0)] + current, scale(current, -diagonal))
        if k:
            off_squared = (U / (H + 2 * k)) ** 2 \
                * k * (k + alpha) * (k + beta) * (k + H) \
                / ((H + 2 * k - 1) * (H + 2 * k + 1))
            shifted = add(shifted, scale(previous, -off_squared))
        previous, current = current, shifted
    return scale(current, 1 / current[0])


def coarse_conditions(d, A, B, C, D):
    return (d >= 1 and min(A, B, C, D) > 0 and A >= 8 * B
            and B >= 144 * d and D >= 144 * d
            and 4 * d <= C - D <= F(D, 4))


def counterexamples():
    d, D, C = 2, 288, 296
    q0 = F(D * (C + 1), C * (D + 1))
    require(1 - q0 == F(1, 10693), "limiting gap identity")
    require(F(1, 104) ** 2 < 1 - q0, "rational lower bound for limiting gap")
    records = []
    for constant in (1, 10, 100, 1000):
        t = 106 * constant
        B, A = 2 * t * t, 16 * t * t
        require(coarse_conditions(d, A, B, C, D), "counterfamily assumptions")
        p = model_coefficients(d, A, B, C, D)
        q = F(B, B + 1) * F(A + 1, A) * q0
        require(p == [F(1), F(-2, B), q / (B * B)], "quadratic coefficients")
        require(0 < q < q0 < 1, "positive distinct quadratic roots")
        require(F(B, D) > 4, "witness must violate the added assumption")
        radius_unit = 2 * t
        require(radius_unit ** 2 == B * d, "exact sqrt(Bd)")
        threshold = B - constant * radius_unit
        # p(0)>0>p(threshold): a root lies to the left of the claimed interval.
        require(0 < threshold < B and at(p, threshold) < 0,
                "explicit violation of the requested localization constant")
        require(root_count(p, F(0), F(threshold)) == 1,
                "exactly one root is below the claimed interval")
        records.append({"C_loc": constant, "A": A, "B": B, "C": C,
                        "D": D, "d": d, "sqrt_Bd": radius_unit,
                        "root_below": threshold,
                        "normalized_displacement_strictly_greater_than": constant})
    return records


def repaired_cases():
    records = []
    # Boundary comparability, minimum scales, both gap endpoints, several degrees.
    cases = [(1, 144, 144, 4), (2, 1152, 288, 72),
             (3, 432, 1728, 12), (5, 2880, 720, 180),
             (8, 8192, 4096, 32), (12, 6912, 1728, 432)]
    for d, B, D, gap in cases:
        A, C = 8 * B, D + gap
        require(coarse_conditions(d, A, B, C, D) and F(B, D) <= 4,
                "repaired example assumptions")
        p = model_coefficients(d, A, B, C, D)
        q1, q2 = jacobi_coefficients(d, A, B), jacobi_coefficients(d, C, D)
        require(q1 == jacobi_characteristic(d, A, B), "first Jacobi identity")
        require(q2 == jacobi_characteristic(d, C, D), "second Jacobi identity")
        convolution = [(-1) ** j * q1[j] * q2[j] * D ** j / comb(d, j)
                       for j in range(d + 1)]
        require(p == convolution, "finite-free coefficient identity")
        unit = isqrt(B * d)
        require(unit * unit == B * d, "example needs rational sqrt(Bd)")
        lower, upper = F(B - 10 * unit), F(B + 10 * unit)
        require(root_count(p, lower, upper) == d, "all roots satisfy repaired bound")
        require(root_count(p, F(9 * B, 16), F(25 * B, 16)) == d,
                "all roots satisfy coarse positive interval")
        require(root_count(derivative(p), lower, upper) == d - 1,
                "all critical points satisfy repaired bound")
        records.append({"d": d, "A": A, "B": B, "C": C, "D": D,
                        "distinct_roots_in_bound": d,
                        "critical_points_in_bound": d - 1})
    return records


def main():
    require(F(3104, 961) < F(13, 4), "diagonal estimate simplification")
    require(F(33, 31) ** 2 * F(33, 32) < F(9, 8) ** 2,
            "off-diagonal estimate simplification")
    require(F(1, 32) < F(3, 16) ** 2, "square-root estimate")
    require(F(13, 4) * F(3, 16) + F(9, 4) == F(183, 64) < 3,
            "Jacobi localization constant")
    require(3 + 6 + F(9, 12) < 10, "repaired localization constant")
    result = {
        "schema_version": "mathematical-research.example-check.v1",
        "example_id": "finite-free-localization",
        "status": "passed",
        "arithmetic": "Python Fraction; exact polynomial signs and Sturm counts",
        "counterexamples": counterexamples(),
        "repaired_cases": repaired_cases(),
        "proof_constants_checked": True,
        "scope": "Explicit witnesses and six finite cases; the general proof and external theorem dependencies are in mathematics.md. This is not a model run or a proof of RH."
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
