from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from research_core.candidate_a1_triage import CandidateA1Ref
from research_core.canonical_admission import (
    CanonicalAdmissionError,
    admitted_result_from_decision,
    prepare_admitted_research_state,
    preview_admitted_result,
    write_admitted_result,
)
from research_core.complete_claim_admission import (
    ADMISSION_ADMITTER_ROLE,
    ADMISSION_DECISION_AUTHORIZE,
    ADMISSION_DECISION_REJECT,
    AdmissionActorProvenance,
    AdmissionEvidenceRef,
    AdmissionMaterialObjection,
    AdmissionOwnerRevisionRef,
    prepare_admission_decision,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_RELATIVE = Path(
    "projects/riemann_hypothesis/research_state.json"
)
STATE_PATH = REPO_ROOT / STATE_RELATIVE


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class CanonicalAdmissionTests(unittest.TestCase):
    def decision(self, candidate_disposition: str = "proof", disposition: str = ADMISSION_DECISION_AUTHORIZE):
        candidate = CandidateA1Ref(
            mission_id="mission.rh.autonomous.test",
            candidate_id=f"candidate.rh.{candidate_disposition}.test",
            revision=3,
            digest_sha256="a" * 64,
        )
        reject_kwargs = {}
        if disposition == ADMISSION_DECISION_REJECT:
            reject_kwargs = {
                "objections": (
                    AdmissionMaterialObjection(
                        "The terminal implication omits one required case.",
                        "the exact frozen final implication",
                        "Without it the complete claim does not follow.",
                    ),
                ),
                "cited_basis": (
                    AdmissionOwnerRevisionRef(
                        "candidate", candidate.candidate_id, candidate.revision,
                        candidate.digest_sha256,
                    ),
                ),
            }
        return prepare_admission_decision(
            candidate_ref=candidate,
            case_ref=AdmissionEvidenceRef(
                evidence_id="evidence:complete-claim-admission-case:" + "b" * 64,
                revision=1,
                payload_sha256="c" * 64,
            ),
            review_ref=AdmissionEvidenceRef(
                evidence_id="evidence:complete-claim-admission-review:" + "d" * 64,
                revision=1,
                payload_sha256="e" * 64,
            ),
            admitter=AdmissionActorProvenance(
                role=ADMISSION_ADMITTER_ROLE,
                identity="admitter:test",
                thread_id="thread:admitter",
                root_thread_id="thread:root",
                parent_thread_id="thread:root",
                assignment_id="assignment:admit",
                context_id="context:admit",
                context_revision=1,
                context_digest_sha256="f" * 64,
                grant_id="grant:admit",
                grant_digest_sha256="1" * 64,
            ),
            disposition=disposition,
            decision_basis="Exact independent Admission review has no material objection.",
            candidate_disposition=candidate_disposition,
            exact_claim=(
                "Every nontrivial zero of the Riemann zeta function has real part 1/2."
                if candidate_disposition == "proof"
                else "A nontrivial zero of the Riemann zeta function has real part other than 1/2."
            ),
            **reject_kwargs,
        )

    def test_symmetric_authorized_decisions_derive_exact_project_results(self) -> None:
        for candidate_disposition, result_disposition in (
            ("proof", "proved"),
            ("disproof", "disproved"),
        ):
            with self.subTest(candidate_disposition=candidate_disposition):
                decision = self.decision(candidate_disposition)
                result = admitted_result_from_decision(decision)
                self.assertEqual(result["disposition"], result_disposition)
                self.assertEqual(result["candidate_ref"], decision.candidate_ref.to_payload())
                self.assertEqual(
                    result["admission_decision_ref"],
                    {
                        "decision_id": decision.evidence.evidence_id,
                        "digest_sha256": decision.payload_digest,
                    },
                )

    def test_decision_alone_changes_only_project_terminal_truth(self) -> None:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        # Optional metadata is synthetic so this preservation test does not
        # depend on fields or ownership data in the public initialization.
        state["project"].update({
            "owners": ["Synthetic research owner"],
            "source_artifact_ref": "source.fixture.canonical-admission-metadata",
        })
        state["sources"].append({
            "id": "source.fixture.canonical-admission-metadata",
            "source_type": "synthetic_test_fixture",
            "title": "Synthetic project metadata preservation fixture",
        })
        original = copy.deepcopy(state)
        prepared, result, replayed = prepare_admitted_research_state(
            state, self.decision("proof")
        )
        self.assertFalse(replayed)
        self.assertEqual(prepared["project"]["proof_status"], "proved")
        self.assertEqual(prepared["project"]["admitted_result"], result)
        self.assertNotIn("active_target", prepared["project"])
        self.assertEqual(
            {key: value for key, value in prepared.items() if key != "project"},
            {key: value for key, value in original.items() if key != "project"},
        )
        for key in (
            "id",
            "title",
            "conjecture",
            "lifecycle_status",
            "as_of",
            "owners",
            "source_artifact_ref",
            "epistemic_rule",
        ):
            self.assertEqual(prepared["project"][key], original["project"][key])

    def test_non_authorizing_decision_has_no_canonical_route(self) -> None:
        with self.assertRaisesRegex(
            CanonicalAdmissionError, "only authorize_exact_delta"
        ) as raised:
            admitted_result_from_decision(self.decision(disposition=ADMISSION_DECISION_REJECT))
        self.assertEqual(raised.exception.code, "admission_decision_not_authorizing")

    def test_writer_is_exact_atomic_and_replayable(self) -> None:
        raw = STATE_PATH.read_bytes()
        source_commit = "2" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / STATE_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_bytes(raw)
            decision = self.decision("disproof")
            preview = preview_admitted_result(
                root,
                source_commit=source_commit,
                expected_prestate_sha256=_sha256(raw),
                decision=decision,
                _commit_reader=lambda _root, _commit: raw,
                _worktree_verifier=lambda _root, _commit: None,
            )
            self.assertFalse(preview.replayed)
            self.assertEqual(preview.disposition, "disproved")
            self.assertEqual(target.read_bytes(), raw)
            self.assertNotEqual(preview.poststate_sha256, _sha256(raw))

            result = write_admitted_result(
                root,
                source_commit=source_commit,
                expected_prestate_sha256=_sha256(raw),
                decision=decision,
                _commit_reader=lambda _root, _commit: raw,
                _worktree_verifier=lambda _root, _commit: None,
            )
            self.assertFalse(result.replayed)
            self.assertEqual(result, preview)
            self.assertEqual(result.disposition, "disproved")
            post_raw = target.read_bytes()
            self.assertEqual(result.poststate_sha256, _sha256(post_raw))
            self.assertNotEqual(post_raw, raw)
            post_state = json.loads(post_raw)
            self.assertEqual(post_state["project"]["proof_status"], "disproved")
            self.assertNotIn("active_target", post_state["project"])

            replay = write_admitted_result(
                root,
                source_commit=source_commit,
                expected_prestate_sha256=_sha256(post_raw),
                decision=decision,
                _commit_reader=lambda _root, _commit: post_raw,
                _worktree_verifier=lambda _root, _commit: None,
            )
            self.assertTrue(replay.replayed)
            self.assertEqual(target.read_bytes(), post_raw)

    def test_stale_or_conflicting_write_leaves_bytes_unchanged(self) -> None:
        raw = STATE_PATH.read_bytes()
        source_commit = "3" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / STATE_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_bytes(raw)
            with self.assertRaises(CanonicalAdmissionError) as stale:
                write_admitted_result(
                    root,
                    source_commit=source_commit,
                    expected_prestate_sha256="0" * 64,
                    decision=self.decision("proof"),
                    _commit_reader=lambda _root, _commit: raw,
                    _worktree_verifier=lambda _root, _commit: None,
                )
            self.assertEqual(stale.exception.code, "canonical_prestate_stale")
            self.assertEqual(target.read_bytes(), raw)

            first = write_admitted_result(
                root,
                source_commit=source_commit,
                expected_prestate_sha256=_sha256(raw),
                decision=self.decision("proof"),
                _commit_reader=lambda _root, _commit: raw,
                _worktree_verifier=lambda _root, _commit: None,
            )
            terminal = target.read_bytes()
            self.assertFalse(first.replayed)
            with self.assertRaises(CanonicalAdmissionError) as conflict:
                write_admitted_result(
                    root,
                    source_commit=source_commit,
                    expected_prestate_sha256=_sha256(terminal),
                    decision=self.decision("disproof"),
                    _commit_reader=lambda _root, _commit: terminal,
                    _worktree_verifier=lambda _root, _commit: None,
                )
            self.assertEqual(conflict.exception.code, "canonical_result_conflict")
            self.assertEqual(target.read_bytes(), terminal)

    def test_writer_requires_exact_clean_selected_git_worktree(self) -> None:
        raw = STATE_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / STATE_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_bytes(raw)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "admission-test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Admission Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "canonical prestate"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            selected = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            unrelated = root / "unrelated.txt"
            unrelated.write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(CanonicalAdmissionError) as dirty:
                write_admitted_result(
                    root,
                    source_commit=selected,
                    expected_prestate_sha256=_sha256(raw),
                    decision=self.decision("proof"),
                )
            self.assertEqual(dirty.exception.code, "canonical_worktree_dirty")
            self.assertEqual(target.read_bytes(), raw)

            unrelated.write_text("committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "advance head"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            with self.assertRaises(CanonicalAdmissionError) as wrong_head:
                write_admitted_result(
                    root,
                    source_commit=selected,
                    expected_prestate_sha256=_sha256(raw),
                    decision=self.decision("proof"),
                )
            self.assertEqual(
                wrong_head.exception.code, "canonical_worktree_head_mismatch"
            )
            self.assertEqual(target.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
