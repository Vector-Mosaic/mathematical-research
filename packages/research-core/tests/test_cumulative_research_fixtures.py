"""Static fixture integrity and exact finite oracle checks, not model trials.

The suite intentionally has no Mission, provider, network, process, or Store
imports. Its passing result cannot establish restored research behavior.
"""

from __future__ import annotations

import hashlib
import json
import math
import unittest
from datetime import datetime
from fractions import Fraction as F
from pathlib import Path
from unittest import mock

import cumulative_research_fixture_support as fixtures


def _determinant(rows: list[list[F]]) -> F:
    matrix = [row[:] for row in rows]
    result = F(1)
    for column in range(len(matrix)):
        pivot = next(
            (index for index in range(column, len(matrix)) if matrix[index][column]),
            None,
        )
        if pivot is None:
            return F(0)
        if pivot != column:
            matrix[pivot], matrix[column] = matrix[column], matrix[pivot]
            result = -result
        divisor = matrix[column][column]
        result *= divisor
        for row in range(column + 1, len(matrix)):
            ratio = matrix[row][column] / divisor
            for index in range(column + 1, len(matrix)):
                matrix[row][index] -= ratio * matrix[column][index]
    return result


def _chebyshev_coefficients(degree: int) -> list[F]:
    """Ascending coefficients of T_degree from its defining recurrence."""
    previous, current = [F(1)], [F(0), F(1)]
    if degree == 0:
        return previous
    for _ in range(1, degree):
        successor = [F(0)] + [2 * coefficient for coefficient in current]
        for index, coefficient in enumerate(previous):
            successor[index] -= coefficient
        previous, current = current, successor
    return current


def _root_power_sums(coefficients: list[F], highest: int) -> list[F]:
    """Newton identities; no rounded trigonometric node approximations."""
    degree = len(coefficients) - 1
    monic = [value / coefficients[-1] for value in coefficients]
    sums = [F(degree)]
    for order in range(1, highest + 1):
        if order <= degree:
            total = sum(
                (monic[degree - index] * sums[order - index] for index in range(1, order)),
                F(0),
            ) + order * monic[degree - order]
        else:
            total = sum(
                (monic[degree - index] * sums[order - index] for index in range(1, degree + 1)),
                F(0),
            )
        sums.append(-total)
    return sums


