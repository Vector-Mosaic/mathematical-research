from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.candidate_a1_triage import CandidateA1Ref  # noqa: E402
from research_core.complete_claim_admission import (  # noqa: E402
    ADMISSION_ADMITTER_ROLE,
    ADMISSION_DECISION_AUTHORIZE,
    ADMISSION_DECISION_REJECT,
    ADMISSION_REVIEWER_ROLE,
    ADMISSION_REVIEW_MATERIAL_OBJECTION,
    ADMISSION_REVIEW_NO_MATERIAL_OBJECTION,
    AdmissionActorProvenance,
    AdmissionEvidenceRef,
    AdmissionMaterialObjection,
    AdmissionOwnerRevisionRef,
    admission_case_from_evidence,
    admission_decision_from_evidence,
    admission_review_from_evidence,
    prepare_admission_case,
    prepare_admission_decision,
    prepare_admission_review,
)


class CompleteClaimAdmissionDomainTests(unittest.TestCase):
    def candidate(self) -> CandidateA1Ref:
        return CandidateA1Ref("mission.1", "candidate.complete", 2, "a" * 64)

    def evidence_ref(self, name: str, digest: str) -> AdmissionEvidenceRef:
        return AdmissionEvidenceRef(name, 1, digest * 64)

    def actor(self, role: str, suffix: str) -> AdmissionActorProvenance:
        return AdmissionActorProvenance(
            role=role,
            identity=f"actor.{suffix}",
            thread_id=f"thread:{suffix}",
            root_thread_id="thread:root",
            parent_thread_id="thread:root",
            assignment_id=f"assignment.{suffix}",
            context_id="context:admission",
            context_revision=1,
            context_digest_sha256="c" * 64,
            grant_id=f"grant.{suffix}",
            grant_digest_sha256="d" * 64,
        )

    def case(self):
        return prepare_admission_case(
            project_id="project.rh",
            candidate_ref=self.candidate(),
            triage_ref=self.evidence_ref("evidence:triage", "b"),
            candidate_disposition="proof",
            exact_claim="The exact argument proves RH.",
            candidate_author_thread_id="thread:author",
            triage_reviewer_thread_id="thread:triage",
            source_closure=(
                AdmissionOwnerRevisionRef("evidence", "evidence.basis", 1, "e" * 64),
            ),
        )

    def review(self, case, disposition=ADMISSION_REVIEW_NO_MATERIAL_OBJECTION):
        case_ref = AdmissionEvidenceRef(
            case.evidence.evidence_id, 1, case.payload_digest
        )
        objections = ()
        if disposition == ADMISSION_REVIEW_MATERIAL_OBJECTION:
            objections = (
                AdmissionMaterialObjection(
                    "The terminal implication omits one case.",
                    "the final implication",
                    "Without it the complete claim does not follow.",
                ),
            )
        return prepare_admission_review(
            candidate_ref=self.candidate(),
            case_ref=case_ref,
            reviewer=self.actor(ADMISSION_REVIEWER_ROLE, "reviewer"),
            disposition=disposition,
            review_finding="Independent exact reconstruction completed.",
            objections=objections,
            cited_basis=(
                AdmissionOwnerRevisionRef("evidence", "evidence.basis", 1, "e" * 64),
            ),
        )

    def test_case_review_decision_are_inert_exact_round_trips(self) -> None:
        case = self.case()
        review = self.review(case)
        case_ref = AdmissionEvidenceRef(
            case.evidence.evidence_id, 1, case.payload_digest
        )
        review_ref = AdmissionEvidenceRef(
            review.evidence.evidence_id, 1, review.payload_digest
        )
        decision = prepare_admission_decision(
            candidate_ref=self.candidate(),
            case_ref=case_ref,
            review_ref=review_ref,
            admitter=self.actor(ADMISSION_ADMITTER_ROLE, "admitter"),
            disposition=ADMISSION_DECISION_AUTHORIZE,
            decision_basis="The exact frozen Case and independent Review qualify.",
            candidate_disposition="proof",
            exact_claim=case.exact_claim,
        )

        self.assertEqual(admission_case_from_evidence(case.evidence), case)
        self.assertEqual(admission_review_from_evidence(review.evidence), review)
        self.assertEqual(admission_decision_from_evidence(decision.evidence), decision)
        self.assertEqual(decision.canonical_result_binding["result"], "proved")
        self.assertEqual(decision.evidence.canonical_effect, "none")
        for subject in (
            case.subject_payload(),
            review.subject_payload(),
            decision.subject_payload(),
        ):
            self.assertEqual(subject["canonical_effect"], "none")
            self.assertEqual(subject["mission_effect"], "none")
            self.assertEqual(subject["strategy_effect"], "none")
            self.assertEqual(subject["public_effect"], "none")

    def test_only_authorize_prepares_symmetric_result_binding(self) -> None:
        case = self.case()
        review = self.review(case)
        case_ref = AdmissionEvidenceRef(
            case.evidence.evidence_id, 1, case.payload_digest
        )
        review_ref = AdmissionEvidenceRef(
            review.evidence.evidence_id, 1, review.payload_digest
        )
        rejected = prepare_admission_decision(
            candidate_ref=self.candidate(),
            case_ref=case_ref,
            review_ref=review_ref,
            admitter=self.actor(ADMISSION_ADMITTER_ROLE, "admitter"),
            disposition=ADMISSION_DECISION_REJECT,
            decision_basis="The exact material objection defeats this Case.",
            candidate_disposition="proof",
            exact_claim=case.exact_claim,
            objections=(
                AdmissionMaterialObjection(
                    "The terminal implication omits one required case.",
                    "the exact frozen final implication",
                    "Without that case the complete RH claim does not follow.",
                ),
            ),
            cited_basis=(
                AdmissionOwnerRevisionRef(
                    "evidence", "evidence.basis", 1, "e" * 64
                ),
            ),
        )
        self.assertIsNone(rejected.canonical_result_binding)
        self.assertEqual(
            admission_decision_from_evidence(rejected.evidence), rejected
        )
        self.assertEqual(len(rejected.objections), 1)

        disproof_case = prepare_admission_case(
            project_id="project.rh",
            candidate_ref=self.candidate(),
            triage_ref=self.evidence_ref("evidence:triage", "b"),
            candidate_disposition="disproof",
            exact_claim="The exact counterexample disproves RH.",
            candidate_author_thread_id="thread:author",
            triage_reviewer_thread_id="thread:triage",
            source_closure=(),
        )
        disproof_case_ref = AdmissionEvidenceRef(
            disproof_case.evidence.evidence_id, 1, disproof_case.payload_digest
        )
        disproof_review = prepare_admission_review(
            candidate_ref=self.candidate(),
            case_ref=disproof_case_ref,
            reviewer=self.actor(ADMISSION_REVIEWER_ROLE, "reviewer"),
            disposition=ADMISSION_REVIEW_NO_MATERIAL_OBJECTION,
            review_finding="No material objection remains.",
        )
        disproof_review_ref = AdmissionEvidenceRef(
            disproof_review.evidence.evidence_id, 1, disproof_review.payload_digest
        )
        disproof = prepare_admission_decision(
            candidate_ref=self.candidate(),
            case_ref=disproof_case_ref,
            review_ref=disproof_review_ref,
            admitter=self.actor(ADMISSION_ADMITTER_ROLE, "admitter"),
            disposition=ADMISSION_DECISION_AUTHORIZE,
            decision_basis="The exact counterexample survives independent review.",
            candidate_disposition="disproof",
            exact_claim=disproof_case.exact_claim,
        )
        self.assertEqual(disproof.canonical_result_binding["result"], "disproved")

    def test_review_dispositions_are_exact_and_actor_roles_are_disjoint(self) -> None:
        case = self.case()
        with self.assertRaisesRegex(ValueError, "cannot carry"):
            prepare_admission_review(
                candidate_ref=self.candidate(),
                case_ref=AdmissionEvidenceRef(
                    case.evidence.evidence_id, 1, case.payload_digest
                ),
                reviewer=self.actor(ADMISSION_REVIEWER_ROLE, "reviewer"),
                disposition=ADMISSION_REVIEW_NO_MATERIAL_OBJECTION,
                review_finding="No objection.",
                objections=(AdmissionMaterialObjection("x", "y", "z"),),
            )
        with self.assertRaisesRegex(ValueError, "requires a concrete"):
            prepare_admission_review(
                candidate_ref=self.candidate(),
                case_ref=AdmissionEvidenceRef(
                    case.evidence.evidence_id, 1, case.payload_digest
                ),
                reviewer=self.actor(ADMISSION_REVIEWER_ROLE, "reviewer"),
                disposition=ADMISSION_REVIEW_MATERIAL_OBJECTION,
                review_finding="One material objection remains.",
                objections=(),
            )
        with self.assertRaisesRegex(ValueError, "direct child"):
            replace(
                self.actor(ADMISSION_REVIEWER_ROLE, "reviewer"),
                parent_thread_id="thread:other",
            )

    def test_decision_reject_requires_concrete_objection_and_exact_basis(self) -> None:
        case = self.case()
        review = self.review(case)
        case_ref = AdmissionEvidenceRef(
            case.evidence.evidence_id, 1, case.payload_digest
        )
        review_ref = AdmissionEvidenceRef(
            review.evidence.evidence_id, 1, review.payload_digest
        )
        common = {
            "candidate_ref": self.candidate(),
            "case_ref": case_ref,
            "review_ref": review_ref,
            "admitter": self.actor(ADMISSION_ADMITTER_ROLE, "admitter"),
            "disposition": ADMISSION_DECISION_REJECT,
            "decision_basis": "A concrete defect defeats this exact Case.",
            "candidate_disposition": "proof",
            "exact_claim": case.exact_claim,
        }
        objection = AdmissionMaterialObjection(
            "The terminal implication omits one required case.",
            "the exact frozen final implication",
            "Without that case the complete RH claim does not follow.",
        )
        with self.assertRaisesRegex(ValueError, "concrete material objection"):
            prepare_admission_decision(**common)
        with self.assertRaisesRegex(ValueError, "Case-bounded cited basis"):
            prepare_admission_decision(**common, objections=(objection,))
        with self.assertRaisesRegex(ValueError, "cannot carry objections"):
            prepare_admission_decision(
                **{**common, "disposition": ADMISSION_DECISION_AUTHORIZE},
                objections=(objection,),
            )
        with self.assertRaisesRegex(ValueError, "cannot carry cited basis"):
            prepare_admission_decision(
                **{**common, "disposition": ADMISSION_DECISION_AUTHORIZE},
                cited_basis=(
                    AdmissionOwnerRevisionRef(
                        "candidate", self.candidate().candidate_id,
                        self.candidate().revision, self.candidate().digest_sha256,
                    ),
                ),
            )

    def test_stale_evidence_bytes_are_rejected(self) -> None:
        case = self.case()
        changed = dict(case.subject_payload())
        changed["exact_claim"] = "changed"
        with self.assertRaisesRegex(ValueError, "subject is stale"):
            replace(case, evidence=replace(case.evidence, subject=changed))


if __name__ == "__main__":
    unittest.main()
