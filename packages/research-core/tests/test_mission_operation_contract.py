from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.mission_operation_contract import (  # noqa: E402
    CHECKPOINT,
    HISTORICAL_READ_OPERATIONS,
    INTERPRET_MATERIAL,
    MISSION_MODEL_USAGE_OPERATION,
    MISSION_OPERATION_CONTRACT_SCHEMA_VERSION,
    MISSION_OPERATION_ORDER,
    MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
    MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
    MissionOperationContractError,
    ORIENT,
    RECORD_BRANCH,
    RECORD_CANDIDATE,
    RECORD_CONTEXT,
    RECORD_STRATEGY,
    RETRIEVE,
    SYNTHESIZE,
    admission_child_request_schema,
    executive_orientation_schema,
    mission_operation,
    mission_operation_contract,
    parse_semantic_request_bytes,
    project_mission_host_model_projection,
    project_cli_operations,
    project_model_operations,
    project_model_usage,
    project_operation_capabilities,
    semantic_request_schema,
    validate_admission_case_request,
    validate_admission_decision_grant_request,
    validate_admission_decision_request,
    validate_admission_review_grant_request,
    validate_admission_review_request,
    validate_candidate_a1_review_grant_request,
    validate_candidate_a1_review_request,
    validate_executive_orientation,
    validate_evidence_consequence_input,
    validate_historical_read_grant_request,
    validate_historical_read_request,
    validate_semantic_request,
    validate_semantic_result,
)
from research_core.research_model import deep_thaw  # noqa: E402


def _request(operation: str, input_value: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
        "operation": operation,
        "input": input_value,
    }


def _scientific_context_input() -> dict[str, object]:
    reference = {"kind": "context", "identity": "endpoint", "revision": 1,
                 "payload_sha256": "a" * 64}
    treatment = {
        "question": "Which consumer restrictions does the endpoint ingredient remove?",
        "account": "Retain the parent program while the normalized consumer reach is unresolved.",
        "qualifications": ["A lower physical-gap bound is not an upper mismatch estimate."],
        "sources": [{
            "reference": reference,
            "selection": {"mode": "treatments", "treatment_ids": ["physical-gap"]},
            "roles": ["reliance"], "why": "Use the exact historically established ingredient.",
            "dependency": {
                "observed_current_reference": {**reference, "revision": 2, "payload_sha256": "b" * 64},
                "change_that_matters": "A change to the literal normalization or domain.",
                "dependent_judgment": "Reconsider the consumer interface, not the parent program.",
            },
        }],
    }
    return {
        "mode": "scientific",
        "updates": [{
            "context_id": "context:mission-science", "expected_head": None,
            "create": {
                "schema_version": 3, "kind": "first_class_context",
                "project_id": "project.rh", "mission_id": "mission.fixture",
                "context_id": "mission-science", "purpose": "Preserve Mission-wide scientific understanding.",
                "question": "What survives and what follows across the Mission?",
                "treatments": {"endpoint-to-consumer": treatment}, "exposed_treatments": [],
                "known_omissions": [], "restricted_uses": [], "restrictions": [],
                "independence_treatment": {"meaning": "No independent proof review is claimed."},
            },
            "patch": None,
        }],
        "exposure_required": [{"context_id": "mission-science", "treatment_id": "endpoint-to-consumer"}],
    }


def _orientation() -> dict[str, object]:
    """Disposable contract input, never a production mathematical body."""
    return {
        "schema_version": "mathematical_research.executive_orientation.v3",
        "target": {
            "target": "riemann_hypothesis",
            "statement": "Every nontrivial zero of the Riemann zeta function has real part one half.",
            "canonical_status": "open",
        },
        "mission": {
            "handle": "mission:mission.fixture@1", "lifecycle": "active",
            "effective": True, "autonomous": True,
            "scientific_context_id": None,
            "purpose": {"objective": "Fixture objective", "proof_standard": "Exact proof",
                        "non_goals": [], "closeout_conditions": {
                            "strategy_mission_continuation": "closeout",
                            "semantic_effect": "none",
                        }},
        },
        "current_strategy": {
            "handle": "strategy:strategy.fixture@1", "mission_continuation": "continue",
            "integrated_comparison": "Fixture comparison", "causal_inputs": [],
            "serious_opportunities": [], "selected_bets": [], "attention_actions": [],
            "context_treatment": [], "creativity_treatment": [],
            "reconsideration_conditions": [], "reversal_conditions": [],
            "revival_conditions": [], "owner_refs": [], "formal_requests": [],
        },
        "scientific_context": {
            "state": "unbound", "binding": None, "root_reference": None,
            "known_omissions": [], "restricted_uses": [], "restrictions": [], "independence_treatment": None,
            "purpose": None, "question": None, "treatments": [],
            "source_changes": [], "unavailable": [],
        },
        "continuity": {
            "checkpoint": None, "mission_relation_to_checkpoint": "no_checkpoint",
            "strategy_relation_to_checkpoint": "no_checkpoint",
            "mission_strategy_changes_since_checkpoint": {"state": "no_checkpoint", "counts_by_kind": {}, "retrieve_call": None},
            "checkpoint_attention": {"unresolved_pointer_count": 0, "retrieve_call": None},
        },
        "proof_attention": {"open_candidate_a1": [], "admitted_result": None},
        "formal_attention": [],
        "retrieval": {"usage_call": {"operation": "usage", "input": {"for_operation": "retrieve"}},
                      "available_modes": ["read", "search", "inventory", "checkpoint", "changes_since_checkpoint", "hooks", "captures", "proof_attention"],
                      "recommended_calls": []},
    }


def _orientation_with_source_qualification(
    kind: str, qualification: dict[str, object],
) -> dict[str, object]:
    orientation = _orientation()
    document = _scientific_context_input()["updates"][0]["create"]
    root = {"kind": "context", "identity": "mission-science", "revision": 4,
            "payload_sha256": "c" * 64}
    cited = {"kind": kind, "identity": "fixture", "revision": 1,
             "payload_sha256": "a" * 64}
    current = {**cited, "revision": 2, "payload_sha256": "b" * 64}
    selection = qualification.get("selection") if kind == "context" else None
    source = {"reference": cited, "selection": selection, "roles": ["reliance"],
              "why": "Preserve this exact authored source use.", "dependency": None}
    treatment = {**document["treatments"]["endpoint-to-consumer"], "sources": [source]}
    orientation["mission"]["scientific_context_id"] = root["identity"]
    orientation["scientific_context"] = {
        "state": "available", "binding": {"context_id": root["identity"]},
        "root_reference": root,
        **{field: deep_thaw(document[field]) for field in (
            "purpose", "question", "known_omissions", "restricted_uses",
            "restrictions", "independence_treatment",
        )},
        "treatments": [{"context_reference": root, "treatment_id": "endpoint-to-consumer",
                        "content": treatment}],
        "source_changes": [{
            "cited_reference": cited, "current_reference": current,
            "selection": selection, "state": "advanced_same_identity",
            "affected": [{"context_reference": root, "treatment_id": "endpoint-to-consumer",
                          "roles": ["reliance"]}],
            "qualification": deep_thaw(qualification), "qualification_ref": None,
            "read_call": {"operation": "retrieve", "input": {
                "mode": "read", "purpose": "Read the exact changed source.",
                "ids": [f"{kind}:fixture@2"],
            }},
        }],
        "unavailable": [],
    }
    return orientation


