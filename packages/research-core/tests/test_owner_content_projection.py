from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core import mission_retrieval
from research_core.executive_orientation import semantic_owner_document
from research_core.json_support import canonical_json_bytes
from research_core.mission_interface import MissionInterface, _readable_content
from research_core.mission_operation_contract import (
    HISTORICAL_RELATIONSHIP_KINDS, HISTORICAL_SEARCH_FIELDS,
    HISTORICAL_SOURCE_FAMILIES, ROOT_RETRIEVAL_KINDS,
)
from research_core.owner_content_projection import (
    CHECKPOINT_PRESENCE_FIELDS, DELEGATED_FAMILY_KINDS,
    DELEGATED_LEGACY_DEFAULT_FIELDS, PROFILE_DIGEST, PROJECTION_FIELDS,
    PROJECTION_PROFILE, ROOT_DEFAULT_FIELDS, ROOT_KINDS, SEARCH_FIELDS,
    current_checkpoint_fields, matching_fields, project_delegated_artifact_fields,
    project_delegated_capture_fields, project_delegated_fields,
    project_delegated_owner_fields, project_root_capture_fields,
    project_root_owner_fields, projection_profile_digest,
)
from research_core.research_model import deep_thaw
from research_core.workspace_store import _DIRECT_CHECKPOINT_UNRESOLVED_FIELDS


def reference(kind: str, identity: str = "fixture", revision: int = 2) -> dict:
    return {"kind": kind, "identity": identity, "revision": revision, "payload_sha256": "a" * 64}


def artifact() -> dict:
    return {"ordinal": 0, "role": "output", "logical_name": "rare-artifact-match", "blob_sha256": "b" * 64}


def capture() -> dict:
    return {
        "capture_id": "fixture-capture", "project_id": "project.rh",
        "mission_id": "mission.rh", "executive_epoch_id": "epoch.one",
        "capture_kind": "output", "observation_id": "observation.one",
        "assignment_id": "rare-capture-match",
        "provenance": {"parent_thread_id": "parent", "child_thread_id": "child", "root_thread_id": "root"},
        "completion": {"state": "completed"}, "artifacts": [artifact()],
    }


def root_capture(*, pending: bool = True) -> dict:
    return {
        "id": "capture:fixture-capture", "kind": "capture", "title": "rare-capture-match",
        "capture_kind": "output", "origin_epoch_id": "epoch.one",
        "origin_epoch_state": "failed_before_checkpoint",
        "late_classification": "after_failed_terminal",
        "cut_relation": "after_latest_checkpoint",
        "pending_state": "current_pending" if pending else "fully_covered",
        "native_lineage": {"parent_thread_id": "parent", "child_thread_id": "child", "root_thread_id": "root"},
        "artifacts": [{"handle": "capture-artifact:fixture-capture#0", "ordinal": 0, "role": "output", "logical_name": "rare-artifact-match", "pending": pending}],
        "failed_interval_or_late_output": pending,
        "trust_class": "custody_descriptor", "completeness": "descriptor",
    }


def owner_documents() -> dict[str, dict]:
    exact = reference("evidence", "theorem", 1)
    return {
        "mission": {"purpose": "unique-mission", "lifecycle": "active", "effective": True, "autonomous": True},
        "strategy": {"integrated_comparison": "unique-strategy", "selected_bets": [], "owner_refs": [exact]},
        "branch": {"question": "unique-branch", "owner_refs": [exact]},
        "candidate": {"exact_statement": "unique-candidate", "normalization": "literal-normalization", "supporting_refs": [exact]},
        "context": {"purpose": "unique-context", "question": "parent", "indispensable_ground": [exact], "known_omissions": []},
        "evidence": {"subtype": "evidence_meaning", "subject": {"claim": "unique-evidence"}, "exact_scope": "literal-scope", "limitations": ["qualification"], "non_inferences": []},
        "session": {"lifecycle": "open", "selected_bet": {"bet": "unique-session"}, "context_ref": reference("context")},
        "capture-annotation": {"annotation_id": "fixture", "capture_id": "fixture-capture", "annotation_kind": "reviewed_no_current_semantic_delta", "exact_scope": "unique-capture-annotation", "lifecycle": "active"},
    }


