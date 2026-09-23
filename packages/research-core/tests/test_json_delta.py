from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.json_delta import (  # noqa: E402
    JsonPointerError,
    compare_json_pointer_delta,
    resolve_json_pointer,
)


class JsonDeltaTests(unittest.TestCase):
    def test_resolves_standard_segments_and_exact_id_selectors(self) -> None:
        document = {
            "a/b": {"~key": ["zero", "one"]},
            "claims": [{"id": "claim.B", "value": 2}, {"id": "claim.A"}],
        }

        self.assertEqual(resolve_json_pointer(document, "/a~1b/~0key/1"), "one")
        self.assertEqual(
            resolve_json_pointer(document, "/claims/@id=claim.B/value"), 2
        )
        self.assertIs(resolve_json_pointer(document, ""), document)

    def test_rejects_ambiguous_or_invalid_traversal(self) -> None:
        duplicate = {"claims": [{"id": "claim.X"}, {"id": "claim.X"}]}

        for document, pointer in (
            (duplicate, "/claims/@id=claim.X"),
            ({"items": []}, "/items/nope"),
            ({"value": 1}, "/value/child"),
            ({"~": 1}, "/~2"),
            ({}, "not-a-pointer"),
        ):
            with self.subTest(pointer=pointer):
                with self.assertRaises(JsonPointerError) as raised:
                    resolve_json_pointer(document, pointer)
                self.assertEqual(raised.exception.code, "json_pointer_invalid")

    def test_delta_keys_record_arrays_by_id_and_freezes_values(self) -> None:
        before = {
            "project": {"id": "project.rh", "as_of": "before"},
            "claims": [
                {"id": "claim.B", "value": 2},
                {"id": "claim.A", "value": 1},
            ],
        }
        after = {
            "project": {"id": "project.rh", "as_of": "after"},
            "claims": [
                {"id": "claim.C", "value": 3},
                {"id": "claim.B", "value": 4},
            ],
        }

        deltas = compare_json_pointer_delta(before, after)

        self.assertEqual(
            tuple(item.pointer for item in deltas),
            (
                "/claims/@id=claim.A",
                "/claims/@id=claim.B/value",
                "/claims/@id=claim.C",
                "/project/as_of",
            ),
        )
        self.assertEqual(
            tuple((item.change, item.identity) for item in deltas),
            (
                ("removed", "claim.A"),
                ("modified", "claim.B"),
                ("added", "claim.C"),
                ("modified", "project.rh"),
            ),
        )
        before["claims"][0]["value"] = 99
        self.assertEqual(deltas[1].before, 2)


if __name__ == "__main__":
    unittest.main()
