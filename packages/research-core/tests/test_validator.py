from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from research_core.validator import validate_file, validate_state  # noqa: E402


from public_validator_fixture_support import (  # noqa: E402
    EXACT_CLAIM,
    PAUSED_CLAIM,
    PROCESS_RECEIPT,
    RAW_SOURCE,
    STATE_PATH,
    TARGET_CLAIM,
    make_validator_state,
)


class ResearchStateValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = make_validator_state()

    def error_messages(self, state: dict) -> list[str]:
        return [item.message for item in validate_state(state).errors]

    def terminal_state(self, disposition: str) -> dict:
        state = copy.deepcopy(self.state)
        state["project"]["proof_status"] = disposition
        state["project"].pop("active_target")
        state["project"]["admitted_result"] = {
            "target": "riemann_hypothesis",
            "disposition": disposition,
            "theorem_or_counterexample_claim": (
                "Every nontrivial zero of the Riemann zeta function has real part 1/2."
                if disposition == "proved"
                else "A nontrivial zero of the Riemann zeta function has real part other than 1/2."
            ),
            "candidate_ref": {
                "mission_id": "mission.rh.autonomous.test",
                "candidate_id": f"candidate.rh.{disposition}.test",
                "revision": 3,
                "digest_sha256": "a" * 64,
            },
            "admission_decision_ref": {
                "decision_id": f"admission-decision.rh.{disposition}.test",
                "digest_sha256": "b" * 64,
            },
        }
        return state

    def test_public_seed_is_valid_and_has_no_research_history(self) -> None:
        result = validate_file(STATE_PATH)
        self.assertTrue(result.ok, [item.to_dict() for item in result.errors])
        self.assertEqual(result.counts["claims"], 1)
        self.assertEqual(result.counts["counterexamples"], 0)
        self.assertEqual(result.counts["sources"], 0)
        self.assertEqual(result.counts["receipts"], 0)

    def test_synthetic_validator_fixture_is_valid(self) -> None:
        result = validate_state(self.state)
        self.assertTrue(result.ok, [item.to_dict() for item in result.errors])
        self.assertEqual(result.counts["claims"], 4)
        self.assertEqual(result.counts["counterexamples"], 3)
        self.assertEqual(result.counts["sources"], 1)
        self.assertEqual(result.counts["receipts"], 3)

    def test_symmetric_terminal_results_are_valid_without_rewriting_frontier_history(self) -> None:
        for disposition in ("proved", "disproved"):
            with self.subTest(disposition=disposition):
                state = self.terminal_state(disposition)
                result = validate_state(state)
                self.assertTrue(result.ok, [item.to_dict() for item in result.errors])
                self.assertEqual(
                    next(
                        item for item in state["obligations"] if item["id"] == "obligation.B0"
                    )["proof_status"],
                    "unproved",
                )
                self.assertEqual(
                    next(
                        item for item in state["claims"] if item["id"] == TARGET_CLAIM
                    )["audit_verdict"],
                    "unproved_gap",
                )

    def test_incomplete_project_requires_active_target_and_forbids_admitted_result(self) -> None:
        missing_target = copy.deepcopy(self.state)
        missing_target["project"].pop("active_target")
        self.assertTrue(
            any(
                "is required while project.proof_status is incomplete" in item
                for item in self.error_messages(missing_target)
            )
        )

        premature_result = copy.deepcopy(self.state)
        premature_result["project"]["admitted_result"] = self.terminal_state("proved")[
            "project"
        ]["admitted_result"]
        self.assertTrue(
            any(
                "must be absent while project.proof_status is incomplete" in item
                for item in self.error_messages(premature_result)
            )
        )

        malformed_status = copy.deepcopy(self.state)
        malformed_status["project"]["proof_status"] = {}
        self.assertTrue(
            any(
                "must be exactly incomplete, proved, or disproved" in item
                for item in self.error_messages(malformed_status)
            )
        )

    def test_terminal_project_requires_result_and_forbids_active_target(self) -> None:
        missing_result = copy.deepcopy(self.state)
        missing_result["project"]["proof_status"] = "proved"
        self.assertTrue(
            any(
                "must be an object for a proved or disproved project" in item
                for item in self.error_messages(missing_result)
            )
        )

        retained_target = self.terminal_state("disproved")
        retained_target["project"]["active_target"] = TARGET_CLAIM
        self.assertTrue(
            any(
                "must be absent after a canonical proof or disproof is admitted" in item
                for item in self.error_messages(retained_target)
            )
        )

    def test_terminal_result_must_match_status_and_exact_rh_authority_refs(self) -> None:
        mismatch = self.terminal_state("proved")
        mismatch["project"]["admitted_result"]["disposition"] = "disproved"
        self.assertTrue(
            any(
                "must exactly match project.proof_status" in item
                for item in self.error_messages(mismatch)
            )
        )

        malformed = self.terminal_state("disproved")
        result = malformed["project"]["admitted_result"]
        result["target"] = "another_conjecture"
        result["theorem_or_counterexample_claim"] = "   "
        result["candidate_ref"]["revision"] = 0
        result["candidate_ref"]["digest_sha256"] = "not-a-digest"
        result["admission_decision_ref"]["digest_sha256"] = "not-a-digest"
        messages = self.error_messages(malformed)
        self.assertTrue(any("must equal riemann_hypothesis" in item for item in messages))
        self.assertTrue(any("must be a non-empty string" in item for item in messages))
        self.assertTrue(any("greater than or equal to 1" in item for item in messages))
        self.assertGreaterEqual(
            sum("64-character hexadecimal SHA-256" in item for item in messages),
            2,
        )

    def test_terminal_result_rejects_unknown_authority_fields(self) -> None:
        state = self.terminal_state("proved")
        state["project"]["admitted_result"]["confidence_score"] = 1
        state["project"]["admitted_result"]["candidate_ref"]["case_ref"] = "case.extra"
        messages = self.error_messages(state)
        self.assertGreaterEqual(
            sum("unknown key for Research State v2" in item for item in messages),
            2,
        )

    def test_both_hankel_obligations_are_permanent_gates(self) -> None:
        state = copy.deepcopy(self.state)
        state["obligations"] = [
            item for item in state["obligations"] if item["id"] != "obligation.B0"
        ]
        messages = self.error_messages(state)
        self.assertTrue(any("missing required obligation: obligation.B0" in item for item in messages))

    def test_immediate_target_must_imply_both_channels(self) -> None:
        state = copy.deepcopy(self.state)
        target = next(item for item in state["claims"] if item["id"] == TARGET_CLAIM)
        target["implies"].remove("obligation.B0")
        messages = self.error_messages(state)
        self.assertTrue(any("both B0 and B1" in item for item in messages))

    def test_finite_or_numerical_claim_requires_all_order_exclusion(self) -> None:
        state = copy.deepcopy(self.state)
        claim = next(item for item in state["claims"] if item["id"] == EXACT_CLAIM)
        claim["claim_type"] = "finite_certificate"
        messages = self.error_messages(state)
        self.assertTrue(any("all_order_exclusion" in item for item in messages))

    def test_blocking_verdict_cannot_propagate_in_rh_chain(self) -> None:
        state = copy.deepcopy(self.state)
        claim = next(item for item in state["claims"] if item["id"] == PAUSED_CLAIM)
        claim["rh_chain_status"] = "usable"
        messages = self.error_messages(state)
        self.assertTrue(any("blocking verdict" in item for item in messages))

    def test_prohibited_source_side_inputs_are_required(self) -> None:
        state = copy.deepcopy(self.state)
        state["status_model"]["prohibited_inputs"].remove("zeta_zero_expansion")
        messages = self.error_messages(state)
        self.assertTrue(any("must declare exactly" in item for item in messages))

    def test_counterexample_evidence_must_resolve(self) -> None:
        state = copy.deepcopy(self.state)
        state["counterexamples"][0]["evidence_refs"].append("receipt.missing")
        messages = self.error_messages(state)
        self.assertTrue(any("unresolved reference: receipt.missing" in item for item in messages))

    def test_schema_v2_retires_the_ambiguous_failed_route_claim_type(self) -> None:
        state = copy.deepcopy(self.state)
        state["schema_version"] = 1
        state["status_model"]["claim_types"].append(
            "counterexample_failed_route"
        )
        state["status_model"]["claim_types"].remove(
            "scoped_method_counterexample"
        )
        messages = self.error_messages(state)
        self.assertTrue(any("must equal 2" in item for item in messages))
        self.assertTrue(any("must declare exactly" in item for item in messages))

    def test_status_model_owns_exact_process_vocabularies(self) -> None:
        cases = (
            ("audit_verdicts", "verified"),
            ("claim_types", "exact_identity"),
            ("dag_roles", "supporting"),
            ("lifecycle_statuses", "active"),
            ("rh_chain_statuses", "usable"),
            ("counterexample_record_kinds", "exact_counterexample"),
            ("route_effects", "exclude_within_scope"),
            ("route_decisions", "exclude"),
            ("receipt_kinds", "campaign_closeout"),
            ("prohibited_inputs", "riemann_hypothesis"),
        )
        for field, used_value in cases:
            state = copy.deepcopy(self.state)
            state["status_model"][field].remove(used_value)
            state["status_model"][field].append("invented_parallel_label")
            messages = self.error_messages(state)
            self.assertTrue(
                any("must declare exactly" in item for item in messages), field
            )

    def test_unknown_state_and_claim_authority_aliases_are_rejected(self) -> None:
        state = copy.deepcopy(self.state)
        state["parallel_truth"] = []
        state["claims"][0]["canonical_verdict"] = "verified"
        messages = self.error_messages(state)
        self.assertTrue(any("unknown key for Research State v2" in item for item in messages))

    def test_paused_claim_requires_a_revival_trigger(self) -> None:
        state = copy.deepcopy(self.state)
        claim = next(item for item in state["claims"] if item["status"] == "paused")
        claim["revival_trigger"] = ""
        self.assertTrue(
            any("must be a non-empty string" in item for item in self.error_messages(state))
        )

    def test_failed_derivation_cannot_make_a_claim_false(self) -> None:
        state = copy.deepcopy(self.state)
        claim = next(
            item
            for item in state["claims"]
            if item["audit_verdict"] in {"verified", "verified_with_conditions"}
        )
        claim["audit_verdict"] = "false"
        claim["statement"] = "The attempted derivation of this statement failed."
        messages = self.error_messages(state)
        self.assertTrue(
            any("proof-carrying refutation object" in item for item in messages)
        )

    def test_false_claim_refutation_requires_scope_evidence_and_boundary(self) -> None:
        state = copy.deepcopy(self.state)
        claim = next(
            item
            for item in state["claims"]
            if item["audit_verdict"] in {"verified", "verified_with_conditions"}
        )
        claim["audit_verdict"] = "false"
        claim["refutation"] = {
            "kind": "failed_search",
            "exact_statement": "",
            "exact_scope": "",
            "evidence_refs": [],
            "does_not_exclude": [],
        }
        messages = self.error_messages(state)
        self.assertTrue(any("exact_counterexample or proof_of_negation" in item for item in messages))
        self.assertTrue(any("must contain proof" in item for item in messages))
        self.assertTrue(any("non-inference boundary" in item for item in messages))

    def test_false_claim_refutation_rejects_direct_self_evidence(self) -> None:
        state = copy.deepcopy(self.state)
        claim = next(
            item
            for item in state["claims"]
            if item["audit_verdict"] in {"verified", "verified_with_conditions"}
        )
        claim["audit_verdict"] = "false"
        claim["refutation"] = {
            "kind": "proof_of_negation",
            "exact_statement": claim["statement"],
            "exact_scope": "The exact recorded statement.",
            "evidence_refs": [claim["id"]],
            "does_not_exclude": ["Any distinct proposition."],
        }
        self.assertTrue(
            any("cannot cite itself" in item for item in self.error_messages(state))
        )

    def test_counterexample_gate_requires_exact_scoped_proof_carrying_fields(self) -> None:
        state = copy.deepcopy(self.state)
        gate = next(item for item in state["counterexamples"] if item["status"] == "active")
        gate["record_kind"] = "historical_non_gate"
        gate["exact_scope"] = ""
        gate["basis"] = ""
        gate["route_effect"] = "no_inference"
        gate["does_not_exclude"] = []
        gate["evidence_refs"] = []
        gate.pop("revival_trigger", None)
        messages = self.error_messages(state)
        self.assertTrue(any("active gates must be exact counterexamples" in item for item in messages))
        self.assertTrue(any("active gates must exclude only" in item for item in messages))
        self.assertTrue(any("active gates require proof-carrying evidence" in item for item in messages))
        self.assertTrue(any("non-inference boundary" in item for item in messages))
        self.assertTrue(any("is required and must be null" in item for item in messages))

    def test_active_counterexample_rejects_self_or_nonproof_evidence(self) -> None:
        state = copy.deepcopy(self.state)
        gate = next(item for item in state["counterexamples"] if item["status"] == "active")
        gate["evidence_refs"] = [gate["id"], "object.public.moments"]
        messages = self.error_messages(state)
        self.assertTrue(any("cannot cite itself" in item for item in messages))
        self.assertTrue(any("not proof-carrying" in item for item in messages))

    def test_active_counterexample_rejects_process_receipt_bootstrap(self) -> None:
        state = copy.deepcopy(self.state)
        gate = next(item for item in state["counterexamples"] if item["status"] == "active")
        gate["evidence_refs"] = [PROCESS_RECEIPT]
        self.assertTrue(
            any("not proof-carrying" in item for item in self.error_messages(state))
        )

    def test_active_counterexample_rejects_raw_artifact_as_sole_anchor(self) -> None:
        state = copy.deepcopy(self.state)
        gate = next(item for item in state["counterexamples"] if item["status"] == "active")
        gate["evidence_refs"] = [RAW_SOURCE]
        self.assertTrue(
            any(
                "independently audited mathematical evidence anchor" in item
                for item in self.error_messages(state)
            )
        )

    def test_active_counterexamples_cannot_bootstrap_each_other(self) -> None:
        state = copy.deepcopy(self.state)
        gates = [
            item for item in state["counterexamples"] if item["status"] == "active"
        ][:2]
        gates[0]["evidence_refs"] = [gates[1]["id"]]
        gates[1]["evidence_refs"] = [gates[0]["id"]]
        messages = self.error_messages(state)
        self.assertGreaterEqual(
            sum("not proof-carrying" in item for item in messages), 2
        )

    def test_nonactive_counterexample_is_nonrefuting_and_pause_is_revivable(self) -> None:
        state = copy.deepcopy(self.state)
        record = next(
            item for item in state["counterexamples"] if item["status"] != "active"
        )
        record["route_effect"] = "exclude_within_scope"
        paused = next(
            item for item in state["counterexamples"] if item["status"] == "paused"
        )
        paused["revival_trigger"] = ""
        messages = self.error_messages(state)
        self.assertTrue(any("must have no_inference route effect" in item for item in messages))
        self.assertTrue(any("paused records require" in item for item in messages))

    def test_campaign_closeout_pause_requires_revival_and_is_nonrefuting(self) -> None:
        state = copy.deepcopy(self.state)
        receipt = next(
            item
            for item in state["audit_receipts"]
            if item.get("receipt_kind") == "campaign_closeout"
        )
        disposition = receipt["route_dispositions"][0]
        disposition.update(
            {
                "decision": "pause",
                "truth_effect": "scoped_exclusion",
                "revival_trigger": None,
            }
        )
        messages = self.error_messages(state)
        self.assertTrue(any("pause is non-refuting" in item for item in messages))
        self.assertTrue(any("pause requires a non-empty revival trigger" in item for item in messages))

    def test_campaign_closeout_exclusion_requires_scoped_truth_effect(self) -> None:
        state = copy.deepcopy(self.state)
        receipt = next(
            item
            for item in state["audit_receipts"]
            if item.get("receipt_kind") == "campaign_closeout"
        )
        disposition = receipt["route_dispositions"][0]
        disposition["decision"] = "exclude"
        disposition["truth_effect"] = "none"
        self.assertTrue(
            any(
                "exclude requires a proof-carrying scoped_exclusion" in item
                for item in self.error_messages(state)
            )
        )

    def test_campaign_closeout_disposition_requires_canonical_evidence(self) -> None:
        state = copy.deepcopy(self.state)
        receipt = next(
            item
            for item in state["audit_receipts"]
            if item.get("receipt_kind") == "campaign_closeout"
        )
        disposition = receipt["route_dispositions"][0]
        disposition["evidence_refs"] = []
        messages = self.error_messages(state)
        self.assertTrue(any("must cite canonical evidence" in item for item in messages))

    def test_no_result_cannot_supply_exclusion_evidence(self) -> None:
        state = copy.deepcopy(self.state)
        receipt = next(
            item
            for item in state["audit_receipts"]
            if item.get("receipt_kind") == "campaign_closeout"
        )
        disposition = receipt["route_dispositions"][0]
        disposition["decision"] = "exclude"
        disposition["truth_effect"] = "scoped_exclusion"
        disposition["basis"] = "No result within budget."
        disposition["evidence_refs"] = [EXACT_CLAIM]
        self.assertTrue(
            any(
                "must cite an active scoped gate or a refuted claim" in item
                for item in self.error_messages(state)
            )
        )


if __name__ == "__main__":
    unittest.main()