class OwnerContentProjectionTests(unittest.TestCase):
    def test_profile_closes_every_existing_kind_field_and_default(self):
        self.assertEqual(ROOT_KINDS, ROOT_RETRIEVAL_KINDS)
        self.assertEqual(SEARCH_FIELDS, HISTORICAL_SEARCH_FIELDS)
        self.assertEqual(tuple(DELEGATED_FAMILY_KINDS), HISTORICAL_SOURCE_FAMILIES)
        self.assertEqual(set(PROJECTION_FIELDS["root"]), set(ROOT_KINDS))
        self.assertEqual(set(PROJECTION_FIELDS["delegated"]), set(DELEGATED_FAMILY_KINDS.values()))
        normalized = mission_retrieval._normalized({"mode": "search", "query": "x", "purpose": "fixture"})
        self.assertEqual(normalized["fields"], list(ROOT_DEFAULT_FIELDS))
        self.assertEqual(set(normalized["kinds"]), set(ROOT_KINDS))
        self.assertEqual(DELEGATED_LEGACY_DEFAULT_FIELDS, ("title", "content"))
        self.assertNotIn("capture-artifact", PROJECTION_FIELDS["root"])
        self.assertNotIn("mission", PROJECTION_FIELDS["delegated"])
        self.assertNotIn("session", PROJECTION_FIELDS["delegated"])

    def test_profile_digest_binds_closed_contract(self):
        self.assertEqual(PROFILE_DIGEST, hashlib.sha256(canonical_json_bytes(PROJECTION_PROFILE)).hexdigest())
        self.assertEqual(projection_profile_digest(), PROFILE_DIGEST)
        altered = deep_thaw(PROJECTION_PROFILE)
        altered["raw_body_search"] = True
        self.assertNotEqual(PROFILE_DIGEST, hashlib.sha256(canonical_json_bytes(altered)).hexdigest())
        with self.assertRaises(TypeError):
            PROJECTION_PROFILE["raw_body_search"] = True

    def test_all_root_owner_fields_match_actual_search_projector(self):
        for kind, document in owner_documents().items():
            with self.subTest(kind=kind):
                ref = reference(kind)
                # Independent oracle: the retrieval entrypoint now delegates to
                # the helper under test, so invoking it would be tautological.
                expected = {
                    "id": f"{kind}:fixture@2", "title": "fixture",
                    "content": canonical_json_bytes(semantic_owner_document(kind, document)).decode(),
                    "relationships": canonical_json_bytes([
                        {"id": f"{edge['kind']}:{edge['identity']}", "revision": edge["revision"]}
                        for edge in MissionInterface._exact_references_in(document)
                    ]).decode(),
                }
                actual = project_root_owner_fields(ref, document)
                self.assertEqual(actual, expected)
                self.assertEqual(matching_fields(actual, f"unique-{kind}", ROOT_DEFAULT_FIELDS), ("content",))

    def test_root_and_delegated_content_are_deliberately_not_unified(self):
        document = {"question": "math", "nonclaims": ["provenance is not proof"], "lifecycle": "open", "extra_math": "old-readable-field", "owner_refs": [reference("evidence")]}
        root = project_root_owner_fields(reference("branch"), document)
        delegated = project_delegated_owner_fields(reference("branch"), document)
        self.assertEqual(root["content"], canonical_json_bytes(semantic_owner_document("branch", document)).decode())
        self.assertEqual(delegated["content"], _readable_content(document))
        self.assertNotIn("old-readable-field", root["content"])
        self.assertIn("old-readable-field", delegated["content"])
        self.assertNotIn('"lifecycle"', delegated["content"])

    def test_context_v3_discovery_excludes_observed_current_head_only(self):
        historical = reference("evidence", "historical-theorem", 1)
        observed = reference("evidence", "historical-theorem", 2)
        def source(ref, roles, dependency=None):
            return {"reference": ref, "selection": None, "roles": roles, "why": "Exact selected use", "dependency": dependency}
        document = {
            "schema_version": 3, "kind": "first_class_context",
            "project_id": "project.rh", "mission_id": "mission.rh", "context_id": "fixture",
            "purpose": "Scientific understanding", "question": "Parent program",
            "treatments": {"bridge": {"question": "Does the theorem compose?", "account": "Qualified claim", "qualifications": ["Exact historical normalization"], "sources": [
                source(historical, ["reliance"], {"observed_current_reference": observed, "change_that_matters": "qualification", "dependent_judgment": "composition"}),
                source(reference("evidence", "recognized", 1), ["recognition"]),
                source(reference("evidence", "background", 1), ["history"]),
            ]}},
            "exposed_treatments": [], "known_omissions": [], "restricted_uses": [],
            "restrictions": [], "independence_treatment": {"method": "exact source comparison"},
        }
        fields = project_root_owner_fields(reference("context"), document)
        self.assertIn('"revision":1', fields["relationships"])
        self.assertNotIn('"revision":2', fields["relationships"])
        self.assertIn("evidence:recognized", fields["relationships"])
        self.assertIn("evidence:background", fields["relationships"])
        delegated = project_delegated_owner_fields(reference("context"), document)
        self.assertIn("evidence:historical-theorem@1", delegated["relationships"])
        self.assertNotIn("evidence:historical-theorem@2", delegated["relationships"])
        self.assertIn("evidence:recognized@1", delegated["relationships"])
        self.assertIn("evidence:background@1", delegated["relationships"])
        self.assertIn("/treatments/bridge/sources/0/reference", delegated["relationships"])

    def test_delegated_owner_and_annotation_relationship_parity(self):
        for kind, document in owner_documents().items():
            if kind in {"mission", "session", "strategy"}:
                continue
            with self.subTest(kind=kind):
                result = project_delegated_owner_fields(reference(kind), document)
                authoritative = document if kind != "capture-annotation" else {key: document[key] for key in ("annotation_id", "capture_id", "exact_scope")}
                item = {"id": result["id"], "kind": kind, "title": result["title"], "readable_content": result["content"]}
                expected = MissionInterface._historical_explicit_edges(item, HISTORICAL_RELATIONSHIP_KINDS, authoritative_document=authoritative)
                self.assertEqual(result["relationships"], canonical_json_bytes(expected).decode())
                self.assertIn(f"{kind}:fixture@1", result["relationships"])

    def test_strategy_cannot_silently_drop_formal_selectors(self):
        document = owner_documents()["strategy"]
        with self.assertRaisesRegex(ValueError, "formal selectors"):
            project_delegated_owner_fields(reference("strategy"), document)
        selectors = [{"selected_bet_sha256": "c" * 64, "bet": "formal-bet", "discriminator": "exact-discriminator", "purpose": "formal-purpose", "context_retrieval_handle": "context:formal@1"}]
        result = project_delegated_owner_fields(reference("strategy"), document, strategy_formal_requests=selectors)
        self.assertIn("c" * 64, result["content"])
        self.assertIn("context:formal@1", result["content"])
        self.assertIn("formal-bet", result["content"])

    def test_root_capture_fields_keep_dynamic_metadata_and_null_lineage(self):
        descriptor = root_capture()
        result = project_root_capture_fields(descriptor)
        self.assertEqual(result, {"id": descriptor["id"], "title": descriptor["title"], "content": canonical_json_bytes(descriptor).decode(), "relationships": canonical_json_bytes(descriptor["native_lineage"]).decode()})
        changed = project_root_capture_fields(root_capture(pending=False))
        self.assertNotEqual(result["content"], changed["content"])
        self.assertEqual(result["id"], changed["id"])
        self.assertIn("current_pending", result["content"])
        self.assertIn("fully_covered", changed["content"])
        descriptor["native_lineage"] = None
        self.assertEqual(project_root_capture_fields(descriptor)["relationships"], "null")

    def test_root_capture_descriptor_rejects_raw_body_extension(self):
        descriptor = root_capture()
        descriptor["body"] = "must not be indexed"
        with self.assertRaises(ValueError):
            project_root_capture_fields(descriptor)
        descriptor = root_capture()
        descriptor["artifacts"][0]["content_bytes"] = b"must not be indexed"
        with self.assertRaises(ValueError):
            project_root_capture_fields(descriptor)

    def test_delegated_capture_metadata_retains_exact_existing_title_and_lineage(self):
        metadata = capture()
        result = project_delegated_capture_fields(metadata)
        expected = {**metadata, "native_lineage": {"parent_thread_id": "parent", "child_thread_id": "child"}}
        self.assertEqual(result["id"], "capture:fixture-capture")
        self.assertEqual(result["title"], "fixture-capture")
        self.assertEqual(result["content"], _readable_content(expected))
        self.assertIn("rare-capture-match", result["content"])
        self.assertNotIn("current_pending", result["content"])
        self.assertEqual(metadata, capture())

    def test_delegated_artifact_is_metadata_only_even_for_raw_allowed_caller(self):
        result = project_delegated_artifact_fields("fixture-capture", artifact())
        self.assertEqual(result["id"], "capture-artifact:fixture-capture#0")
        self.assertEqual(result["title"], "rare-artifact-match")
        self.assertIn("withheld by delegated historical read policy", result["content"])
        self.assertNotIn("b" * 64, result["content"])
        with self.assertRaisesRegex(ValueError, "captured bodies"):
            project_delegated_fields({"id": result["id"], "kind": "capture-artifact", "title": result["title"], "readable_content": "private raw mathematical output"}, authoritative_document={"capture_id": "fixture-capture", "artifact": artifact()})
        with self.assertRaises(ValueError):
            project_delegated_artifact_fields("fixture-capture", {**artifact(), "content_bytes": b"raw"})

    def test_substring_matching_preserves_json_boundaries_casefold_and_short_terms(self):
        values = {"id": "branch:abc@1", "title": "Straße", "content": '{"a":"alpha","b":"beta"}', "relationships": "[]"}
        self.assertEqual(matching_fields(values, "STRASSE", SEARCH_FIELDS), ("title",))
        self.assertEqual(matching_fields(values, 'alpha","b":', SEARCH_FIELDS), ("content",))
        self.assertEqual(matching_fields(values, "@1", SEARCH_FIELDS), ("id",))
        self.assertEqual(matching_fields(values, "[", SEARCH_FIELDS), ("relationships",))
        self.assertEqual(matching_fields(values, "no-match", SEARCH_FIELDS), ())
        with self.assertRaises(ValueError):
            matching_fields(values, "", SEARCH_FIELDS)

    def test_checkpoint_presence_is_exact_existing_authored_field_predicate(self):
        self.assertEqual(deep_thaw(CHECKPOINT_PRESENCE_FIELDS), {key: list(value) for key, value in _DIRECT_CHECKPOINT_UNRESOLVED_FIELDS.items()})
        self.assertEqual(current_checkpoint_fields("candidate", {"gaps": ["missing lemma"], "obligations": [], "objections": ["counterexample"], "circularity_risks": []}), ("gaps", "objections"))
        self.assertEqual(current_checkpoint_fields("context", {"known_omissions": [], "treatments": {"open": {"question": "unfinished science"}}}), ())
        self.assertEqual(current_checkpoint_fields("context", {"known_omissions": ["missing source"]}), ("known_omissions",))
        self.assertEqual(current_checkpoint_fields("branch", {"known_omissions": ["not this owner field"]}), ())


if __name__ == "__main__":
    unittest.main()