class MissionOperationContractTests(unittest.TestCase):
    def test_scientific_context_create_and_patch_preserve_exact_source_selectors(self) -> None:
        create = _request(RECORD_CONTEXT, _scientific_context_input())
        self.assertEqual(deep_thaw(validate_semantic_request(create)), create)
        patch = deep_thaw(create)
        update = patch["input"]["updates"][0]
        treatment = update["create"]["treatments"]["endpoint-to-consumer"]
        update["create"] = None
        update["expected_head"] = {"kind": "context", "identity": "mission-science",
                                   "revision": 4, "payload_sha256": "c" * 64}
        update["patch"] = {
            "insert": {}, "replace": {"endpoint-to-consumer": treatment}, "remove": [],
            "exposure_add": [], "exposure_remove": [], "metadata_replace": {},
        }
        accepted = deep_thaw(validate_semantic_request(patch))
        self.assertEqual(accepted, patch)
        source = accepted["input"]["updates"][0]["patch"]["replace"]["endpoint-to-consumer"]["sources"][0]
        self.assertEqual(source["reference"]["revision"], 1)
        self.assertEqual(source["dependency"]["observed_current_reference"]["revision"], 2)
        self.assertEqual(source["selection"], {"mode": "treatments", "treatment_ids": ["physical-gap"]})

    def test_scientific_treatment_map_schema_validates_values_and_exact_references(self) -> None:
        base = _request(RECORD_CONTEXT, _scientific_context_input())
        malformed = []
        missing_account = deep_thaw(base)
        del missing_account["input"]["updates"][0]["create"]["treatments"]["endpoint-to-consumer"]["account"]
        malformed.append(missing_account)
        invalid_map_value = deep_thaw(base)
        invalid_map_value["input"]["updates"][0]["create"]["treatments"]["endpoint-to-consumer"] = "not a treatment"
        malformed.append(invalid_map_value)
        missing_selection = deep_thaw(base)
        del missing_selection["input"]["updates"][0]["create"]["treatments"]["endpoint-to-consumer"]["sources"][0]["selection"]
        malformed.append(missing_selection)
        empty_selection = deep_thaw(base)
        empty_selection["input"]["updates"][0]["create"]["treatments"]["endpoint-to-consumer"]["sources"][0]["selection"]["treatment_ids"] = []
        malformed.append(empty_selection)
        invalid_digest = deep_thaw(base)
        invalid_digest["input"]["updates"][0]["create"]["treatments"]["endpoint-to-consumer"]["sources"][0]["reference"]["payload_sha256"] = "not-an-exact-digest"
        malformed.append(invalid_digest)
        empty_independence = deep_thaw(base)
        empty_independence["input"]["updates"][0]["create"]["independence_treatment"] = {}
        malformed.append(empty_independence)
        empty_name = deep_thaw(base)
        treatments = empty_name["input"]["updates"][0]["create"]["treatments"]
        treatments[""] = treatments.pop("endpoint-to-consumer")
        malformed.append(empty_name)
        for index, request in enumerate(malformed):
            with self.subTest(case=index), self.assertRaises(MissionOperationContractError) as caught:
                validate_semantic_request(request)
            self.assertEqual(caught.exception.code, "mission_operation_request_invalid")

    def test_scientific_exact_reference_exceptions_do_not_exempt_nested_owner_facts(self) -> None:
        base = _request(RECORD_CONTEXT, _scientific_context_input())
        for key, value in (("writer_epoch", 3), ("command_id", "caller-selected"),
                           ("payload_sha256", "d" * 64), ("mission_id", "another-mission")):
            request = deep_thaw(base)
            request["input"]["updates"][0]["create"]["independence_treatment"][key] = value
            with self.subTest(key=key), self.assertRaises(MissionOperationContractError) as caught:
                validate_semantic_request(request)
            self.assertEqual(caught.exception.code, "mission_operation_owner_fact_forbidden")
            self.assertIn("independence_treatment", caught.exception.location)
        unrelated = _request(ORIENT, {"reference": {"kind": "context", "identity": "endpoint",
                                                   "revision": 1, "payload_sha256": "a" * 64}})
        with self.assertRaises(MissionOperationContractError) as caught:
            validate_semantic_request(unrelated)
        self.assertEqual(caught.exception.code, "mission_operation_owner_fact_forbidden")

    def test_this_evidence_and_evidence_head_are_confined_to_paired_continuation(self) -> None:
        ordinary = _request(RECORD_CONTEXT, _scientific_context_input())
        source = ordinary["input"]["updates"][0]["create"]["treatments"]["endpoint-to-consumer"]["sources"][0]
        source["reference"] = {"source": "this_evidence"}
        source["selection"] = None
        source["dependency"] = None
        with self.assertRaises(MissionOperationContractError):
            validate_semantic_request(ordinary)
        continuation = deep_thaw(ordinary["input"])
        del continuation["mode"]
        evidence_input = deep_thaw(project_model_usage(for_operation=INTERPRET_MATERIAL))["examples"][0]["call"]["input"]
        evidence_input["scientific_continuation"] = continuation
        evidence_input["expected_head"] = {"kind": "evidence", "identity": "meaning", "revision": 1,
                                           "payload_sha256": "d" * 64}
        paired = _request(INTERPRET_MATERIAL, evidence_input)
        self.assertEqual(deep_thaw(validate_semantic_request(paired)), paired)
        unpaired = deep_thaw(paired)
        del unpaired["input"]["scientific_continuation"]
        with self.assertRaises(MissionOperationContractError) as caught:
            validate_semantic_request(unpaired)
        self.assertEqual(caught.exception.code, "mission_operation_owner_fact_forbidden")
        forged = deep_thaw(paired)
        forged["input"]["scientific_continuation"]["updates"][0]["create"]["independence_treatment"]["writer_epoch"] = 5
        with self.assertRaises(MissionOperationContractError) as caught:
            validate_semantic_request(forged)
        self.assertEqual(caught.exception.code, "mission_operation_owner_fact_forbidden")

    def test_publication_lookup_is_exact_root_read_not_a_child_command_grant(self) -> None:
        lookup = _request(RETRIEVE, {"mode": "publication_result", "command_id": "scientific-publication.fixture"})
        self.assertEqual(deep_thaw(validate_semantic_request(lookup)), lookup)
        with self.assertRaises(MissionOperationContractError):
            validate_historical_read_request(lookup)
        for field in ("project_commit", "writer_epoch", "root_digest"):
            altered = deep_thaw(lookup)
            altered["input"][field] = 7
            with self.subTest(field=field), self.assertRaises(MissionOperationContractError) as caught:
                validate_semantic_request(altered)
            self.assertEqual(caught.exception.code, "mission_operation_owner_fact_forbidden")

    def test_paired_synthesis_validation_is_local_before_independent_candidate_siblings(self) -> None:
        strategy_schema = deep_thaw(project_model_usage(for_operation=RECORD_STRATEGY))["input_schema"]
        causal_input = strategy_schema["properties"]["causal_inputs"]["items"]
        self.assertEqual(set(causal_input["properties"]), {"source", "decision_consequence"})
        continuation = _scientific_context_input()
        del continuation["mode"]
        source = continuation["updates"][0]["create"]["treatments"]["endpoint-to-consumer"]["sources"][0]
        source.update(reference={"source": "this_evidence"}, selection=None, dependency=None)
        consequence = {
            "kind": "evidence", "evidence_id": "evidence:combined-endpoint",
            "statement": "The exact endpoint construction survives the scoped correction.",
            "scope": "The authored normalized domain.", "strength": "conditional_theorem",
            "semantic_role": "surviving ingredient", "non_inferences": ["No complete proof is asserted."],
            "expected_head": None, "scientific_continuation": continuation,
        }
        self.assertEqual(deep_thaw(validate_evidence_consequence_input(consequence)), consequence)
        synthesis = deep_thaw(project_model_usage(for_operation=SYNTHESIZE))["examples"][0]["call"]["input"]
        candidate = deep_thaw(project_model_usage(for_operation=RECORD_CANDIDATE))["examples"][0]["call"]["input"]
        candidate["complete_target_claim"] = {"target": "riemann_hypothesis", "disposition": "proof"}
        for invalid_group in ({"updates": "malformed"}, {"writer_epoch": 99}, None):
            malformed = deep_thaw(consequence)
            malformed["scientific_continuation"] = invalid_group
            synthesis["consequences"] = [malformed, {"kind": "candidate", "candidate": candidate}]
            request = _request(SYNTHESIZE, synthesis)
            with self.subTest(invalid_group=invalid_group):
                self.assertEqual(deep_thaw(validate_semantic_request(request)), request)
                with self.assertRaises(MissionOperationContractError):
                    validate_evidence_consequence_input(malformed)
        # Schema admission preserves the sibling for its owning loop; it does
        # not prove that loop wrote a Candidate or committed any publication.
        self.assertEqual(request["input"]["consequences"][1]["candidate"]["complete_target_claim"]["disposition"], "proof")

    def test_candidate_context_selection_is_optional_and_preserves_explicit_other_sources(self) -> None:
        candidate = deep_thaw(project_model_usage(for_operation=RECORD_CANDIDATE))["examples"][0]["call"]["input"]
        candidate["supporting_refs"] = [
            {"id": "context:mission-science", "revision": 4,
             "selection": {"mode": "treatments", "treatment_ids": ["endpoint-to-consumer"]}},
            {"id": "evidence:independent-objection", "revision": 1},
        ]
        request = _request(RECORD_CANDIDATE, candidate)
        self.assertEqual(deep_thaw(validate_semantic_request(request)), request)
        implicit_whole = deep_thaw(request)
        del implicit_whole["input"]["supporting_refs"][0]["selection"]
        self.assertEqual(deep_thaw(validate_semantic_request(implicit_whole)), implicit_whole)
        explicit_whole = deep_thaw(request)
        explicit_whole["input"]["supporting_refs"][0]["selection"] = {"mode": "whole_context"}
        self.assertEqual(deep_thaw(validate_semantic_request(explicit_whole)), explicit_whole)
        missing_key = deep_thaw(request)
        missing_key["input"]["supporting_refs"][0]["selection"]["treatment_ids"] = []
        with self.assertRaises(MissionOperationContractError):
            validate_semantic_request(missing_key)

    def test_historical_inventory_and_search_scope_remain_explicit_and_child_bounded(self) -> None:
        for mode in ("history_inventory", "history_search"):
            for scope in (None, "retained_history", "current_at_cut"):
                input_value = {"mode": mode, "purpose": "Inspect the authorized fixed-cut scientific family.",
                               "source_families": ["branches", "contexts", "evidence"], "page_size": 1}
                if mode == "history_search":
                    input_value.update(query="physical gap", fields=["content"])
                if scope is not None:
                    input_value["revision_scope"] = scope
                request = _request(RETRIEVE, input_value)
                with self.subTest(mode=mode, scope=scope):
                    self.assertEqual(deep_thaw(validate_historical_read_request(request)), request)
                    with self.assertRaises(MissionOperationContractError):
                        validate_semantic_request(request)
                invalid = deep_thaw(request)
                invalid["input"]["revision_scope"] = "latest_live_state"
                with self.assertRaises(MissionOperationContractError):
                    validate_historical_read_request(invalid)

    def test_scientific_orientation_preserves_root_and_selected_source_qualifications(self) -> None:
        orientation = _orientation()
        document = _scientific_context_input()["updates"][0]["create"]
        treatment = document["treatments"]["endpoint-to-consumer"]
        source = treatment["sources"][0]
        root = {"kind": "context", "identity": "mission-science", "revision": 4,
                "payload_sha256": "c" * 64}
        root_qualifications = {
            "known_omissions": ["A complete-target proof remains absent."],
            "restricted_uses": ["ROOT_RESTRICTION: preserve the scoped parent program."],
            "restrictions": ["Retain exact consumer hypotheses."],
            "independence_treatment": {"meaning": "Review remains separately owned."},
        }
        orientation["mission"]["scientific_context_id"] = "mission-science"
        orientation["scientific_context"] = {
            "state": "available", "binding": {"context_id": "mission-science"}, "root_reference": root,
            "purpose": document["purpose"], "question": document["question"], **root_qualifications,
            "treatments": [{"context_reference": root, "treatment_id": "endpoint-to-consumer", "content": treatment}],
            "source_changes": [{
                "cited_reference": source["reference"], "selection": source["selection"],
                "current_reference": source["dependency"]["observed_current_reference"],
                "state": "advanced_same_identity",
                "affected": [{"context_reference": root, "treatment_id": "endpoint-to-consumer", "roles": ["reliance"]}],
                "qualification": {
                    "selection": source["selection"],
                    "context_qualifications": {**root_qualifications,
                                               "restricted_uses": ["SOURCE_RESTRICTION: only the stated real domain."]},
                    "treatments": {"physical-gap": {
                        "question": "Which domain supports the corrected gap normalization?",
                        "account": "The estimate applies to real x >= 1, not all complex x.",
                        "qualifications": ["No upper mismatch estimate follows."], "sources": [],
                    }},
                },
                "qualification_ref": None,
                "read_call": {"operation": "retrieve", "input": {"mode": "read", "purpose": "Read the exact changed source.", "ids": ["context:endpoint@2"]}},
            }],
            "unavailable": [],
        }
        self.assertEqual(deep_thaw(validate_executive_orientation(orientation)), orientation)
        self.assertIn("ROOT_RESTRICTION", json.dumps(orientation))
        self.assertIn("SOURCE_RESTRICTION", json.dumps(orientation))
        for field in ("restricted_uses", "known_omissions", "restrictions", "independence_treatment"):
            omitted = deep_thaw(orientation)
            del omitted["scientific_context"]["source_changes"][0]["qualification"]["context_qualifications"][field]
            with self.subTest(field=field), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(omitted)
        unrelated = deep_thaw(orientation)
        qualifications = unrelated["scientific_context"]["source_changes"][0]["qualification"]["treatments"]
        qualifications["unselected"] = deep_thaw(qualifications["physical-gap"])
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(unrelated)
        omitted = deep_thaw(orientation)
        omitted["scientific_context"]["source_changes"][0]["qualification"]["treatments"] = {}
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(omitted)

        # Direct deeper membership needs its enclosing qualifications even
        # without a changed-source exposure for that owner.
        deeper = {"context_reference": {**root, "identity": "consumer", "revision": 2,
                                        "payload_sha256": "d" * 64},
                  "treatment_id": "unfinished-consumer",
                  "content": {**deep_thaw(treatment), "sources": []},
                  "context_qualifications": {**deep_thaw(root_qualifications),
                                             "restrictions": ["DEEPER_RESTRICTION: fixed-order only."]}}
        orientation["scientific_context"]["treatments"].append(deeper)
        self.assertEqual(deep_thaw(validate_executive_orientation(orientation)), orientation)
        for field in (None, *root_qualifications):
            omitted = deep_thaw(orientation)
            entry = omitted["scientific_context"]["treatments"][1]
            if field is None:
                del entry["context_qualifications"]
            else:
                del entry["context_qualifications"][field]
            with self.subTest(omitted=field), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(omitted)
        for invalid_metadata in (
            {**deeper["context_qualifications"], "unrelated_treatments": {}},
            {**deeper["context_qualifications"], "restrictions": "not an array"},
            {**deeper["context_qualifications"], "independence_treatment": {}},
        ):
            malformed = deep_thaw(orientation)
            malformed["scientific_context"]["treatments"][1]["context_qualifications"] = invalid_metadata
            with self.subTest(invalid_metadata=invalid_metadata), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(malformed)
        duplicate = deep_thaw(orientation)
        duplicate["scientific_context"]["treatments"][0]["context_qualifications"] = root_qualifications
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(duplicate)
        drifted = deep_thaw(orientation)
        drifted["scientific_context"]["treatments"][0]["context_reference"]["payload_sha256"] = "e" * 64
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(drifted)
        for path in (
            "/strategy_ground/0/summary/treatments/unfinished-consumer",
            "/scientific_context/treatments/0/content",
        ):
            for keep_content in (False, True):
                malformed = deep_thaw(orientation)
                entry = malformed["scientific_context"]["treatments"][1]
                if not keep_content:
                    del entry["content"]
                entry["content_ref"] = {"orientation_path": path}
                with self.subTest(path=path, keep_content=keep_content), self.assertRaises(MissionOperationContractError):
                    validate_executive_orientation(malformed)

    def test_continuity_change_summary_is_closed_to_mission_and_strategy_roots(self) -> None:
        orientation = _orientation()
        orientation["continuity"]["mission_strategy_changes_since_checkpoint"] = {
            "state": "present",
            "counts_by_kind": {
                "mission": {"new": 0, "advanced": 1, "retired": 0},
                "strategy": {"new": 1, "advanced": 0, "retired": 1},
            },
            "retrieve_call": None,
        }
        self.assertEqual(deep_thaw(validate_executive_orientation(orientation)), orientation)

        stale_name = deep_thaw(orientation)
        stale_name["continuity"]["owner_changes_since_checkpoint"] = stale_name[
            "continuity"
        ].pop("mission_strategy_changes_since_checkpoint")
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(stale_name)

        overbroad_count = deep_thaw(orientation)
        overbroad_count["continuity"]["mission_strategy_changes_since_checkpoint"][
            "counts_by_kind"
        ]["candidate"] = {"new": 1, "advanced": 0, "retired": 0}
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(overbroad_count)

    def test_orientation_v3_rejects_retired_ground_and_version(self) -> None:
        orientation = _orientation()
        self.assertEqual(deep_thaw(validate_executive_orientation(orientation)), orientation)
        self.assertEqual(executive_orientation_schema()["properties"]["schema_version"]["const"],
                         "mathematical_research.executive_orientation.v3")
        self.assertNotIn("strategy_ground", orientation)
        retired_version = deep_thaw(orientation)
        retired_version["schema_version"] = "mathematical_research.executive_orientation.v2"
        retired_ground = deep_thaw(orientation)
        retired_ground["strategy_ground"] = []
        for malformed in (retired_version, retired_ground):
            with self.subTest(malformed=malformed), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(malformed)

    def test_source_qualification_accepts_known_session_and_annotation_projection_fields(self) -> None:
        from research_core.executive_orientation import source_qualification_document

        reference = lambda kind: {"kind": kind, "identity": f"{kind}.1", "revision": 1, "payload_sha256": "a" * 64}
        documents = {
            "session": {
                "lifecycle": "terminal", "mission_ref": reference("mission"),
                "strategy_ref": reference("strategy"), "selected_bet_sha256": "b" * 64,
                "selected_bet": {"bet": "Retain this exact selected bet."},
                "context_ref": reference("context"), "terminal_binding": {
                    "attempt_result_ref": {"attempt_id": "attempt.1", "attempt_state": "completed"},
                    "raw_capture_ref": {"capture_id": "capture.1"},
                },
            },
            "capture-annotation": {
                "annotation_id": "annotation.1", "capture_id": "capture.1",
                "exact_scope": {"literal": "Keep arbitrary mathematical JSON intact."},
                "lifecycle": "active", "annotation_kind": "scope_classification",
            },
        }
        for kind, document in documents.items():
            with self.subTest(kind=kind):
                projected = source_qualification_document(kind, document)
                self.assertEqual(set(projected), set(document))
                orientation = _orientation_with_source_qualification(kind, projected)
                self.assertEqual(deep_thaw(validate_executive_orientation(orientation)), orientation)
                malformed = _orientation_with_source_qualification(
                    kind, {**projected, "undeclared_owner_machinery": "must reject"},
                )
                with self.assertRaises(MissionOperationContractError):
                    validate_executive_orientation(malformed)

    def test_source_qualification_is_closed_and_discriminated_by_exact_kind(self) -> None:
        from research_core.executive_orientation import source_qualification_document

        documents_and_cross_kind_fields = {
            "mission": ({"lifecycle": "active", "effective": True, "autonomous": True, "purpose": {"literal": "Mission purpose"}}, "selected_bets"),
            "strategy": ({"mission_continuation": "continue", "integrated_comparison": "Exact comparison", "owner_refs": []}, "question"),
            "branch": ({"question": "Fixture branch question", "revival_conditions": [], "nonclaims": ["Not a theorem"]}, "purpose"),
            "candidate": ({"exact_statement": "Fixture Candidate", "limitations": ["Conditional"]}, "known_omissions"),
            "evidence": ({"subtype": "evidence_meaning", "subject": {"statement": "Fixture result", "semantic_role": "conditional_result"}, "exact_scope": "Fixture scope", "rigor": "conditional_theorem", "limitations": ["Conditional"], "non_inferences": ["Not RH"]}, "standing"),
            "session": ({"lifecycle": "open", "selected_bet": {"literal": "Fixture bet"}}, "exact_statement"),
            "capture-annotation": ({"annotation_id": "fixture", "capture_id": "fixture", "exact_scope": "Fixture scope", "lifecycle": "active", "annotation_kind": "scope_classification"}, "question"),
        }
        for kind, (document, foreign_field) in documents_and_cross_kind_fields.items():
            with self.subTest(kind=kind):
                qualification = source_qualification_document(kind, document)
                orientation = _orientation_with_source_qualification(kind, qualification)
                self.assertEqual(deep_thaw(validate_executive_orientation(orientation)), orientation)
                for field in (foreign_field, "arbitrary_future_field"):
                    faulty = _orientation_with_source_qualification(kind, {**qualification, field: []})
                    with self.assertRaises(MissionOperationContractError) as caught:
                        validate_executive_orientation(faulty)
                    self.assertEqual(caught.exception.code, "executive_orientation_invalid")
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(_orientation_with_source_qualification("capture", {}))
        # This body passes the outer qualification union as a Candidate, but
        # cannot qualify a Branch source with the same exact reference fields.
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(_orientation_with_source_qualification(
                "branch", {"exact_statement": "A Candidate qualification is not a Branch account."},
            ))

    def test_scientific_orientation_rejects_unselected_inventory_fields(self) -> None:
        orientation = _orientation_with_source_qualification("branch", {"question": "Fixture question"})
        for field in ("owner_source_references", "immutable_historical_references", "untrusted_material_locators"):
            malformed = deep_thaw(orientation)
            malformed["scientific_context"][field] = []
            with self.subTest(field=field), self.assertRaises(MissionOperationContractError) as caught:
                validate_executive_orientation(malformed)
            self.assertEqual(caught.exception.code, "executive_orientation_invalid")

    def test_evidence_source_qualification_preserves_declared_science_and_rejects_future_fields(self) -> None:
        qualification = {"subtype": "evidence_meaning", "subject": {
            "statement": "Keep this exact statement.", "semantic_role": "conditional_result",
        }, "exact_scope": "Exact scope", "rigor": "conditional_theorem",
            "limitations": ["Required hypothesis"], "non_inferences": ["No complete claim"]}
        for consequence in (None, "This exact decision consequence remains optional."):
            subject = dict(qualification["subject"])
            if consequence is not None:
                subject["decision_consequence"] = consequence
            subject.update(objects=["Exact object"], hypotheses=["Required hypothesis"],
                           domain="Real x >= 1", normalization="Divide by the exact scale.",
                           quantifiers="For every x in the stated domain.",
                           parent_bet_outcome="The parent restriction remains.", standing="conditional")
            orientation = _orientation_with_source_qualification("evidence", {**qualification, "subject": subject})
            self.assertEqual(deep_thaw(validate_executive_orientation(orientation)), orientation)
        for field in ("authority_basis", "dependencies", "interpreted_owner_inputs", "future_subject_field"):
            with self.subTest(field=field), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(_orientation_with_source_qualification("evidence", {
                    **qualification, "subject": {**qualification["subject"], field: []},
                }))

    def test_candidate_source_qualification_excludes_graph_and_proof_attention_alias(self) -> None:
        ordinary = {"exact_statement": "Fixture statement", "complete_target_claim": {"disposition": "proof"}}
        validate_executive_orientation(_orientation_with_source_qualification("candidate", ordinary))
        for field in ("argument_edges", "argument_steps", "proof_body"):
            with self.subTest(field=field), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(_orientation_with_source_qualification("candidate", {**ordinary, field: []}))
        for disposition in ("proof", "disproof", "legacy_unspecified"):
            metadata = {"treatment": "see_proof_attention", "claim_disposition": disposition}
            with self.subTest(disposition=disposition), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(_orientation_with_source_qualification("candidate", metadata))

    def test_source_qualification_reuse_requires_direct_exact_inline_target(self) -> None:
        document = _scientific_context_input()["updates"][0]["create"]
        selection = {"mode": "treatments", "treatment_ids": ["physical-gap"]}
        qualification = {
            "selection": selection,
            "context_qualifications": {field: deep_thaw(document[field]) for field in (
                "known_omissions", "restricted_uses", "restrictions", "independence_treatment",
            )},
            "treatments": {"physical-gap": {
                "question": "Which domain supports the corrected gap normalization?",
                "account": "The estimate applies to real x >= 1, not all complex x.",
                "qualifications": ["No upper mismatch estimate follows."], "sources": [],
            }},
        }
        orientation = _orientation_with_source_qualification("context", qualification)
        science = orientation["scientific_context"]
        first = science["source_changes"][0]
        first["current_reference"].update(revision=3, payload_sha256="d" * 64)
        first["read_call"]["input"]["ids"] = ["context:fixture@3"]
        second = deep_thaw(first)
        second["cited_reference"].update(revision=2, payload_sha256="e" * 64)
        second["qualification"] = None
        second["qualification_ref"] = {"orientation_path": "/scientific_context/source_changes/0/qualification"}
        science["source_changes"].append(second)
        source = deep_thaw(science["treatments"][0]["content"]["sources"][0])
        source["reference"] = deep_thaw(second["cited_reference"])
        science["treatments"][0]["content"]["sources"].append(source)
        self.assertEqual(deep_thaw(validate_executive_orientation(orientation)), orientation)

        for path in (
            "/strategy_ground/0/summary",
            "/scientific_context/treatments/0/content",
            "scientific_context/source_changes/0/qualification",
            "/scientific_context/source_changes/0/qualifi~2cation",
            "/scientific_context/source_changes/01/qualification",
            "/scientific_context/source_changes/-1/qualification",
            "/scientific_context/source_changes/999/qualification",
            "/scientific_context/source_changes/0/qualification_ref",
            "/scientific_context/source_changes/0/qualification/treatments",
            "/scientific_context/source_changes/0/qualification/",
            "/scientific_context/source_changes/1/qualification",
        ):
            malformed = deep_thaw(orientation)
            malformed["scientific_context"]["source_changes"][1]["qualification_ref"]["orientation_path"] = path
            with self.subTest(path=path), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(malformed)
        for representation in ("both", "neither"):
            malformed = deep_thaw(orientation)
            changes = malformed["scientific_context"]["source_changes"]
            if representation == "both":
                changes[1]["qualification"] = deep_thaw(qualification)
            else:
                changes[1]["qualification_ref"] = None
            with self.subTest(representation=representation), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(malformed)
        chained = deep_thaw(orientation)
        chain_science = chained["scientific_context"]
        for change in chain_science["source_changes"]:
            change["current_reference"].update(revision=4, payload_sha256="f" * 64)
            change["read_call"]["input"]["ids"] = ["context:fixture@4"]
        third = deep_thaw(chain_science["source_changes"][1])
        third["cited_reference"].update(revision=3, payload_sha256="d" * 64)
        chain_science["source_changes"].append(third)
        third_source = deep_thaw(chain_science["treatments"][0]["content"]["sources"][1])
        third_source["reference"] = deep_thaw(third["cited_reference"])
        chain_science["treatments"][0]["content"]["sources"].append(third_source)
        self.assertEqual(deep_thaw(validate_executive_orientation(chained)), chained)
        # A remains inline and B points directly to A; C must not reach A via B.
        third["qualification_ref"]["orientation_path"] = "/scientific_context/source_changes/1/qualification"
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(chained)
        for field, replacement in (("revision", 4), ("payload_sha256", "f" * 64)):
            malformed = deep_thaw(orientation)
            malformed["scientific_context"]["source_changes"][1]["current_reference"][field] = replacement
            with self.subTest(field=field), self.assertRaises(MissionOperationContractError):
                validate_executive_orientation(malformed)
        different_selection = deep_thaw(orientation)
        changes = different_selection["scientific_context"]["source_changes"]
        changes[1]["selection"] = {"mode": "treatments", "treatment_ids": ["other-gap"]}
        different_selection["scientific_context"]["treatments"][0]["content"]["sources"][1]["selection"] = deep_thaw(changes[1]["selection"])
        # The second authored selection is valid when it has its own body.
        independent = deep_thaw(different_selection)
        independent_change = independent["scientific_context"]["source_changes"][1]
        independent_change["qualification_ref"] = None
        independent_change["qualification"] = {
            **deep_thaw(qualification), "selection": deep_thaw(independent_change["selection"]),
            "treatments": {"other-gap": deep_thaw(qualification["treatments"]["physical-gap"])},
        }
        self.assertEqual(deep_thaw(validate_executive_orientation(independent)), independent)
        with self.assertRaises(MissionOperationContractError):
            validate_executive_orientation(different_selection)

    def test_complete_claim_admission_plane_is_closed_and_not_a_tenth_operation(self) -> None:
        candidate_ref = {
            "id": "candidate:complete-rh-claim",
            "revision": 4,
            "payload_sha256": "a" * 64,
        }
        case_request = {"candidate_ref": candidate_ref}
        self.assertEqual(
            deep_thaw(validate_admission_case_request(case_request)), case_request
        )
        with self.assertRaises(MissionOperationContractError):
            validate_admission_case_request({**case_request, "effect": "canonical"})

        case_ref = {
            "id": "evidence:evidence:complete-claim-admission-case:case",
            "revision": 1,
            "payload_sha256": "b" * 64,
        }
        grant = {
            "child_thread_id": "child:admission-reviewer",
            "assignment": "Independently review this exact frozen Case.",
            "context": {"id": "context:admission", "revision": 2},
            "case_ref": case_ref,
        }
        self.assertEqual(
            deep_thaw(validate_admission_review_grant_request(grant)), grant
        )
        self.assertEqual(
            deep_thaw(validate_admission_decision_grant_request(grant)), grant
        )

        owner_ref = {
            "kind": "branch",
            "identity": "branch:exact-basis",
            "revision": 3,
            "payload_sha256": "c" * 64,
        }
        review = {
            "mode": "submit",
            "disposition": "no_material_objection",
            "review_finding": "The exact reconstruction has no material objection.",
            "cited_basis": [owner_ref],
        }
        self.assertEqual(
            deep_thaw(validate_admission_review_request(review)), review
        )
        decision = {
            "mode": "submit",
            "disposition": "authorize_exact_delta",
            "decision_basis": "The frozen Case satisfies exact Admission.",
        }
        self.assertEqual(
            deep_thaw(validate_admission_decision_request(decision)), decision
        )
        rejected = {
            "mode": "submit",
            "disposition": "reject",
            "decision_basis": "One exact material objection defeats the claim.",
            "objections": [
                {
                    "exact_objection": "The final implication omits one case.",
                    "affected_scope": "The exact frozen final implication.",
                    "materiality_basis": "The complete claim does not follow.",
                }
            ],
            "cited_basis": [owner_ref],
        }
        self.assertEqual(
            deep_thaw(validate_admission_decision_request(rejected)), rejected
        )
        child_branches = deep_thaw(admission_child_request_schema())["oneOf"]
        self.assertEqual(len(child_branches), 5)
        self.assertEqual(
            child_branches[3]["properties"]["disposition"],
            {"const": "authorize_exact_delta"},
        )
        self.assertEqual(
            child_branches[4]["properties"]["disposition"],
            {"const": "reject"},
        )
        for missing in ("objections", "cited_basis"):
            with self.subTest(missing=missing), self.assertRaises(
                MissionOperationContractError
            ):
                validate_admission_decision_request(
                    {key: value for key, value in rejected.items() if key != missing}
                )
        with self.assertRaises(MissionOperationContractError):
            validate_admission_decision_request(
                {**decision, "objections": rejected["objections"]}
            )
        with self.assertRaises(MissionOperationContractError):
            validate_admission_decision_request(
                {**decision, "disposition": "blocked"}
            )
        with self.assertRaises(MissionOperationContractError):
            validate_admission_review_request(
                {**review, "cited_basis": [{**owner_ref, "identity": ""}]}
            )
        self.assertEqual(len(MISSION_OPERATION_ORDER), 9)

    def test_candidate_a1_review_plane_is_closed_and_not_a_tenth_operation(self) -> None:
        grant_request = {
            "child_thread_id": "child:a1-reviewer",
            "assignment": "Independently reconstruct this frozen A1.",
            "context": {"id": "context:a1-review", "revision": 2},
            "candidate_ref": {
                "id": "candidate:complete-rh-claim",
                "revision": 4,
                "payload_sha256": "a" * 64,
            },
        }
        self.assertEqual(
            deep_thaw(validate_candidate_a1_review_grant_request(grant_request)),
            grant_request,
        )
        with self.assertRaises(MissionOperationContractError):
            validate_candidate_a1_review_grant_request(
                {**grant_request, "source_families": ["all"]}
            )

        evidence_ref = {
            "id": "evidence:contour-defect",
            "revision": 1,
            "payload_sha256": "b" * 64,
        }
        invalidated = {
            "mode": "submit",
            "disposition": "invalidated",
            "review_finding": "One exact contour step is false.",
            "cited_basis": [evidence_ref],
            "concrete_defects": [
                {
                    "exact_defect": "The contour crosses an uncancelled pole.",
                    "affected_scope": "The equality used for the RH conclusion.",
                    "sufficiency_basis": "The omitted residue defeats that equality.",
                }
            ],
            "no_remaining_material_objection": False,
        }
        self.assertEqual(
            deep_thaw(validate_candidate_a1_review_request(invalidated)),
            invalidated,
        )
        self.assertEqual(
            deep_thaw(validate_candidate_a1_review_request({"mode": "usage"})),
            {"mode": "usage"},
        )
        self.assertEqual(
            deep_thaw(
                validate_candidate_a1_review_request(
                    {"mode": "retrieve", "page_size": 1}
                )
            ),
            {"mode": "retrieve", "page_size": 1},
        )
        with self.assertRaises(MissionOperationContractError):
            validate_candidate_a1_review_request(
                {**invalidated, "root_thread_id": "forbidden-caller-fact"}
            )
        with self.assertRaises(MissionOperationContractError):
            validate_candidate_a1_review_request(
                {**invalidated, "disposition": "withdrawn"}
            )
        self.assertEqual(len(MISSION_OPERATION_ORDER), 9)

    def test_host_projection_derives_exact_read_alias_and_closed_grant_input(self) -> None:
        projection = deep_thaw(project_mission_host_model_projection())
        self.assertEqual(
            set(projection),
            {
                "admission_tool",
                "closeout_root_tool",
                "schema_version",
                "root_tool",
                "historical_read_tool",
                "candidate_a1_review_tool",
            },
        )
        self.assertEqual(projection["root_tool"]["name"], "rh_mission")
        self.assertEqual(
            projection["root_tool"]["model_projection"]["allowed_operations"],
            list(MISSION_OPERATION_ORDER),
        )
        closeout = projection["closeout_root_tool"]
        self.assertEqual(closeout["name"], "rh_mission")
        self.assertEqual(
            closeout["model_projection"]["allowed_operations"],
            ["record_strategy", "checkpoint"],
        )
        ordinary_strategy_guide = projection["root_tool"]["model_projection"][
            "usage"
        ]["operation_guides"]["record_strategy"]
        closeout_strategy_guide = closeout["model_projection"]["usage"][
            "operation_guides"
        ]["record_strategy"]
        self.assertEqual(
            ordinary_strategy_guide["input_schema"]["properties"][
                "mission_continuation"
            ]["enum"],
            ["continue"],
        )
        self.assertEqual(
            closeout_strategy_guide["input_schema"]["properties"][
                "mission_continuation"
            ]["enum"],
            ["closeout"],
        )
        self.assertEqual(
            ordinary_strategy_guide["examples"][0]["call"]["input"][
                "mission_continuation"
            ],
            "continue",
        )
        self.assertEqual(
            closeout_strategy_guide["examples"][0]["call"]["input"][
                "mission_continuation"
            ],
            "closeout",
        )
        self.assertIn(
            "exact current admitted result",
            closeout_strategy_guide["examples"][0]["call"]["input"][
                "integrated_comparison"
            ],
        )
        historical = projection["historical_read_tool"]
        self.assertEqual(historical["name"], "rh_mission_history")
        self.assertEqual(
            historical["model_projection"]["allowed_operations"],
            list(HISTORICAL_READ_OPERATIONS),
        )
        self.assertEqual(
            historical["model_projection"]["usage"]["index"]["tool"],
            "rh_mission_history",
        )
        a1_review = projection["candidate_a1_review_tool"]
        self.assertEqual(
            set(a1_review),
            {
                "name",
                "grant_tool_name",
                "page_tool_name",
                "grant_input_schema",
                "request_schema",
            },
        )
        self.assertEqual(a1_review["name"], "rh_mission_a1_review")
        self.assertEqual(
            a1_review["grant_tool_name"], "rh_mission_a1_review_grant"
        )
        self.assertEqual(
            a1_review["page_tool_name"], "rh_mission_a1_review_page"
        )
        admission = projection["admission_tool"]
        self.assertEqual(
            set(admission),
            {
                "name",
                "open_tool_name",
                "grant_tool_name",
                "page_tool_name",
                "case_input_schema",
                "grant_input_schema",
                "request_schema",
            },
        )
        self.assertEqual(admission["name"], "rh_mission_admission")
        self.assertEqual(
            admission["open_tool_name"], "rh_mission_admission_open"
        )
        self.assertEqual(
            admission["grant_tool_name"], "rh_mission_admission_grant"
        )
        self.assertEqual(
            admission["page_tool_name"], "rh_mission_admission_page"
        )
        self.assertEqual(
            admission["grant_input_schema"]["properties"]["role"]["enum"],
            ["reviewer", "admitter"],
        )
        self.assertEqual(len(admission["request_schema"]["oneOf"]), 5)
        root_retrieve = projection["root_tool"]["model_projection"]["usage"][
            "operation_guides"
        ][RETRIEVE]["input_schema"]
        historical_retrieve = historical["model_projection"]["usage"][
            "operation_guides"
        ][RETRIEVE]["input_schema"]
        def retrieve_modes(schema: dict[str, object]) -> set[str]:
            return {
                branch["properties"]["mode"]["const"]  # type: ignore[index]
                for branch in schema["oneOf"]  # type: ignore[union-attr]
            }
        self.assertEqual(retrieve_modes(root_retrieve), {"read", "search", "inventory", "checkpoint", "changes_since_checkpoint", "hooks", "captures", "proof_attention", "publication_result"})
        self.assertEqual(
            retrieve_modes(historical_retrieve),
            {
                "search",
                "read",
                "history_inventory",
                "history_search",
                "history_read",
                "history_traverse",
            },
        )
        root_search = _request(
            RETRIEVE,
            {
                "mode": "search",
                "purpose": "Search the current panorama.",
                "query": "current branch",
            },
        )
        root_read = _request(
            RETRIEVE,
            {
                "mode": "read",
                "purpose": "Read one current owner.",
                "ids": ["branch:current-branch@1"],
            },
        )
        historical_search = _request(
            RETRIEVE,
            {
                "mode": "history_search",
                "purpose": "Search the granted fixed-cut history.",
                "query": "retained bridge",
                "fields": ["content"],
            },
        )
        self.assertEqual(deep_thaw(validate_semantic_request(root_search)), root_search)
        self.assertEqual(deep_thaw(validate_semantic_request(root_read)), root_read)
        with self.assertRaises(MissionOperationContractError):
            validate_semantic_request(historical_search)
        self.assertEqual(
            deep_thaw(validate_historical_read_request(historical_search)),
            historical_search,
        )
        grant = {
            "child_thread_id": "child:historian",
            "assignment_mode": "lifecycle_historian",
            "assignment": "Determine whether retained work changes this decision.",
            "context": {"id": "context:history-review", "revision": 2},
            "source_families": ["branches", "strategies"],
            "raw_body_policy": "metadata_only",
        }
        self.assertEqual(
            deep_thaw(validate_historical_read_grant_request(grant)), grant
        )
        with self.assertRaises(MissionOperationContractError):
            validate_historical_read_grant_request({**grant, "grant_id": "forbidden"})

    def test_one_owner_has_exactly_nine_distinct_capabilities(self) -> None:
        self.assertEqual(
            MISSION_OPERATION_ORDER,
            (
                ORIENT,
                RETRIEVE,
                RECORD_CONTEXT,
                INTERPRET_MATERIAL,
                RECORD_CANDIDATE,
                RECORD_BRANCH,
                SYNTHESIZE,
                RECORD_STRATEGY,
                CHECKPOINT,
            ),
        )
        contract = deep_thaw(mission_operation_contract())
        self.assertEqual(
            contract["schema_version"], MISSION_OPERATION_CONTRACT_SCHEMA_VERSION
        )
        self.assertEqual(
            [item["operation"] for item in contract["operations"]],
            list(MISSION_OPERATION_ORDER),
        )

    def test_model_cli_and_capability_projections_share_exact_branches(self) -> None:
        model = deep_thaw(project_model_operations())
        cli = deep_thaw(project_cli_operations())
        capabilities = deep_thaw(project_operation_capabilities())
        self.assertEqual(model["allowed_operations"], list(MISSION_OPERATION_ORDER))
        self.assertEqual(
            [item["operation"] for item in cli["verbs"]], list(MISSION_OPERATION_ORDER)
        )
        front_door = model["input_schema"]
        self.assertNotIn("oneOf", front_door)
        self.assertEqual(
            set(front_door["properties"]), {"operation", "input"}
        )
        self.assertNotIn("schema_version", front_door["properties"])
        self.assertEqual(
            front_door["properties"]["operation"]["enum"],
            [MISSION_MODEL_USAGE_OPERATION, *MISSION_OPERATION_ORDER],
        )
        self.assertEqual(
            model["semantic_request_schema_version"],
            MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
        )
        usage = model["usage"]
        self.assertEqual(usage["index"], deep_thaw(project_model_usage()))
        self.assertEqual(
            list(usage["operation_guides"]), list(MISSION_OPERATION_ORDER)
        )
        for operation in MISSION_OPERATION_ORDER:
            guide = usage["operation_guides"][operation]
            owner_schema = deep_thaw(semantic_request_schema(operation))
            expected_input_schema = owner_schema["properties"]["input"]
            if operation == RECORD_STRATEGY:
                expected_input_schema["properties"]["mission_continuation"][
                    "enum"
                ] = ["continue"]
            self.assertEqual(
                guide["input_schema"], expected_input_schema
            )
            self.assertEqual(guide["purpose"], mission_operation(operation)["purpose"])
            self.assertGreaterEqual(len(guide["examples"]), 1)
            for example in guide["examples"]:
                model_call = example["call"]
                accepted = validate_semantic_request(
                    {
                        "schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
                        **model_call,
                    }
                )
                self.assertEqual(
                    deep_thaw(accepted)["operation"], operation
                )
            self.assertEqual(
                capabilities["operations"][operation]["effect_class"],
                mission_operation(operation)["effect"]["class"],
            )
        self.assertIn("zero-effect usage", front_door["description"])
        self.assertIn("Every non-usage operation", front_door["description"])
        self.assertIn(
            "exact returned child-thread id", mission_operation(RETRIEVE)["purpose"]
        )
        self.assertIn(
            "question-shaped Context may ground several workers",
            mission_operation(RECORD_CONTEXT)["purpose"],
        )
        self.assertIn(
            "One Evidence meaning may cite several capture scopes",
            mission_operation(INTERPRET_MATERIAL)["purpose"],
        )
        self.assertIn(
            "owner material changes the decision",
            mission_operation(RECORD_STRATEGY)["purpose"],
        )

    def test_generated_owner_selectors_describe_bounded_discovery_not_all_owner_orient(self) -> None:
        projection = deep_thaw(project_mission_host_model_projection())["root_tool"]["model_projection"]
        guides = projection["usage"]["operation_guides"]
        properties = {name: guide["input_schema"].get("properties", {}) for name, guide in guides.items()}
        properties[RECORD_CONTEXT] = next(
            variant["properties"] for variant in guides[RECORD_CONTEXT]["input_schema"]["oneOf"]
            if "material" in variant["properties"]
        )
        selectors = {
            "current_owner": properties[RECORD_CONTEXT]["material"]["items"]["properties"]["id"],
            "owner_selector": properties[RECORD_BRANCH]["owner_refs"]["items"]["properties"]["id"],
            "interpreted_owner": properties[RECORD_CANDIDATE]["supporting_refs"]["items"]["properties"]["id"],
            "synthesis_input": properties[SYNTHESIZE]["inputs"]["items"]["properties"]["id"],
            "existing_context": properties[RECORD_STRATEGY]["context_treatment"]["items"]["properties"]["context"]["properties"]["id"],
            "existing_candidate": properties[RECORD_CANDIDATE]["predecessor"]["properties"]["id"],
            "existing_branch": properties[RECORD_STRATEGY]["attention_actions"]["items"]["properties"]["branch"]["properties"]["id"],
        }
        for name, selector in selectors.items():
            with self.subTest(selector=name):
                description = selector["description"]
                self.assertIn("current Executive orientation", description)
                self.assertIn("successful owner-write result", description)
                self.assertIn("bounded retrieve", description)
                self.assertNotIn("from orient", description)

        capture = guides[INTERPRET_MATERIAL]["input_schema"]["oneOf"][0]["properties"]["capture_scopes"]["items"]["oneOf"][0]["properties"]["captured_material_id"]
        self.assertIn("retrieve search or captures", capture["description"])
        self.assertNotIn("orient", capture["description"])
        retrieval = next(
            variant for variant in guides[RETRIEVE]["input_schema"]["oneOf"]
            if variant["properties"]["mode"]["const"] == "read"
        )["properties"]["ids"]["items"]
        self.assertIn("selected", retrieval["description"])
        self.assertIn("bounded retrieve", retrieval["description"])
        self.assertIn("@revision", retrieval["description"])
        self.assertEqual(projection["allowed_operations"], list(MISSION_OPERATION_ORDER))
        self.assertEqual(
            [variant["properties"]["mode"]["const"] for variant in guides[RETRIEVE]["input_schema"]["oneOf"]],
            ["publication_result", "read", "search", "inventory", "checkpoint", "changes_since_checkpoint", "hooks", "captures", "proof_attention"],
        )

        def descriptions(value):
            if isinstance(value, dict):
                if isinstance(value.get("description"), str):
                    yield value["description"]
                for child in value.values():
                    yield from descriptions(child)
            elif isinstance(value, list):
                for child in value:
                    yield from descriptions(child)

        for description in descriptions(projection):
            self.assertNotIn("copied from orient", description)
            self.assertNotIn("id from orient", description)
            self.assertNotIn("from orient or retrieve search", description)

    def test_record_context_teaches_and_enforces_current_owner_id_grammar(self) -> None:
        guide = deep_thaw(project_model_usage(for_operation=RECORD_CONTEXT))
        properties = next(variant["properties"] for variant in guide["input_schema"]["oneOf"]
                          if "material" in variant["properties"])
        self.assertEqual(
            properties["context_id"]["examples"],
            ["context:shared-research-question"],
        )
        self.assertIn("context:<identity>", properties["context_id"]["description"])
        for selector in (
            properties["material"]["items"]["properties"]["id"],
            properties["dependencies"]["items"]["properties"]["id"],
        ):
            self.assertIn("current Executive orientation", selector["description"])
            self.assertIn("successful owner-write result", selector["description"])
            self.assertIn("bounded retrieve", selector["description"])
            self.assertIn("id@revision", selector["description"])
            self.assertIn("evidence:current-obstruction", selector["examples"])

        valid_input = {
            "context_id": "context:shared-research-question",
            "subject": "One shared reconstruction question.",
            "question": "Which exact obstruction survives?",
            "material": [
                {
                    "id": "evidence:current-obstruction",
                    "why": "It is the current direct-owner ground.",
                }
            ],
            "dependencies": [
                {
                    "id": "strategy:mission-wide",
                    "change_that_matters": "A new obstruction replaces the current one.",
                    "dependent_judgment": "Reconsider the shared question.",
                }
            ],
        }
        valid = _request(RECORD_CONTEXT, valid_input)
        self.assertEqual(deep_thaw(validate_semantic_request(valid)), valid)

        invalid_cases = (
            (
                "bare context id",
                {**valid_input, "context_id": "shared-research-question"},
                "$.input.context_id",
            ),
            (
                "trailing newline context id",
                {
                    **valid_input,
                    "context_id": "context:shared-research-question\n",
                },
                "$.input.context_id",
            ),
            (
                "bare material id",
                {
                    **valid_input,
                    "material": [{"id": "current-obstruction", "why": "Invalid."}],
                },
                "$.input.material[0].id",
            ),
            (
                "revision material handle",
                {
                    **valid_input,
                    "material": [
                        {"id": "evidence:current-obstruction@1", "why": "Invalid."}
                    ],
                },
                "$.input.material[0].id",
            ),
            (
                "bare dependency id",
                {
                    **valid_input,
                    "dependencies": [
                        {
                            "id": "mission-wide",
                            "change_that_matters": "Invalid.",
                            "dependent_judgment": "Invalid.",
                        }
                    ],
                },
                "$.input.dependencies[0].id",
            ),
            (
                "revision dependency handle",
                {
                    **valid_input,
                    "dependencies": [
                        {
                            "id": "strategy:mission-wide@1",
                            "change_that_matters": "Invalid.",
                            "dependent_judgment": "Invalid.",
                        }
                    ],
                },
                "$.input.dependencies[0].id",
            ),
            (
                "non-owner material kind",
                {
                    **valid_input,
                    "material": [
                        {"id": "capture:raw-capture:example", "why": "Invalid."}
                    ],
                },
                "$.input.material[0].id",
            ),
        )
        for label, invalid_input, expected_location in invalid_cases:
            with self.subTest(label=label):
                with self.assertRaises(MissionOperationContractError) as caught:
                    validate_semantic_request(_request(RECORD_CONTEXT, invalid_input))
                self.assertEqual(
                    caught.exception.code,
                    "mission_operation_request_invalid",
                )
                self.assertEqual(caught.exception.location, expected_location)

    def test_retrieve_read_rejects_trailing_newlines_in_exact_handles(self) -> None:
        exact_handles = (
            "evidence:current-obstruction@1",
            "capture:raw-capture:" + ("a" * 48),
            "capture-artifact:raw-capture:" + ("a" * 48) + "#0",
        )
        for handle in exact_handles:
            with self.subTest(handle=handle):
                request = _request(
                    RETRIEVE,
                    {
                        "mode": "read",
                        "purpose": "Read one exact retained item.",
                        "ids": [handle + "\n"],
                    },
                )
                with self.assertRaises(MissionOperationContractError) as caught:
                    validate_semantic_request(request)
                self.assertEqual(
                    caught.exception.code,
                    "mission_operation_request_invalid",
                )
                self.assertEqual(caught.exception.location, "$.input.ids[0]")

    def test_semantic_error_location_is_optional_but_exact_when_present(self) -> None:
        rejected = {
            "schema_version": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": RECORD_CONTEXT,
            "status": "rejected",
            "result": None,
            "error": {
                "code": "mission_operation_request_invalid",
                "message": "$.input.context_id: must match the published grammar",
                "location": "$.input.context_id",
                "property": "request_shape",
                "failure_scope": "call",
                "correction": "revise_request",
            },
        }
        self.assertEqual(
            deep_thaw(validate_semantic_result(RECORD_CONTEXT, rejected)),
            rejected,
        )
        legacy = json.loads(json.dumps(rejected))
        del legacy["error"]["location"]
        self.assertEqual(
            deep_thaw(validate_semantic_result(RECORD_CONTEXT, legacy)),
            legacy,
        )
        empty = json.loads(json.dumps(rejected))
        empty["error"]["location"] = ""
        with self.assertRaises(MissionOperationContractError):
            validate_semantic_result(RECORD_CONTEXT, empty)

    def test_every_model_facing_owner_and_capture_id_family_publishes_grammar(self) -> None:
        def operation_input(operation: str) -> dict[str, object]:
            return deep_thaw(project_model_usage(for_operation=operation))["input_schema"]

        retrieve = operation_input(RETRIEVE)
        context = next(variant for variant in operation_input(RECORD_CONTEXT)["oneOf"]
                       if "material" in variant["properties"])
        interpretation = operation_input(INTERPRET_MATERIAL)
        candidate = operation_input(RECORD_CANDIDATE)
        branch = operation_input(RECORD_BRANCH)
        synthesis = operation_input(SYNTHESIZE)
        strategy = operation_input(RECORD_STRATEGY)
        published_id_schemas = {
            "retrieve.ids": next(branch for branch in retrieve["oneOf"] if branch["properties"]["mode"]["const"] == "read")["properties"]["ids"]["items"],
            "context.context_id": context["properties"]["context_id"],
            "context.material.id": context["properties"]["material"]["items"]["properties"]["id"],
            "context.dependencies.id": context["properties"]["dependencies"]["items"]["properties"]["id"],
            "interpret.evidence_id": interpretation["oneOf"][0]["properties"]["evidence_id"],
            "interpret.capture_id": interpretation["oneOf"][0]["properties"]["capture_scopes"]["items"]["oneOf"][0]["properties"]["captured_material_id"],
            "interpret.annotation_id": interpretation["oneOf"][1]["properties"]["annotation_id"],
            "candidate.candidate_id": candidate["properties"]["candidate_id"],
            "candidate.predecessor.id": candidate["properties"]["predecessor"]["properties"]["id"],
            "candidate.supporting_refs.id": candidate["properties"]["supporting_refs"]["items"]["properties"]["id"],
            "branch.branch_id": branch["properties"]["branch_id"],
            "branch.genealogy.id": branch["properties"]["genealogy"]["items"]["properties"]["branch"]["properties"]["id"],
            "branch.owner_refs.id": branch["properties"]["owner_refs"]["items"]["properties"]["id"],
            "synthesis.inputs.id": synthesis["properties"]["inputs"]["items"]["properties"]["id"],
            "synthesis.dependencies.id": synthesis["properties"]["dependencies"]["items"]["properties"]["id"],
            "strategy.causal_input.id": strategy["properties"]["causal_inputs"]["items"]["properties"]["source"]["properties"]["id"],
            "strategy.attention_branch.id": strategy["properties"]["attention_actions"]["items"]["properties"]["branch"]["properties"]["id"],
            "strategy.context.id": strategy["properties"]["context_treatment"]["items"]["properties"]["context"]["properties"]["id"],
            "strategy.owner_refs.id": strategy["properties"]["owner_refs"]["items"]["properties"]["id"],
        }
        for location, schema in published_id_schemas.items():
            with self.subTest(location=location):
                self.assertEqual(schema["type"], "string")
                self.assertTrue(schema["pattern"])
                self.assertTrue(schema["description"])
                self.assertTrue(schema["examples"])

        local_consequence_ids = {
            "synthesis.evidence_id": synthesis["properties"]["consequences"]["items"]["oneOf"][0]["properties"]["evidence_id"],
            "synthesis.candidate_id": synthesis["properties"]["consequences"]["items"]["oneOf"][1]["properties"]["candidate"]["properties"]["candidate_id"],
        }
        for location, schema in local_consequence_ids.items():
            with self.subTest(location=location):
                self.assertEqual(schema["type"], "string")
                self.assertNotIn("pattern", schema)
                self.assertTrue(schema["description"])
                self.assertTrue(schema["examples"])

        search_query = next(branch for branch in retrieve["oneOf"] if branch["properties"]["mode"]["const"] == "search")["properties"]["query"]
        self.assertIn("Free nonempty substring", search_query["description"])
        self.assertIn(
            "01a00000-0000-7000-8000-000000000001",
            search_query["examples"],
        )
        self.assertEqual(
            set(strategy["properties"]["owner_refs"]["items"]["required"]),
            {"id"},
        )
        self.assertEqual(
            set(strategy["properties"]["owner_refs"]["items"]["properties"]),
            {"id", "revision"},
        )
        self.assertEqual(
            set(synthesis["properties"]["inputs"]["items"]["required"]),
            {"id", "role"},
        )

        interpretation_examples = deep_thaw(
            project_model_usage(for_operation=INTERPRET_MATERIAL)
        )["examples"]
        existing_capture = interpretation_examples[0]["call"]["input"]["capture_scopes"][0]
        annotation = interpretation_examples[2]["call"]["input"]
        self.assertEqual(
            existing_capture["captured_material_id"],
            "capture:raw-capture:" + ("a" * 48),
        )
        self.assertEqual(
            annotation["annotation_id"],
            "capture-annotation:no-current-delta",
        )
        self.assertEqual(
            annotation["captured_material_id"],
            "capture:raw-capture:" + ("a" * 48),
        )

    def test_record_strategy_projection_carries_executive_semantics_only(self) -> None:
        branch = deep_thaw(semantic_request_schema(RECORD_STRATEGY))
        strategy = branch["properties"]["input"]
        self.assertEqual(
            set(strategy["properties"]),
            {
                "mission_continuation",
                "integrated_comparison",
                "causal_inputs",
                "serious_opportunities",
                "selected_bets",
                "attention_actions",
                "context_treatment",
                "creativity_treatment",
                "reconsideration_conditions",
                "reversal_conditions",
                "revival_conditions",
                "owner_refs",
            },
        )
        self.assertEqual(
            set(strategy["required"]),
            {
                "mission_continuation",
                "integrated_comparison",
                "reconsideration_conditions",
            },
        )
        self.assertEqual(
            strategy["properties"]["mission_continuation"]["enum"],
            ["continue", "closeout"],
        )
        selected_bet = strategy["properties"]["selected_bets"]["items"]
        self.assertNotIn("formal_request", selected_bet["required"])
        formal_request = selected_bet["properties"]["formal_request"]
        self.assertEqual(
            set(formal_request["properties"]), {"purpose", "context"}
        )
        self.assertEqual(
            set(formal_request["required"]), {"purpose", "context"}
        )
        serialized = json.dumps(strategy, sort_keys=True)
        for owner_fact in (
            "strategy_id",
            "branch_revision",
            "output_contract_sha256",
            "resolution_requirement_ref",
            "issued_at",
        ):
            self.assertNotIn(owner_fact, serialized)

        semantic_input = {
            "mission_continuation": "continue",
            "integrated_comparison": (
                "The obstruction branch has a sharper discriminator than the "
                "available contour-transfer branch."
            ),
            "reconsideration_conditions": [
                {
                    "condition": (
                        "Reconsider when the obstruction survives the normalized test."
                    )
                }
            ],
            "selected_bets": [
                {
                    "bet": "falsify the normalized local-kernel route",
                    "discriminator": "find one exact sign-changing input",
                    "formal_request": {
                        "purpose": "targeted_falsification",
                        "context": {"id": "context:normalized-local-kernel"},
                    },
                }
            ],
        }
        request = _request(RECORD_STRATEGY, semantic_input)
        self.assertEqual(deep_thaw(validate_semantic_request(request)), request)
        without_formal_request = json.loads(json.dumps(semantic_input))
        del without_formal_request["selected_bets"][0]["formal_request"]
        self.assertEqual(
            deep_thaw(
                validate_semantic_request(
                    _request(RECORD_STRATEGY, without_formal_request)
                )
            ),
            _request(RECORD_STRATEGY, without_formal_request),
        )
        for invalid_request in (
            {
                "purpose": "open_ended_research",
                "context": {"id": "context:normalized-local-kernel"},
            },
            {
                "purpose": "targeted_verification",
                "context": {"id": "context:normalized-local-kernel"},
                "timeout": 60,
            },
        ):
            invalid = json.loads(json.dumps(semantic_input))
            invalid["selected_bets"][0]["formal_request"] = invalid_request
            with self.subTest(formal_request=invalid_request):
                with self.assertRaises(MissionOperationContractError) as rejected:
                    validate_semantic_request(_request(RECORD_STRATEGY, invalid))
                self.assertEqual(
                    rejected.exception.code, "mission_operation_request_invalid"
                )
        for invalid_continuation in ("pause", "later"):
            semantic_input["mission_continuation"] = invalid_continuation
            with self.subTest(mission_continuation=invalid_continuation):
                with self.assertRaises(MissionOperationContractError) as continuation:
                    validate_semantic_request(_request(RECORD_STRATEGY, semantic_input))
                self.assertEqual(
                    continuation.exception.code, "mission_operation_request_invalid"
                )
        semantic_input["mission_continuation"] = "continue"
        del semantic_input["integrated_comparison"]
        with self.assertRaises(MissionOperationContractError) as missing:
            validate_semantic_request(_request(RECORD_STRATEGY, semantic_input))
        self.assertEqual(missing.exception.code, "mission_operation_request_invalid")

    def test_productive_owner_inputs_are_sparse_and_have_no_research_budgets(self) -> None:
        candidate = {
            "candidate_id": "candidate:local-obstruction",
            "proposal_kind": "lemma",
            "exact_statement": "The normalized obstruction is positive at n=3.",
            "standing": {
                "status": "open",
                "basis": "The exact finite calculation has not been generalized.",
            },
        }
        branch = {
            "branch_id": "branch:normalized-obstruction",
            "question": "Does the sign obstruction persist after normalization?",
            "leverage_fingerprint": "A surviving sign defect rules out this route.",
            "target_hook": "candidate:local-obstruction",
        }
        self.assertEqual(
            deep_thaw(
                validate_semantic_request(_request(RECORD_CANDIDATE, candidate))
            ),
            _request(RECORD_CANDIDATE, candidate),
        )
        self.assertEqual(
            deep_thaw(validate_semantic_request(_request(RECORD_BRANCH, branch))),
            _request(RECORD_BRANCH, branch),
        )

        serialized = json.dumps(
            {
                operation: deep_thaw(semantic_request_schema(operation))
                for operation in MISSION_OPERATION_ORDER
            },
            sort_keys=True,
        )
        for arbitrary_limit in (
            "token_budget",
            "time_budget",
            "max_workers",
            "worker_quota",
            "tool_cap",
            "objective_cap",
            "payload_cap",
            "timeout",
            "deadline",
            '"score"',
            '"rank"',
        ):
            self.assertNotIn(arbitrary_limit, serialized)

    def test_candidate_complete_target_claim_is_optional_and_closed(self) -> None:
        ordinary = {
            "candidate_id": "candidate:local-obstruction",
            "proposal_kind": "lemma",
            "exact_statement": "The normalized obstruction is positive at n=3.",
            "standing": {
                "status": "open",
                "basis": "The exact finite calculation has not been generalized.",
            },
        }
        proof = {
            **ordinary,
            "candidate_id": "candidate:complete-target-proof",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
        }
        disproof = {
            **ordinary,
            "candidate_id": "candidate:complete-target-disproof",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "disproof",
            },
        }
        for semantic_input in (ordinary, proof, disproof):
            request = _request(RECORD_CANDIDATE, semantic_input)
            self.assertEqual(
                deep_thaw(validate_semantic_request(request)),
                request,
            )

        for malformed_claim in (
            {"target": "some_other_target", "disposition": "proof"},
            {"target": "riemann_hypothesis", "disposition": "unknown"},
            {
                "target": "riemann_hypothesis",
                "disposition": "proof",
                "proof_material": "forbidden extra field",
            },
        ):
            malformed = {**proof, "complete_target_claim": malformed_claim}
            with self.assertRaises(MissionOperationContractError):
                validate_semantic_request(_request(RECORD_CANDIDATE, malformed))

    def test_candidate_result_returns_exact_open_a1_fact_or_null(self) -> None:
        ordinary = {
            "schema_version": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": RECORD_CANDIDATE,
            "status": "completed",
            "result": {
                "record_id": "candidate:local-obstruction",
                "revision": 1,
                "semantic_summary": "One scoped obstruction remains open.",
                "open_candidate_a1": None,
            },
            "error": None,
        }
        self.assertEqual(
            deep_thaw(validate_semantic_result(RECORD_CANDIDATE, ordinary)),
            ordinary,
        )
        purported_complete = json.loads(json.dumps(ordinary))
        purported_complete["result"] = {
            "record_id": "candidate:complete-rh",
            "revision": 2,
            "semantic_summary": "A purported complete proof requires review.",
            "open_candidate_a1": {
                "candidate_ref": {
                    "kind": "candidate",
                    "identity": "complete-rh",
                    "revision": 2,
                    "payload_sha256": "a" * 64,
                },
                "retrieval_handle": "candidate:complete-rh@2",
                "classification": "purported_complete_rh_proof_or_disproof",
                "disposition": "proof",
                "hold_lifecycle": "open",
            },
        }
        self.assertEqual(
            deep_thaw(
                validate_semantic_result(RECORD_CANDIDATE, purported_complete)
            ),
            purported_complete,
        )
        missing_fact = json.loads(json.dumps(ordinary))
        del missing_fact["result"]["open_candidate_a1"]
        with self.assertRaises(MissionOperationContractError):
            validate_semantic_result(RECORD_CANDIDATE, missing_fact)

    def test_synthesis_can_record_zero_or_partially_accepted_consequences(self) -> None:
        semantic_input = {
            "relation_question": "Do these two obstructions compose?",
            "inputs": [
                {"id": "evidence:first", "role": "premise"},
                {"id": "candidate:second", "role": "comparison"},
            ],
            "compatibility_analysis": "They share the same normalization.",
            "derivation_or_incompatibility": "No further consequence is justified yet.",
            "scope": "The two exact interpreted inputs only.",
            "strength": "heuristic",
            "edge_survival": "The shared normalization edge survives.",
            "consequences": [],
        }
        request = _request(SYNTHESIZE, semantic_input)
        self.assertEqual(deep_thaw(validate_semantic_request(request)), request)

        one_input = json.loads(json.dumps(request))
        one_input["input"]["inputs"] = one_input["input"]["inputs"][:1]
        with self.assertRaises(MissionOperationContractError) as caught:
            validate_semantic_request(one_input)
        self.assertEqual(caught.exception.code, "mission_operation_request_invalid")

        incomplete_evidence = json.loads(json.dumps(request))
        incomplete_evidence["input"]["consequences"] = [
            {"kind": "evidence", "evidence_id": "evidence:derived"}
        ]
        with self.assertRaises(MissionOperationContractError) as discriminated:
            validate_semantic_request(incomplete_evidence)
        self.assertEqual(
            discriminated.exception.location,
            "$.input.consequences[0]",
        )
        self.assertEqual(
            discriminated.exception.message,
            "missing required schema field: statement",
        )

        result = {
            "schema_version": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": SYNTHESIZE,
            "status": "completed",
            "result": {
                "records": [
                    {
                        "record_id": "evidence:derived",
                        "revision": 1,
                        "semantic_summary": "One consequence survived.",
                    }
                ],
                "rejections": [
                    {
                        "consequence_index": 1,
                        "message": "The candidate consequence overclaimed its scope.",
                    }
                ],
                "semantic_summary": "Each stated consequence was handled independently.",
            },
            "error": None,
        }
        self.assertEqual(deep_thaw(validate_semantic_result(SYNTHESIZE, result)), result)

    def test_checkpoint_has_no_model_authored_disposition(self) -> None:
        request = _request(CHECKPOINT, {})
        self.assertEqual(deep_thaw(validate_semantic_request(request)), request)
        for removed_field in ("disposition", "summary", "carried", "next_focus"):
            with self.subTest(removed_field=removed_field):
                with self.assertRaises(MissionOperationContractError) as caught:
                    validate_semantic_request(
                        _request(CHECKPOINT, {removed_field: "model-authored control"})
                    )
                self.assertEqual(
                    caught.exception.code, "mission_operation_request_invalid"
                )

    def test_checkpoint_result_is_only_identity_and_factual_epoch_closure(self) -> None:
        result = {
            "schema_version": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": CHECKPOINT,
            "status": "completed",
            "result": {
                "checkpoint_id": "checkpoint:abc",
                "executive_epoch_id": "epoch:abc",
                "state": "checkpointed",
            },
            "error": None,
        }
        self.assertEqual(
            deep_thaw(validate_semantic_result(CHECKPOINT, result)), result
        )
        copied_meaning = deep_thaw(result)
        copied_meaning["result"]["semantic_summary"] = "receipt prose"
        with self.assertRaises(MissionOperationContractError):
            validate_semantic_result(CHECKPOINT, copied_meaning)

    def test_subset_can_omit_but_cannot_redefine_or_reorder_branches(self) -> None:
        selected = (CHECKPOINT, ORIENT, RECORD_CONTEXT)
        subset_model = deep_thaw(project_model_operations(selected))
        full_model = deep_thaw(project_model_operations())
        self.assertEqual(
            subset_model["allowed_operations"], [ORIENT, RECORD_CONTEXT, CHECKPOINT]
        )
        self.assertEqual(
            subset_model["input_schema"]["properties"]["operation"]["enum"],
            [MISSION_MODEL_USAGE_OPERATION, ORIENT, RECORD_CONTEXT, CHECKPOINT],
        )
        self.assertEqual(
            list(subset_model["usage"]["operation_guides"]),
            [ORIENT, RECORD_CONTEXT, CHECKPOINT],
        )
        for operation in subset_model["allowed_operations"]:
            self.assertEqual(
                subset_model["usage"]["operation_guides"][operation],
                full_model["usage"]["operation_guides"][operation],
            )
        with self.assertRaises(MissionOperationContractError) as caught:
            project_model_usage(selected, for_operation=RETRIEVE)
        self.assertEqual(caught.exception.location, "$.input.for_operation")
        with self.assertRaisesRegex(MissionOperationContractError, "duplicate-free"):
            project_model_operations((ORIENT, ORIENT))
        with self.assertRaisesRegex(MissionOperationContractError, "Unsupported"):
            project_cli_operations(("invented",))

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(self) -> None:
        duplicate = (
            '{"schema_version":"%s","operation":"orient",'
            '"input":{"unexpected":null,"unexpected":"x"}}'
            % MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION
        ).encode()
        with self.assertRaisesRegex(MissionOperationContractError, "duplicate JSON key"):
            parse_semantic_request_bytes(duplicate)
        nonfinite = duplicate.replace(
            b'{"unexpected":null,"unexpected":"x"}', b'{"unexpected":NaN}'
        )
        with self.assertRaisesRegex(MissionOperationContractError, "non-finite"):
            parse_semantic_request_bytes(nonfinite)

    def test_closed_request_and_recursive_owner_fact_rejection_precede_effect(self) -> None:
        valid = _request(ORIENT, {})
        self.assertEqual(deep_thaw(validate_semantic_request(valid)), valid)
        with self.assertRaises(MissionOperationContractError) as unknown:
            validate_semantic_request({**valid, "extra": "not allowed"})
        self.assertEqual(unknown.exception.code, "mission_operation_request_invalid")

        forbidden_values = [
            {"command_id": "caller-command"},
            {"nested": {"goal_thread_id": "thread.1"}},
            {"nested": [{"output_sha256": "0" * 64}]},
            {"content_base64": "QQ=="},
            {"observed_at": "2026-08-13T00:00:00Z"},
        ]
        for forbidden in forbidden_values:
            with self.subTest(forbidden=forbidden):
                request = _request(ORIENT, {})
                request["input"] = forbidden
                with self.assertRaises(MissionOperationContractError) as caught:
                    validate_semantic_request(request)
                self.assertEqual(
                    caught.exception.code, "mission_operation_owner_fact_forbidden"
                )

    def test_effect_error_and_owner_fact_metadata_are_exact(self) -> None:
        for operation in MISSION_OPERATION_ORDER:
            item = mission_operation(operation)
            self.assertEqual(item["effect"]["canonical"], "none")
            self.assertEqual(item["effect"]["public"], "none")
            self.assertEqual(item["effect"]["provider"], "none")
            self.assertTrue(item["owner_fact_injection"])
            self.assertNotIn("semantic_input", item["owner_fact_injection"])
            self.assertEqual(
                {entry["failure_scope"] for entry in item["errors"]}.intersection(
                    {"call", "operation", "goal", "mission"}
                ),
                {entry["failure_scope"] for entry in item["errors"]},
            )

        retrieve_errors = {
            entry["code"]: entry for entry in mission_operation(RETRIEVE)["errors"]
        }
        self.assertEqual(
            retrieve_errors["mission_material_unavailable"],
            {
                "code": "mission_material_unavailable",
                "property": "selected_material_availability",
                "failure_scope": "call",
                "correction": "continue_other_material_and_report_exact_artifact",
            },
        )
        self.assertNotIn(
            "mission_material_unavailable",
            {entry["code"] for entry in mission_operation(ORIENT)["errors"]},
        )

    def test_readable_results_accept_text_and_reject_encoded_mechanics(self) -> None:
        result = {
            "schema_version": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": RETRIEVE,
            "status": "completed",
            "result": {
                "mode": "read",
                "purpose": "Read exact material.",
                "scope": {"basis": "exact_selection", "checkpoint_id": None, "selection": {"mode": "read", "purpose": "Read exact material.", "ids": ["evidence:evidence.1"]}},
                "state": "present", "completeness": "exhausted", "next_cursor": None,
                "items": [
                    {
                        "requested_id": "evidence:evidence.1", "status": "readable",
                        "id": "evidence:evidence.1",
                        "kind": "worker_output",
                        "title": "Exact obstruction",
                        "readable_content": "The proposed inequality fails at n=3.",
                        "completeness": "complete",
                        "trust_class": "semantic_owner_projection",
                    }
                ],
            },
            "error": None,
        }
        self.assertEqual(deep_thaw(validate_semantic_result(RETRIEVE, result)), result)
        unversioned_route = json.loads(json.dumps(result))
        unversioned_route["result"]["scope"]["route"] = "direct_exact_read"
        with self.assertRaises(MissionOperationContractError) as caught:
            validate_semantic_result(RETRIEVE, unversioned_route)
        self.assertEqual(caught.exception.code, "mission_operation_result_invalid")
        encoded = json.loads(json.dumps(result))
        encoded["result"]["items"][0]["content_base64"] = "QQ=="
        with self.assertRaises(MissionOperationContractError) as caught:
            validate_semantic_result(RETRIEVE, encoded)
        self.assertEqual(caught.exception.code, "mission_operation_result_invalid")

        empty_search = json.loads(json.dumps(result))
        empty_search["result"]["mode"] = "search"
        empty_search["result"]["purpose"] = "No authorized material matched."
        empty_search["result"]["state"] = "none"
        empty_search["result"]["scope"] = {"basis": "current_at_first_page", "checkpoint_id": None, "selection": {"mode": "search", "purpose": "No authorized material matched.", "query": "absent"}}
        empty_search["result"]["items"] = []
        self.assertEqual(
            deep_thaw(validate_semantic_result(RETRIEVE, empty_search)),
            empty_search,
        )

    def test_strategy_result_exposes_only_the_exact_formal_request_selector(
        self,
    ) -> None:
        result = {
            "schema_version": MISSION_SEMANTIC_RESULT_SCHEMA_VERSION,
            "operation": RECORD_STRATEGY,
            "status": "completed",
            "result": {
                "record_id": "strategy:mission-wide",
                "revision": 2,
                "semantic_summary": "One exact formal request is selected.",
                "formal_requests": [
                    {
                        "selected_bet_sha256": "a" * 64,
                        "bet": "Verify the exact construction.",
                        "discriminator": "The exact identity holds or fails.",
                        "purpose": "targeted_verification",
                        "context_retrieval_handle": "context:formal.1@1",
                    }
                ],
            },
            "error": None,
        }
        self.assertEqual(
            deep_thaw(validate_semantic_result(RECORD_STRATEGY, result)),
            result,
        )

        invalid_digest = json.loads(json.dumps(result))
        invalid_digest["result"]["formal_requests"][0][
            "selected_bet_sha256"
        ] = "A" * 64
        with self.assertRaises(MissionOperationContractError) as digest_error:
            validate_semantic_result(RECORD_STRATEGY, invalid_digest)
        self.assertEqual(
            digest_error.exception.code, "mission_operation_result_invalid"
        )

        extra_field = json.loads(json.dumps(result))
        extra_field["result"]["formal_requests"][0]["provider"] = "invented"
        with self.assertRaises(MissionOperationContractError) as shape_error:
            validate_semantic_result(RECORD_STRATEGY, extra_field)
        self.assertEqual(shape_error.exception.code, "mission_operation_result_invalid")

        request = _request(
            RECORD_STRATEGY,
            {
                "mission_continuation": "continue",
                "integrated_comparison": "A request cannot inject its digest.",
                "selected_bet_sha256": "a" * 64,
                "reconsideration_conditions": [
                    {"condition": "Reconsider after the exact check."}
                ],
            },
        )
        with self.assertRaises(MissionOperationContractError) as request_error:
            validate_semantic_request(request)
        self.assertEqual(
            request_error.exception.code,
            "mission_operation_owner_fact_forbidden",
        )

        unrelated_result = json.loads(json.dumps(result))
        unrelated_result["operation"] = RECORD_BRANCH
        unrelated_result["result"] = {
            "record_id": "branch:branch.1",
            "revision": 2,
            "semantic_summary": "No digest belongs here.",
            "selected_bet_sha256": "a" * 64,
        }
        with self.assertRaises(MissionOperationContractError) as unrelated_error:
            validate_semantic_result(RECORD_BRANCH, unrelated_result)
        self.assertEqual(
            unrelated_error.exception.code,
            "mission_operation_result_invalid",
        )

    def test_interpretation_owns_one_retryable_evidence_head_over_ordered_capture_scopes(
        self,
    ) -> None:
        schema = deep_thaw(semantic_request_schema(INTERPRET_MATERIAL))
        variants = schema["properties"]["input"]["oneOf"]
        properties = variants[0]["properties"]
        self.assertIn("evidence_id", properties)
        self.assertIn("capture_scopes", properties)
        self.assertNotIn("captured_material_id", properties)
        self.assertNotIn("captured_material_ids", properties)
        scope_variants = properties["capture_scopes"]["items"]["oneOf"]
        self.assertEqual(len(scope_variants), 2)
        self.assertIn(
            "captured_material_id",
            scope_variants[0]["properties"],
        )
        adopted_properties = scope_variants[1]["properties"]
        self.assertIn("adopted_root_material", adopted_properties)
        self.assertEqual(
            adopted_properties["adopted_root_material"]["properties"]["channel"],
            {"type": "string", "minLength": 1},
        )
        native_lineage_schema = adopted_properties["adopted_root_material"][
            "properties"
        ]["native_lineage"]
        self.assertEqual(
            set(native_lineage_schema["required"]),
            {"material_kind", "parent_thread_id", "child_thread_id"},
        )
        self.assertFalse(native_lineage_schema["additionalProperties"])
        annotation_properties = variants[1]["properties"]
        self.assertEqual(
            annotation_properties["judgment"]["const"],
            "reviewed_no_current_semantic_delta",
        )
        self.assertEqual(
            annotation_properties["coverage"]["const"],
            "complete_artifact",
        )
        request = _request(
            INTERPRET_MATERIAL,
            {
                "evidence_id": "evidence:evidence.obstruction",
                "capture_scopes": [
                    {
                        "captured_material_id": "capture:raw-capture:" + ("a" * 48),
                        "artifact_ordinal": 0,
                        "exact_scope": "the complete first artifact",
                        "coverage": "complete_artifact",
                    },
                    {
                        "adopted_root_material": {
                            "channel": "web_search",
                            "content": "The exact source passage used by the root.",
                        },
                        "exact_scope": "the complete adopted source passage",
                        "coverage": "partial_artifact",
                    },
                ],
                "interpretation": "The worker isolated a usable obstruction.",
                "scope": "the stated obstruction in the ordered source scopes",
                "strength": "heuristic",
                "limitations": ["The general case remains open."],
                "significance": "This changes the next experiment.",
            },
        )
        self.assertEqual(deep_thaw(validate_semantic_request(request)), request)

        native_recovery = json.loads(json.dumps(request))
        adopted_native = native_recovery["input"]["capture_scopes"][1][
            "adopted_root_material"
        ]
        adopted_native["channel"] = "native_output"
        adopted_native["native_lineage"] = {
            "material_kind": "output",
            "parent_thread_id": "thread:researcher",
            "child_thread_id": "thread:researcher:worker",
        }
        self.assertEqual(
            deep_thaw(validate_semantic_request(native_recovery)),
            native_recovery,
        )
        malformed_native = json.loads(json.dumps(native_recovery))
        malformed_native["input"]["capture_scopes"][1][
            "adopted_root_material"
        ]["native_lineage"]["observation_id"] = "not accepted from the model"
        with self.assertRaises(MissionOperationContractError) as rejected_native:
            validate_semantic_request(malformed_native)
        self.assertEqual(
            rejected_native.exception.location,
            "$.input.capture_scopes[1].adopted_root_material.native_lineage.observation_id",
        )

        invalid_evidence = json.loads(json.dumps(request))
        invalid_evidence["input"]["evidence_id"] = "not-an-evidence-owner"
        with self.assertRaises(MissionOperationContractError) as rejected_evidence:
            validate_semantic_request(invalid_evidence)
        self.assertEqual(
            rejected_evidence.exception.location,
            "$.input.evidence_id",
        )

        invalid_capture = json.loads(json.dumps(request))
        invalid_capture["input"]["capture_scopes"][0]["captured_material_id"] = (
            "capture:raw-capture:not-a-digest"
        )
        with self.assertRaises(MissionOperationContractError) as rejected_capture:
            validate_semantic_request(invalid_capture)
        self.assertEqual(
            rejected_capture.exception.location,
            "$.input.capture_scopes[0].captured_material_id",
        )
        newline_capture = json.loads(json.dumps(request))
        newline_capture["input"]["capture_scopes"][0]["captured_material_id"] += "\n"
        with self.assertRaises(MissionOperationContractError) as rejected_newline:
            validate_semantic_request(newline_capture)
        self.assertEqual(
            rejected_newline.exception.location,
            "$.input.capture_scopes[0].captured_material_id",
        )
        annotation = _request(
            INTERPRET_MATERIAL,
            {
                "judgment": "reviewed_no_current_semantic_delta",
                "annotation_id": "capture-annotation:neutral-output",
                "captured_material_id": "capture:raw-capture:" + ("a" * 48),
                "artifact_ordinal": 0,
                "exact_scope": "The complete first artifact.",
                "coverage": "complete_artifact",
                "lifecycle": "active",
            },
        )
        self.assertEqual(
            deep_thaw(validate_semantic_request(annotation)), annotation
        )
        plural = _request(
            INTERPRET_MATERIAL,
            {
                "evidence_id": "evidence:evidence.obstruction",
                "capture_scopes": [
                    {
                        "captured_material_id": "capture:raw-capture:" + ("a" * 48),
                        "artifact_ordinal": 0,
                        "exact_scope": "the complete first artifact",
                        "coverage": "complete_artifact",
                    }
                ],
                "captured_material_ids": [
                    "capture:capture.first",
                    "capture:capture.second",
                ],
                "interpretation": "Reject a cross-lineage interpretation request.",
                "scope": "the complete captured artifacts",
                "strength": "heuristic",
                "limitations": [],
                "significance": "No partial semantic write is allowed.",
            },
        )
        with self.assertRaises(MissionOperationContractError) as caught:
            validate_semantic_request(plural)
        self.assertEqual(caught.exception.code, "mission_operation_request_invalid")
        errors = {
            item["code"]: item
            for item in mission_operation(INTERPRET_MATERIAL)["errors"]
        }
        self.assertEqual(
            errors["mission_authorization_expired"]["failure_scope"],
            "operation",
        )

if __name__ == "__main__":
    unittest.main()
