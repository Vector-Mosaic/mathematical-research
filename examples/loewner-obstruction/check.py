"""Rigorous Arb certificate for the theta Mellin-ratio Loewner obstruction.

This standalone check encloses finite integrals with Arb and adds explicit
bounds for the three omitted regions:

* theta components n > N on the finite x interval;
* the endpoint x < -X (equivalently t < exp(-X));
* the superexponential tail x > Y (equivalently t > exp(Y)).

The normalized density constant cancels from all ratios, so the unnormalized
theta source Phi is used throughout.  A passing result certifies the displayed
finite Loewner obstruction for this exact interpolation.  It proves neither
an ordinary Bernstein failure, either Hankel sign, the Stieltjes target, nor
RH.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

import flint
from flint import acb, arb, ctx


ctx.dps = 70
ctx.threads = 1

N_THETA = 5
X_LEFT = 60
Y_RIGHT = 1
REL_TOL = arb("1e-52")
ABS_TOL = arb("1e-52")

NODES = (1, 2, 4, 6)
VECTOR = (28336, -66314, 64141, -26181)


def require(condition: bool, message: str) -> None:
    """Enforce a certificate gate even when Python is run with ``-O``."""

    if not condition:
        raise RuntimeError(message)


def symmetric_error(radius: arb) -> arb:
    """Return a zero-centered ball containing +/- radius."""

    return arb(0, radius.upper())


def theta_source_truncated(t: acb) -> acb:
    """Return the first N_THETA exact theta-source components."""

    pi = acb.pi()
    e4t = (4 * t).exp()
    total = acb(0)
    for n in range(1, N_THETA + 1):
        n2 = n * n
        q = pi * n2 * e4t
        total += pi * n2 * (5 * t - q).exp() * (2 * q - 3)
    return total


def integration_segments() -> list[arb]:
    values = list(range(-X_LEFT, -10, 5))
    values += list(range(-10, 1))
    values += [arb(1) / 4, arb(1) / 2, arb(3) / 4, arb(Y_RIGHT)]
    return [arb(value) for value in values]


def finite_integral(s: int, log_order: int) -> arb:
    """Enclose the finite theta sum on -X_LEFT <= x <= Y_RIGHT."""

    def integrand(x: acb, analytic: bool) -> acb:
        # Only exp, integer powers and sums/products occur: this is entire.
        del analytic
        t = x.exp()
        return ((s + 1) * x).exp() * (x**log_order) * theta_source_truncated(t)

    total = acb(0)
    segments = integration_segments()
    for left, right in zip(segments, segments[1:]):
        total += acb.integral(
            integrand,
            left,
            right,
            rel_tol=REL_TOL,
            abs_tol=ABS_TOL,
            deg_limit=160,
            eval_limit=200000,
            depth_limit=40,
            use_heap=True,
        )
    require(total.is_finite(), "finite integral did not produce a finite enclosure")
    require(total.imag.contains(0), "finite integral does not enclose a real value")
    return total.real


def fourth_power_gaussian_tail(first_n: int, q: arb) -> arb:
    """Bound sum_{n>=first_n} n^4 exp(-q n^2)."""

    n = first_n
    first = arb(n) ** 4 * (-q * n * n).exp()
    ratio = (arb(n + 1) / n) ** 4 * (-q * (2 * n + 1)).exp()
    require(ratio.upper() < 1, "Gaussian-tail ratio is not below one")
    return first / (1 - ratio)


def source_global_bound() -> arb:
    """Bound Phi(t) uniformly for t >= 0."""

    pi = arb.pi()
    return 2 * pi**2 * fourth_power_gaussian_tail(1, pi)


def source_component_tail_bound() -> arb:
    """Bound sum_{n>N_THETA} Phi_n(t) uniformly for t >= 0."""

    pi = arb.pi()
    return 2 * pi**2 * fourth_power_gaussian_tail(N_THETA + 1, pi)


def left_tail_bound(s: int, log_order: int) -> arb:
    """Bound the absolute x < -X_LEFT endpoint tail."""

    a = arb(s + 1)
    exponential = (-a * X_LEFT).exp()
    if log_order == 0:
        integral = exponential / a
    elif log_order == 1:
        integral = exponential * (arb(X_LEFT) / a + 1 / a**2)
    else:
        raise ValueError("only log orders zero and one are used")
    return source_global_bound() * integral


def finite_component_tail_bound(s: int, log_order: int) -> arb:
    """Bound omitted theta indices on the finite x interval."""

    a = arb(s + 1)
    integral_without_log = ((a * Y_RIGHT).exp() - (-a * X_LEFT).exp()) / a
    log_bound = 1 if log_order == 0 else max(X_LEFT, Y_RIGHT)
    return source_component_tail_bound() * log_bound * integral_without_log


def right_tail_bound(s: int, log_order: int) -> arb:
    """Bound the complete t > exp(Y_RIGHT) theta tail.

    Use q=pi*exp(4t), t=(1/4)log(q/pi), log(t)<=t<=q, and
    integral_q0^infinity q^a exp(-q)dq <=
    q0^a exp(-q0)/(1-a/q0).
    """

    pi = arb.pi()
    t0 = arb(Y_RIGHT).exp()
    q0 = pi * (4 * t0).exp()
    theta_ratio = 16 * (-3 * q0).exp()
    require(theta_ratio.upper() < 1, "right-tail theta ratio is not below one")
    source_factor = 2 * pi**2 / (1 - theta_ratio)
    exponent = arb(s + log_order) + arb(5) / 4
    require(exponent.upper() < q0.lower(), "incomplete-gamma denominator is not positive")
    gamma_tail = q0**exponent * (-q0).exp() / (1 - exponent / q0)
    return source_factor * gamma_tail / (4 * pi ** (arb(9) / 4))


@lru_cache(maxsize=None)
def moment_ball(s: int, log_order: int) -> tuple[arb, dict[str, arb]]:
    finite = finite_integral(s, log_order)
    errors = {
        "left": left_tail_bound(s, log_order),
        "theta": finite_component_tail_bound(s, log_order),
        "right": right_tail_bound(s, log_order),
    }
    radius = sum(errors.values(), arb(0))
    return finite + symmetric_error(radius), errors


def midpoint_radius(value: arb) -> tuple[arb, arb]:
    midpoint = (value.lower() + value.upper()) / 2
    radius = (value.upper() - value.lower()) / 2
    return midpoint, radius


def exact_dyadic(value: arb) -> dict[str, str | int]:
    """Serialize an exact binary endpoint without a decimal/float round-trip."""

    mantissa, exponent = value.man_exp()
    return {"mantissa": str(mantissa), "exponent2": int(exponent)}


def enclosure(value: arb) -> dict[str, object]:
    """Bounds mean mantissa * 2**exponent2; display is only for readability."""

    require(value.is_finite(), "cannot serialize a nonfinite enclosure")
    return {
        "lower_bound": exact_dyadic(value.lower()),
        "upper_bound": exact_dyadic(value.upper()),
        "display": value.str(25),
    }


def certificate() -> dict[str, object]:
    exponents = (0, 2, 4, 6, 8, 10, 12)
    moments: dict[int, arb] = {}
    log_moments: dict[int, arb] = {}
    moment_records: list[dict[str, object]] = []
    for s in exponents:
        print(f"Enclosing U({s}) and U'({s})...", file=sys.stderr, flush=True)
        moments[s], errors0 = moment_ball(s, 0)
        log_moments[s], errors1 = moment_ball(s, 1)
        require(moments[s].lower() > 0, f"U({s}) is not certified positive")
        moment_records.append({
            "s": s,
            "U": enclosure(moments[s]),
            "U_prime": enclosure(log_moments[s]),
            "omitted_absolute_error_bounds": {
                "U": {key: enclosure(value) for key, value in errors0.items()},
                "U_prime": {key: enclosure(value) for key, value in errors1.items()},
            },
        })

    values: list[arb] = []
    derivatives: list[arb] = []
    for u in NODES:
        lower = moments[2 * u - 2]
        upper = moments[2 * u]
        lower_log = log_moments[2 * u - 2]
        upper_log = log_moments[2 * u]
        value = (arb(u) - arb(1) / 2) * lower / upper
        derivative = value * (
            1 / (arb(u) - arb(1) / 2)
            + 2 * (lower_log / lower - upper_log / upper)
        )
        values.append(value)
        derivatives.append(derivative)

    loewner: list[list[arb]] = []
    for i, u in enumerate(NODES):
        row: list[arb] = []
        for j, v in enumerate(NODES):
            if i == j:
                entry = derivatives[i]
            else:
                entry = (values[i] - values[j]) / (u - v)
            row.append(entry)
        loewner.append(row)

    max_radius = arb(0)
    for row in loewner:
        for entry in row:
            _, radius = midpoint_radius(entry)
            if radius.upper() > max_radius.upper():
                max_radius = radius

    quadratic = arb(0)
    for i, qi in enumerate(VECTOR):
        for j, qj in enumerate(VECTOR):
            quadratic += qi * loewner[i][j] * qj

    require(max_radius.upper() < arb("1e-8"), "Loewner entry radius is too wide")
    require(quadratic.upper() < 0, "Loewner quadratic form is not certified negative")
    return {
        "status": "certified",
        "nodes": list(NODES),
        "witness": list(VECTOR),
        "moments": moment_records,
        "function_values": [
            {"u": u, "phi": enclosure(value), "phi_prime": enclosure(derivative)}
            for u, value, derivative in zip(NODES, values, derivatives)
        ],
        "matrix": {
            "entries": [[enclosure(entry) for entry in row] for row in loewner],
            "maximum_entry_radius": enclosure(max_radius),
            "radius_less_than_1e_minus_8": True,
            "certified_not_psd": True,
        },
        "quadratic_form": {**enclosure(quadratic), "certified_negative": True},
        "scope": {
            "excluded": [
                "Pick extension of this exact interpolation",
                "complete Bernstein extension of this exact interpolation",
                "this interpolation's KPS BP1 route",
                "positive Stieltjes representations of phi(u)/u or 1/phi(u)",
            ],
            "undecided": [
                "ordinary Bernstein status", "other interpolations",
                "other research routes", "Riemann Hypothesis",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    started = time.perf_counter()
    root = Path(__file__).resolve().parent
    result: dict[str, object] = {
        "schema_version": "mathematical-research.loewner-certificate.v1",
        "example": "loewner-obstruction",
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "dependencies": {
            "python": platform.python_version(),
            "python_flint": flint.__version__,
            "flint": flint.__FLINT_VERSION__,
            "flint_release": flint.__FLINT_RELEASE__,
        },
        "configuration": {
            "precision_decimal_digits": ctx.dps,
            "precision_bits": ctx.prec,
            "threads": ctx.threads,
            "theta_components": N_THETA,
            "x_interval": [-X_LEFT, Y_RIGHT],
            "relative_tolerance": "1e-52",
            "absolute_tolerance": "1e-52",
            "integration_segments": len(integration_segments()) - 1,
            "deg_limit": 160, "eval_limit": 200000, "depth_limit": 40,
            "use_heap": True,
        },
        "source_sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("check.py", "problem.json", "mathematics.md", "requirements.txt")
        },
        "bound_encoding": "Each endpoint is exactly mantissa * 2**exponent2. Decimal display strings are not used for decisions.",
    }
    try:
        result.update(certificate())
        exit_code = 0
    except (RuntimeError, ValueError, ArithmeticError) as error:
        result.update(status="not_certified", error=str(error))
        exit_code = 1
    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
