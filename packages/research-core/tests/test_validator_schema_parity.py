from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from research_core.validator import _schema_violations, validate_file, validate_state  # noqa: E402


from public_validator_fixture_support import make_validator_state  # noqa: E402


class ResearchContractVocabularyTests(unittest.TestCase):
    def test_named_treatment_map_validates_keys_values_and_nested_closed_shapes(self) -> None:
        schema = {
            "type": "object", "minProperties": 1,
            "propertyNames": {"type": "string", "minLength": 1},
            "additionalProperties": {
                "type": "object", "additionalProperties": False,
                "required": ["account"],
                "properties": {"account": {"type": "string", "minLength": 1}},
            },
        }
        self.assertEqual(_schema_violations({"parent": {"account": "Exact qualified account"}}, schema, schema, "$"), [])
        for malformed in ({}, {"": {"account": "Exact"}}, {"parent": {"account": ""}},
                          {"parent": {"account": "Exact", "silently_ignored": True}},
                          {"parent": "not a treatment"}):
            with self.subTest(malformed=malformed):
                self.assertTrue(_schema_violations(malformed, schema, schema, "$"))

    def test_additional_property_schema_does_not_override_declared_properties(self) -> None:
        schema = {"type": "object", "properties": {"revision": {"type": "integer"}},
                  "additionalProperties": {"type": "string"}}
        self.assertEqual(_schema_violations({"revision": 1, "account": "Exact"}, schema, schema, "$"), [])
        self.assertTrue(_schema_violations({"revision": "wrong"}, schema, schema, "$"))
        self.assertTrue(_schema_violations({"revision": 1, "account": 2}, schema, schema, "$"))

    def test_any_of_accepts_overlapping_valid_projections_but_not_unmatched_values(self) -> None:
        schema = {"anyOf": [{"type": "string", "minLength": 2},
                            {"type": "string", "pattern": "^a"}]}
        for valid in ("a", "bc", "abc"):
            self.assertEqual(_schema_violations(valid, schema, schema, "$"), [])
        for invalid in ("b", "", 5, {}):
            self.assertTrue(_schema_violations(invalid, schema, schema, "$"))
        self.assertTrue(_schema_violations("any", {"anyOf": []}, {"anyOf": []}, "$"))


class ResearchStateSchemaParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = make_validator_state()

    def _representative_paths(self) -> list[tuple[list[object], tuple[str, ...]]]:
        paused_claim = next(
            index
            for index, item in enumerate(self.state["claims"])
            if item["status"] == "paused"
        )
        closeout = next(
            index
            for index, item in enumerate(self.state["audit_receipts"])
            if item["receipt_kind"] == "campaign_closeout"
        )
        return [
            ([], ("schema_version", "kind", "project", "status_model", "canonical_objects", "obligations", "dag", "claims", "counterexamples", "sources", "audit_receipts")),
            (["project"], ("id", "title", "lifecycle_status", "proof_status", "as_of", "active_target")),
            (["status_model"], ("audit_verdicts", "claim_types", "dag_roles", "lifecycle_statuses", "rh_chain_statuses", "counterexample_record_kinds", "route_effects", "route_decisions", "receipt_kinds", "prohibited_inputs")),
            (["canonical_objects", 0], ("id", "symbol", "definition", "domain")),
            (["obligations", 0], ("id", "label", "statement", "proof_status", "lifecycle_status")),
            (["dag"], ("nodes", "edges", "first_dependency_gap", "immediate_target")),
            (["dag", "nodes", 0], ("id", "label", "status")),
            (["dag", "edges", 0], ("id", "from", "to", "status")),
            (["claims", 0], ("id", "title", "statement", "hypotheses", "status", "claim_type", "audit_verdict", "dag_role", "rh_chain_status", "depends_on", "implies", "citation_refs", "evidence_refs", "dag_node_refs", "dag_edge_refs", "proof_restrictions")),
            (["claims", paused_claim], ("revival_trigger",)),
            (["counterexamples", 0], ("id", "title", "record_kind", "exact_scope", "basis", "route_effect", "does_not_exclude", "revival_trigger", "status", "audit_verdict", "proof_restrictions", "evidence_refs")),
            (["sources", 0], ("id", "source_type")),
            (["audit_receipts", 0], ("id", "receipt_kind", "date", "verdict", "source_refs", "claim_refs", "evidence_refs", "tracked_receipt_path", "conclusion")),
            (["audit_receipts", closeout], ("campaign_id", "route_dispositions")),
            (["audit_receipts", closeout, "route_dispositions", 0], ("decision", "scope", "basis", "evidence_refs", "truth_effect", "non_inferences", "revival_trigger")),
        ]

    @staticmethod
    def _resolve(value: object, path: list[object]) -> dict:
        current = value
        for segment in path:
            current = current[segment]  # type: ignore[index]
        return current  # type: ignore[return-value]

    def test_every_required_field_shape_rejects_deletion(self) -> None:
        for path, fields in self._representative_paths():
            for field in fields:
                with self.subTest(path=path, field=field):
                    state = copy.deepcopy(self.state)
                    self._resolve(state, path).pop(field)
                    self.assertFalse(validate_state(state).ok)

    def test_every_required_field_shape_rejects_wrong_type_or_value(self) -> None:
        nullable = {"revival_trigger"}
        for path, fields in self._representative_paths():
            for field in fields:
                with self.subTest(path=path, field=field):
                    state = copy.deepcopy(self.state)
                    self._resolve(state, path)[field] = {} if field in nullable else None
                    self.assertFalse(validate_state(state).ok)

    def test_strict_file_loader_rejects_duplicate_and_nonfinite_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            duplicate = Path(directory) / "duplicate.json"
            duplicate.write_bytes(b'{"schema_version":2,"schema_version":2}')
            self.assertFalse(validate_file(duplicate).ok)
            self.assertIn("duplicate JSON key", validate_file(duplicate).errors[0].message)

            nonfinite = Path(directory) / "nonfinite.json"
            nonfinite.write_bytes(b'{"schema_version":NaN}')
            self.assertFalse(validate_file(nonfinite).ok)
            self.assertIn("non-finite JSON", validate_file(nonfinite).errors[0].message)

            invalid_utf8 = Path(directory) / "invalid-utf8.json"
            invalid_utf8.write_bytes(b'{"schema_version":"\xff"}')
            self.assertFalse(validate_file(invalid_utf8).ok)
            self.assertIn("unable to load JSON", validate_file(invalid_utf8).errors[0].message)

    def test_schema_date_and_pattern_constraints_are_enforced(self) -> None:
        invalid_date = copy.deepcopy(self.state)
        invalid_date["project"]["as_of"] = "2026-99-99"
        self.assertTrue(
            any("ISO 8601 calendar date" in item.message for item in validate_state(invalid_date).errors)
        )
        invalid_hash = copy.deepcopy(self.state)
        source = next(item for item in invalid_hash["sources"] if "sha256" in item)
        source["sha256"] = "not-a-hash"
        self.assertTrue(
            any("schema pattern" in item.message for item in validate_state(invalid_hash).errors)
        )

    def test_false_verdict_condition_requires_a_structured_refutation(self) -> None:
        state = copy.deepcopy(self.state)
        state["claims"][0]["audit_verdict"] = "false"
        state["claims"][0].pop("refutation", None)
        errors = validate_state(state).errors
        self.assertTrue(
            any("missing required schema field: refutation" in item.message for item in errors)
        )
        state["claims"][0]["refutation"] = None
        errors = validate_state(state).errors
        self.assertTrue(any("must have JSON type object" in item.message for item in errors))

    def test_terminal_admitted_result_schema_is_closed_and_exact(self) -> None:
        state = copy.deepcopy(self.state)
        project = state["project"]
        project["proof_status"] = "proved"
        project.pop("active_target")
        project["admitted_result"] = {
            "target": "riemann_hypothesis",
            "disposition": "proved",
            "theorem_or_counterexample_claim": "Every nontrivial zeta zero has real part 1/2.",
            "candidate_ref": {
                "mission_id": "mission.rh.test",
                "candidate_id": "candidate.rh.proof.test",
                "revision": 1,
                "digest_sha256": "a" * 64,
            },
            "admission_decision_ref": {
                "decision_id": "admission-decision.rh.proof.test",
                "digest_sha256": "b" * 64,
            },
        }
        self.assertTrue(validate_state(state).ok)

        for field in (
            "target",
            "disposition",
            "theorem_or_counterexample_claim",
            "candidate_ref",
            "admission_decision_ref",
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(state)
                invalid["project"]["admitted_result"].pop(field)
                self.assertFalse(validate_state(invalid).ok)

        unknown = copy.deepcopy(state)
        unknown["project"]["admitted_result"]["reviewer_count"] = 2
        self.assertTrue(
            any(
                item.location == "$.project.admitted_result.reviewer_count"
                and "unknown field under schema" in item.message
                for item in validate_state(unknown).errors
            )
        )


if __name__ == "__main__":
    unittest.main()