class CumulativeResearchFixtureTests(unittest.TestCase):
    def setUp(self):
        self.manifest = fixtures._read_json(fixtures.MANIFEST_PATH)

    def test_case_coverage_authority_and_separate_rubrics(self):
        ids = {case["case_id"] for case in self.manifest["cases"]}
        self.assertEqual(ids, {"J2", "L2", "R2", "R3", "R4", "U1", "U2", "U3", "C1"})
        rubrics = fixtures._read_json(
            fixtures.FIXTURE_ROOT / self.manifest["files"]["withheld_rubrics"]
        )
        self.assertEqual(set(rubrics["cases"]), ids)
        heldout = {case["case_id"] for case in self.manifest["cases"] if case["held_out"]}
        self.assertEqual(heldout, {"J2", "L2", "R3", "R4", "U2"})
        self.assertEqual(self.manifest["authority"]["model_bearing_execution"], "not_authorized")
        self.assertEqual(self.manifest["authority"]["production_effect"], "none")
        root = Path(__file__).resolve().parents[3]
        for path in self.manifest["owners"].values():
            self.assertTrue((root / path).exists(), path)

    def test_one_case_loader_never_reads_withheld_rubrics(self):
        with mock.patch.object(fixtures, "_read_json", wraps=fixtures._read_json) as reads:
            packet = fixtures.load_visible_case("U1")
        names = {call.args[0].name for call in reads.call_args_list}
        self.assertNotIn(self.manifest["files"]["heldout_inputs"], names)
        self.assertNotIn(self.manifest["files"]["withheld_rubrics"], names)
        self.assertEqual(set(packet), {"provider_initial", "provider_incoming", "store_seed", "initial_source_aliases"})
        self.assertNotIn("new_contribution", packet["provider_initial"])
        self.assertIn("contribution", packet["provider_incoming"])
        for case in ("J2", "L2", "R3", "R4", "U2"):
            with self.assertRaisesRegex(ValueError, "held-out"):
                fixtures.load_visible_case(case)
        with self.assertRaisesRegex(ValueError, "absent or ambiguous"):
            fixtures.load_visible_case("missing")

    def test_unlinked_archives_are_not_exposed_or_connected(self):
        for case_id in ("U1", "U2", "U3"):
            packet = fixtures.load_visible_case(case_id, include_held_out=True)
            exposed = json.dumps([packet["provider_initial"], packet["provider_incoming"]])
            self.assertEqual(packet["initial_source_aliases"], [])
            self.assertGreater(len(packet["store_seed"]), 1)
            for record in packet["store_seed"]:
                self.assertEqual(record["links"], [])
                self.assertNotIn(record["alias"], exposed)
                self.assertNotIn(record["title"], exposed)
                self.assertTrue(record["body"].strip())


    def test_future_comparison_cells_are_only_unrun_data(self):
        cells = fixtures.comparison_cells()
        self.assertEqual(len(cells), 54)
        self.assertEqual(len({cell["cell_id"] for cell in cells}), 54)
        self.assertEqual({cell["status"] for cell in cells}, {"not_authorized_not_run"})
        self.assertTrue(all(cell["fresh_state_required"] for cell in cells))
        for spec in self.manifest["cases"]:
            selected = [cell for cell in cells if cell["case_id"] == spec["case_id"]]
            self.assertEqual(len(selected), 6)
            for replica in (1, 2, 3):
                self.assertEqual(
                    {cell["arm"] for cell in selected if cell["replica"] == replica},
                    set(self.manifest["comparison"]["arms"]),
                )
            self.assertNotEqual(selected[0]["arm"], selected[2]["arm"])



    def test_l2_exact_moments_and_first_nonexact_degree(self):
        for count in range(1, 7):
            sums = _root_power_sums(_chebyshev_coefficients(count), 2 * count)
            for degree in range(2 * count):
                continuous = F(0) if degree % 2 else F(math.comb(degree, degree // 2), 2**degree)
                self.assertEqual(sums[degree] / count, continuous)
            first_error = sums[2 * count] / count - F(math.comb(2 * count, count), 4**count)
            self.assertEqual(first_error, -F(1, 2 ** (2 * count - 1)))
        self.assertEqual(F(3, 2) ** 2 * F(1, 2), F(9, 8))
        self.assertEqual(F(3, 2) ** 4 * F(3, 8), F(243, 128))
        self.assertEqual(-F(3, 2) ** 6 / 32, -F(729, 2048))

    def test_recomposition_gap_normalization_and_quantifier_witnesses(self):
        b, B, C, D, A = map(F, (1, 10, 20, 40, 80))
        self.assertTrue(all(gap >= 5 for gap in (B-b, C-B, D-C, A-D)))
        self.assertGreaterEqual(A, 4 * B)
        self.assertEqual(B * (D-C) / (C*D), F(1, 4))
        self.assertGreater(F(1, 4), F(1, 512))
        B, C, D = map(F, (1, 1024, 2048))
        eta = B * (1/C - 1/D)
        self.assertEqual(eta, F(1, 2048))
        self.assertLessEqual(eta, F(1, 1024))
        self.assertEqual((D/B) * eta, D/C - 1)
        self.assertGreater(D/C - 1, F(1, 512))
        for k in (2, 3, 4, 8):
            n, r = k**3, k**2
            self.assertEqual(r**3, n**2)
            self.assertEqual(1 - F(r*r, n), 1-k)
            self.assertLess(1-k, 0)
        for order in (2, 3, 4):
            n = order**4
            self.assertGreater(1-F(order*order, n), 0)

    def test_unlinked_gram_bridges_and_signed_negative(self):
        support, weights = (F(1, 2), F(1), F(3)), (F(1), F(2), F(1))
        moment = lambda power: sum((weight * point**power for point, weight in zip(support, weights)), F(0))
        for shift in (0, 1):
            for size in (1, 2, 3):
                self.assertGreater(_determinant([[moment(i+j+shift) for j in range(size)] for i in range(size)]), 0)
            self.assertEqual(_determinant([[moment(i+j+shift) for j in range(4)] for i in range(4)]), 0)
        signed = [2-2**power for power in range(3)]
        self.assertEqual(signed, [1, 0, -2])
        self.assertEqual(signed[0]*signed[2]-signed[1]**2, -2)

        samples, weights = (F(0), F(1), F(3)), (F(1), F(2), F(1))
        sites = (F(1), F(2), F(4), F(5))
        value = lambda z: sum((w/(z+t) for t, w in zip(samples, weights)), F(0))
        table = []
        for x in sites:
            row = []
            for y in sites:
                gram = sum((w/((x+t)*(y+t)) for t, w in zip(samples, weights)), F(0))
                secant = (value(x)-value(y))/(y-x) if x != y else sum((w/(x+t)**2 for t, w in zip(samples, weights)), F(0))
                self.assertEqual(secant, gram)
                row.append(secant)
            table.append(row)
        for size in (1, 2, 3):
            self.assertGreater(_determinant([row[:size] for row in table[:size]]), 0)
        self.assertEqual(_determinant(table), 0)

    def test_continuity_special_constant_is_not_subsumed(self):
        n, order, tolerance = 16, 2, F(1, 8)
        self.assertGreater(F(2**order, n), tolerance)
        self.assertLessEqual(F(1, n), tolerance)
        self.assertEqual(F(1, 8), tolerance)
        self.assertEqual(F(2**order, 32), tolerance)


if __name__ == "__main__":
    unittest.main()
