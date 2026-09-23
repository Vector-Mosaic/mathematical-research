from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import replace
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.candidate_a1_triage import (  # noqa: E402
    CANDIDATE_A1_TRIAGE_ADMISSION_READY,
    CANDIDATE_A1_TRIAGE_EVIDENCE_ID_PREFIX,
    CANDIDATE_A1_TRIAGE_EVIDENCE_SUBTYPE,
    CANDIDATE_A1_TRIAGE_INVALIDATED,
    CandidateA1ConcreteDefect,
    CandidateA1Ref,
    CandidateA1ReviewerProvenance,
    CandidateA1TriageRecord,
    EvidenceRevisionRef,
    candidate_a1_triage_evidence_id,
    candidate_a1_triage_from_evidence,
    prepare_candidate_a1_triage,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.research_model import deep_thaw  # noqa: E402


class CandidateA1TriageTests(unittest.TestCase):
    def candidate_ref(self, **overrides) -> CandidateA1Ref:
        values = {
            "mission_id": "mission.rh.public.1",
            "candidate_id": "candidate.rh.complete.1",
            "revision": 3,
            "digest_sha256": "a" * 64,
        }
        values.update(overrides)
        return CandidateA1Ref(**values)

    def evidence_ref(self, **overrides) -> EvidenceRevisionRef:
        values = {
            "mission_id": "mission.rh.public.1",
            "evidence_id": "evidence.hostile-review.1",
            "revision": 2,
            "digest_sha256": "b" * 64,
        }
        values.update(overrides)
        return EvidenceRevisionRef(**values)

    def reviewer(self, **overrides) -> CandidateA1ReviewerProvenance:
        values = {
            "reviewer_identity": "worker.independent-reviewer.1",
            "reviewer_thread_id": "thread:reviewer.1",
            "root_thread_id": "thread:root.1",
            "parent_thread_id": "thread:root.1",
            "assignment_id": "assignment.a1-review.1",
            "context_id": "context.a1-review.1",
            "context_revision": 4,
            "context_digest_sha256": "c" * 64,
            "grant_id": "grant.a1-review.1",
            "grant_digest_sha256": "d" * 64,
        }
        values.update(overrides)
        return CandidateA1ReviewerProvenance(**values)

    def defect(self, **overrides) -> CandidateA1ConcreteDefect:
        values = {
            "exact_defect": (
                "The asserted contour shift crosses the stated pole without a "
                "residue term."
            ),
            "affected_scope": "argument edge 7 and the complete-target conclusion",
            "sufficiency_basis": (
                "The missing nonzero residue defeats the equality on which the "
                "complete RH implication depends."
            ),
        }
        values.update(overrides)
        return CandidateA1ConcreteDefect(**values)

    def invalidated(self, **overrides) -> CandidateA1TriageRecord:
        values = {
            "candidate_ref": self.candidate_ref(),
            "disposition": CANDIDATE_A1_TRIAGE_INVALIDATED,
            "reviewer": self.reviewer(),
            "review_finding": (
                "Independent reconstruction found one exact defeating contour defect."
            ),
            "cited_basis": (self.evidence_ref(),),
            "concrete_defects": (self.defect(),),
            "no_remaining_material_objection": False,
            "limitations": ("review is scoped to this exact Candidate revision",),
        }
        values.update(overrides)
        return prepare_candidate_a1_triage(**values)

    def admission_ready(self, **overrides) -> CandidateA1TriageRecord:
        values = {
            "candidate_ref": self.candidate_ref(),
            "disposition": CANDIDATE_A1_TRIAGE_ADMISSION_READY,
            "reviewer": self.reviewer(),
            "review_finding": (
                "Independent reconstruction leaves no material objection to the "
                "exact frozen complete claim."
            ),
            "cited_basis": (self.evidence_ref(),),
            "no_remaining_material_objection": True,
            "limitations": ("Admission authority remains separate",),
        }
        values.update(overrides)
        return prepare_candidate_a1_triage(**values)

    def test_invalidated_prepares_one_reserved_inert_evidence_revision(self) -> None:
        record = self.invalidated()

        self.assertEqual(
            record.evidence.subtype,
            CANDIDATE_A1_TRIAGE_EVIDENCE_SUBTYPE,
        )
        self.assertEqual(record.evidence.revision, 1)
        self.assertEqual(record.evidence.canonical_effect, "none")
        self.assertEqual(record.canonical_effect, "none")
        self.assertEqual(record.public_effect, "none")
        self.assertEqual(record.evidence.availability_state, "verified_available")
        self.assertEqual(record.evidence.blob_roles, ())
        self.assertEqual(record.disposition, CANDIDATE_A1_TRIAGE_INVALIDATED)
        self.assertFalse(record.no_remaining_material_objection)
        self.assertEqual(record.cited_basis, (self.evidence_ref(),))
        self.assertEqual(
            record.payload_digest,
            hashlib.sha256(
                canonical_json_bytes(record.evidence.to_payload())
            ).hexdigest(),
        )
        subject = deep_thaw(record.evidence.subject)
        self.assertEqual(subject, record.subject_payload())
        self.assertEqual(subject["canonical_effect"], "none")
        self.assertEqual(subject["public_effect"], "none")
        self.assertIn(
            "does not change canonical RH truth",
            subject["non_inferences"],
        )
        self.assertIn(
            "does not authorize public disclosure",
            subject["non_inferences"],
        )

    def test_identity_is_exact_candidate_only_and_outcome_independent(self) -> None:
        candidate = self.candidate_ref()
        material = {
            "candidate_id": candidate.candidate_id,
            "revision": candidate.revision,
            "digest_sha256": candidate.digest_sha256,
        }
        expected = (
            CANDIDATE_A1_TRIAGE_EVIDENCE_ID_PREFIX
            + hashlib.sha256(canonical_json_bytes(material)).hexdigest()
        )
        invalidated = self.invalidated(candidate_ref=candidate)
        ready = self.admission_ready(
            candidate_ref=candidate,
            reviewer=self.reviewer(
                reviewer_identity="worker.independent-reviewer.2",
                reviewer_thread_id="thread:reviewer.2",
            ),
        )

        self.assertEqual(invalidated.evidence.evidence_id, expected)
        self.assertEqual(ready.evidence.evidence_id, expected)
        self.assertNotEqual(invalidated.payload_digest, ready.payload_digest)
        self.assertEqual(
            candidate_a1_triage_evidence_id(
                candidate_id=candidate.candidate_id,
                candidate_revision=candidate.revision,
                candidate_digest_sha256=candidate.digest_sha256,
            ),
            expected,
        )
        changed = self.candidate_ref(digest_sha256="e" * 64)
        self.assertNotEqual(
            expected,
            candidate_a1_triage_evidence_id(
                candidate_id=changed.candidate_id,
                candidate_revision=changed.revision,
                candidate_digest_sha256=changed.digest_sha256,
            ),
        )

    def test_admission_ready_is_defect_free_and_explicitly_objection_free(self) -> None:
        record = self.admission_ready()

        self.assertEqual(record.disposition, CANDIDATE_A1_TRIAGE_ADMISSION_READY)
        self.assertEqual(record.concrete_defects, ())
        self.assertTrue(record.no_remaining_material_objection)
        self.assertEqual(
            deep_thaw(record.evidence.subject)["concrete_defects"],
            [],
        )

    def test_only_two_dispositions_exist(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.admission_ready(disposition="withdrawn")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.admission_ready(disposition="remains_live")

    def test_invalidated_requires_source_backed_concrete_defect(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one concrete defect"):
            self.invalidated(concrete_defects=())
        defect = self.defect()
        record = self.invalidated(concrete_defects=(defect,), cited_basis=())
        self.assertEqual(record.concrete_defects, (defect,))
        self.assertEqual(record.cited_basis, ())
        self.assertEqual(
            set(deep_thaw(record.evidence.subject)["concrete_defects"][0]),
            {"exact_defect", "affected_scope", "sufficiency_basis"},
        )
        self.assertEqual(
            candidate_a1_triage_from_evidence(record.evidence),
            record,
        )
        with self.assertRaisesRegex(ValueError, "no objection remains"):
            self.invalidated(no_remaining_material_objection=True)

    def test_admission_ready_rejects_defect_or_remaining_objection(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot contain"):
            self.admission_ready(concrete_defects=(self.defect(),))
        with self.assertRaisesRegex(ValueError, "no remaining material objection"):
            self.admission_ready(no_remaining_material_objection=False)

    def test_all_cited_evidence_is_same_mission(self) -> None:
        foreign = self.evidence_ref(mission_id="mission.other")
        with self.assertRaisesRegex(ValueError, "Candidate Mission"):
            self.admission_ready(cited_basis=(foreign,))

    def test_reviewer_provenance_requires_disjoint_direct_child(self) -> None:
        with self.assertRaisesRegex(ValueError, "direct child"):
            self.reviewer(parent_thread_id="thread:other-parent")
        with self.assertRaisesRegex(ValueError, "role-disjoint"):
            self.reviewer(reviewer_thread_id="thread:root.1")
        with self.assertRaisesRegex(ValueError, "independent reviewer role"):
            self.reviewer(reviewer_role="mission_executive")

    def test_exact_evidence_round_trips_through_closed_parser(self) -> None:
        record = self.invalidated()
        reconstructed = candidate_a1_triage_from_evidence(record.evidence)

        self.assertEqual(reconstructed, record)
        reconstructed.verify_prepared()

        subject = deep_thaw(record.evidence.subject)
        subject["unexpected"] = "not accepted"
        malformed = replace(record.evidence, subject=subject)
        with self.assertRaisesRegex(ValueError, "exact closed shape"):
            candidate_a1_triage_from_evidence(malformed)

    def test_stale_revision_identity_subject_or_digest_is_rejected(self) -> None:
        record = self.invalidated()
        with self.assertRaisesRegex(ValueError, "revision must be exactly 1"):
            replace(record, evidence=replace(record.evidence, revision=2))
        with self.assertRaisesRegex(ValueError, "identity is stale"):
            replace(
                record,
                evidence=replace(record.evidence, evidence_id="evidence:other"),
            )
        changed_subject = deep_thaw(record.evidence.subject)
        changed_subject["review_finding"] = "different review"
        with self.assertRaisesRegex(ValueError, "subject is stale"):
            replace(
                record,
                evidence=replace(record.evidence, subject=changed_subject),
            )
        with self.assertRaisesRegex(ValueError, "payload digest is stale"):
            replace(record, payload_digest="f" * 64)

    def test_inputs_are_normalized_and_do_not_retain_mutable_aliases(self) -> None:
        limitations = ["second limitation", "first limitation"]
        record = self.admission_ready(limitations=limitations)
        limitations.append("mutated after preparation")

        self.assertEqual(
            record.limitations,
            ("first limitation", "second limitation"),
        )
        with self.assertRaises(TypeError):
            record.evidence.subject["disposition"] = "invalidated"


if __name__ == "__main__":
    unittest.main()
