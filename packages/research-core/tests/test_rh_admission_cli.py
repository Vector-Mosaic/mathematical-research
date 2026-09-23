from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = (
    REPO_ROOT / "packages" / "research-core"
)
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.candidate_a1_triage import CandidateA1Ref  # noqa: E402
from research_core.complete_claim_admission import (  # noqa: E402
    ADMISSION_ADMITTER_ROLE,
    ADMISSION_DECISION_AUTHORIZE,
    ADMISSION_REVIEWER_ROLE,
    ADMISSION_REVIEW_NO_MATERIAL_OBJECTION,
    AdmissionActorProvenance,
    AdmissionEvidenceRef,
    prepare_admission_case,
    prepare_admission_decision,
    prepare_admission_review,
)
from research_core.evidence_store import EvidenceItemRevision  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "rh_admission.py"
SPEC = importlib.util.spec_from_file_location("rh_admission_tool", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TOOL
SPEC.loader.exec_module(TOOL)


MISSION_ID = "mission.rh.public.test"
DECISION_ID = "evidence:complete-claim-admission-decision:test"
DECISION_DIGEST = "a" * 64
SOURCE_DIGEST = "b" * 64
TARGET_DIGEST = "c" * 64
RELEASE_SHA = "d" * 40


def _digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _actor(
    role: str, thread: str, *, root_thread: str = "thread:root"
) -> AdmissionActorProvenance:
    return AdmissionActorProvenance(
        role=role,
        identity=f"{role}:{thread}",
        thread_id=thread,
        root_thread_id=root_thread,
        parent_thread_id=root_thread,
        assignment_id=f"assignment:{thread}",
        context_id=f"context:{thread}",
        context_revision=1,
        context_digest_sha256="9" * 64,
        grant_id=f"grant:{thread}",
        grant_digest_sha256="8" * 64,
    )


def _evidence_readback(record: EvidenceItemRevision, digest: str) -> dict[str, object]:
    return {
        "retrieval_handle": f"evidence:{record.evidence_id}@{record.revision}",
        "readback_kind": "current_owner_document",
        "current_reference": {
            "kind": "evidence",
            "identity": record.evidence_id,
            "revision": record.revision,
            "payload_sha256": digest,
        },
        "document": record.to_payload(),
    }


def _authorizing_fetch(
    *, candidate_readback_kind: str = "current_owner_document"
) -> tuple[dict[str, object], object]:
    exact_claim = (
        "Every nontrivial zero of the Riemann zeta function has real part 1/2."
    )
    candidate_document = {
        "schema_version": 4,
        "exact_statement": exact_claim,
        "complete_target_claim": {
            "target": "riemann_hypothesis",
            "disposition": "proof",
        },
    }
    candidate = CandidateA1Ref(
        mission_id=MISSION_ID,
        candidate_id="candidate.rh.proof.readback",
        revision=1,
        digest_sha256=_digest(candidate_document),
    )
    triage = EvidenceItemRevision(
        evidence_id="evidence:candidate-a1-triage:readback",
        revision=1,
        subtype="candidate_a1_triage",
        subject={"mission_id": MISSION_ID, "disposition": "admission_ready"},
        exact_scope="exact Candidate A1 triage",
        rigor="independent_candidate_a1_triage",
        limitations=(),
        non_inferences=("does not change canonical RH truth",),
        security_classification="workspace_internal",
        retention="mission_candidate_a1_triage",
        blob_roles=(),
        availability_state="verified_available",
    )
    triage_digest = _digest(triage.to_payload())
    case = prepare_admission_case(
        project_id=TOOL.PROJECT_ID,
        candidate_ref=candidate,
        triage_ref=AdmissionEvidenceRef(
            triage.evidence_id, triage.revision, triage_digest
        ),
        candidate_disposition="proof",
        exact_claim=exact_claim,
        candidate_author_thread_id="thread:author",
        triage_reviewer_thread_id="thread:triage",
        source_closure=(),
    )
    case_ref = AdmissionEvidenceRef(
        case.evidence.evidence_id, 1, case.payload_digest
    )
    review = prepare_admission_review(
        candidate_ref=candidate,
        case_ref=case_ref,
        reviewer=_actor(ADMISSION_REVIEWER_ROLE, "thread:review"),
        disposition=ADMISSION_REVIEW_NO_MATERIAL_OBJECTION,
        review_finding="The exact frozen proof has no remaining material objection.",
    )
    review_ref = AdmissionEvidenceRef(
        review.evidence.evidence_id, 1, review.payload_digest
    )
    decision = prepare_admission_decision(
        candidate_ref=candidate,
        case_ref=case_ref,
        review_ref=review_ref,
        admitter=_actor(
            ADMISSION_ADMITTER_ROLE,
            "thread:admit",
            root_thread="thread:successor-root",
        ),
        disposition=ADMISSION_DECISION_AUTHORIZE,
        decision_basis="The exact role-disjoint review supports the exact delta.",
        candidate_disposition="proof",
        exact_claim=exact_claim,
    )
    candidate_reference = {
        "kind": "candidate",
        "identity": candidate.candidate_id,
        "revision": candidate.revision,
        "payload_sha256": candidate.digest_sha256,
    }
    selected = [
        _evidence_readback(triage, triage_digest),
        _evidence_readback(case.evidence, case.payload_digest),
        _evidence_readback(review.evidence, review.payload_digest),
        _evidence_readback(decision.evidence, decision.payload_digest),
        {
            "retrieval_handle": (
                f"candidate:{candidate.candidate_id}@{candidate.revision}"
            ),
            "readback_kind": candidate_readback_kind,
            "current_reference": candidate_reference,
            "document": candidate_document,
            "candidate_a1": {
                "candidate_ref": candidate_reference,
                "retrieval_handle": (
                    f"candidate:{candidate.candidate_id}@{candidate.revision}"
                ),
                "classification": "purported_complete_rh_proof_or_disproof",
                "disposition": "proof",
                "hold_lifecycle": "open",
                "canonical_effect": "none",
                "mathematical_effect": "none",
                "triage": {
                    "disposition": "admission_ready",
                    "evidence_ref": {
                        "kind": "evidence",
                        "identity": triage.evidence_id,
                        "revision": 1,
                        "payload_sha256": triage_digest,
                    },
                    "retrieval_handle": f"evidence:{triage.evidence_id}@1",
                },
            },
        },
    ]
    payload = {
        "schema": TOOL.FETCH_SCHEMA,
        "mission_id": MISSION_ID,
        "release_sha": RELEASE_SHA,
        "binding": {
            "schema": TOOL.FETCH_BINDING_SCHEMA,
            "mission_id": MISSION_ID,
            "release_sha": RELEASE_SHA,
        },
        "systemd": {
            "ActiveState": "inactive",
            "SubState": "dead",
            "MainPID": 0,
            "UnitFileState": "disabled",
        },
        "offline_transition": {
            "service_quiesced": True,
            "runtime_lock_present": False,
            "pending": False,
            "transition": None,
            "binding_matches_selected": True,
            "host_state_schema_version": "workstation_control.rh_mission_host_state.v7",
            "inactive_fetch_eligible": True,
        },
        "host_state": {
            "schemaVersion": "workstation_control.rh_mission_host_state.v7",
            "missionId": MISSION_ID,
            "activeGoal": None,
            "captureRecoveryRequired": [],
            "updatedAt": "2026-08-29T00:00:00Z",
        },
        "mission_reconstruction": {
            "schema_version": TOOL.RECONSTRUCTION_SCHEMA,
            "project_id": TOOL.PROJECT_ID,
            "mission_id": MISSION_ID,
            "observed_project_commit": 42,
            "latest_executive_epoch": {
                "executive_epoch_id": "epoch:checkpointed",
                "state": "checkpointed",
            },
            "recovery_opening": {
                "cut": {"reference": {"checkpoint_id": "checkpoint:latest"}},
                "admitted_result": None,
            },
            "selected_readback": selected,
        },
    }
    return payload, decision


class _Store:
    def __init__(self) -> None:
        self.read_refs: list[object] = []
        self.rebind_calls: list[dict[str, object]] = []
        self.decision = SimpleNamespace(
            candidate_ref=SimpleNamespace(mission_id=MISSION_ID),
            disposition=TOOL.ADMISSION_DECISION_AUTHORIZE,
        )

    def read_complete_claim_admission_decision(self, reference: object) -> object:
        self.read_refs.append(reference)
        return self.decision

    def rebind_admitted_result(
        self, **kwargs: object
    ) -> TOOL.AdmittedResultRebindResult:
        self.rebind_calls.append(kwargs)
        return TOOL.AdmittedResultRebindResult(
            mission_id=MISSION_ID,
            project_id=TOOL.PROJECT_ID,
            workspace_root=str(kwargs.pop("workspace_root", "C:/workspace")),
            selected_release_sha=RELEASE_SHA,
            admission_decision_id=DECISION_ID,
            admission_decision_digest=DECISION_DIGEST,
            source_canonical_authority_digest=SOURCE_DIGEST,
            target_canonical_authority_digest=TARGET_DIGEST,
            predecessor_checkpoint_ref="checkpoint:test",
            project_commit=42,
            root_digest="e" * 64,
            transition_digest="f" * 64,
            idempotent=False,
        )


def _request(workspace_root: str) -> dict[str, object]:
    return {
        "schema_version": TOOL.REBIND_REQUEST_SCHEMA,
        "mission_id": MISSION_ID,
        "project_id": TOOL.PROJECT_ID,
        "workspace_root": workspace_root,
        "selected_release_sha": RELEASE_SHA,
        "admission_decision_ref": {
            "id": DECISION_ID,
            "digest_sha256": DECISION_DIGEST,
        },
        "source_canonical_authority_digest": SOURCE_DIGEST,
        "require_latest_durable_checkpoint_predecessor": True,
        "require_no_admitted_successor": True,
    }


class RhAdmissionCliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_safe_no_arg_and_json_usage_expose_closed_operator_boundary(self) -> None:
        no_arg = self._run()
        self.assertEqual(no_arg.returncode, 0, no_arg.stderr)
        self.assertIn(
            "verbs: usage, write-canonical-result, rebind-canonical-result",
            no_arg.stdout,
        )

        usage = self._run("--format", "json", "usage")
        self.assertEqual(usage.returncode, 0, usage.stderr)
        self.assertEqual(usage.stderr, "")
        payload = json.loads(usage.stdout)
        self.assertEqual(payload["tool_id"], TOOL.TOOL_ID)
        self.assertEqual(
            [item["name"] for item in payload["verbs"]],
            ["usage", "write-canonical-result", "rebind-canonical-result"],
        )
        self.assertFalse(payload["authority_boundary"]["decides_mathematics"])
        self.assertFalse(payload["authority_boundary"]["admits_successor"])
        self.assertEqual(payload["authority_boundary"]["public_effect"], "none")
        self.assertTrue(payload["side_effects"]["supports_dry_run"])
        self.assertIn(
            "--dry-run", {item["name"] for item in payload["selectors"]}
        )

    def test_write_dry_run_uses_pure_preview_and_reports_zero_effect(self) -> None:
        store = _Store()
        expected = TOOL.CanonicalAdmissionWriteResult(
            state_path=(
                "projects/riemann_hypothesis/"
                "research_state.json"
            ),
            source_commit=RELEASE_SHA,
            decision_id=DECISION_ID,
            decision_digest=DECISION_DIGEST,
            disposition="proved",
            prestate_sha256="1" * 64,
            poststate_sha256="2" * 64,
            replayed=False,
        )
        stream = io.StringIO()
        with (
            patch.object(
                TOOL,
                "_read_authorizing_decision_from_readback",
                return_value=store.decision,
            ),
            patch.object(
                TOOL, "preview_admitted_result", return_value=expected
            ) as preview,
            patch.object(TOOL, "write_admitted_result") as writer,
            redirect_stdout(stream),
        ):
            exit_code = TOOL.main(
                [
                    "--dry-run",
                    "write-canonical-result",
                    "--root",
                    str(REPO_ROOT),
                    "--admission-readback",
                    "C:/readback/latest.json",
                    "--project-id",
                    TOOL.PROJECT_ID,
                    "--mission-id",
                    MISSION_ID,
                    "--admission-decision-id",
                    DECISION_ID,
                    "--admission-decision-digest",
                    DECISION_DIGEST,
                    "--source-commit",
                    RELEASE_SHA,
                    "--expected-prestate-sha256",
                    "1" * 64,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["data"]["dry_run"])
        self.assertEqual(payload["data"]["canonical_effect"], "none")
        self.assertEqual(
            payload["data"]["expected_canonical_effect"],
            "exact_authorized_delta",
        )
        self.assertEqual(payload["data"]["poststate_sha256"], "2" * 64)
        preview.assert_called_once()
        writer.assert_not_called()

    def test_write_exact_reads_fetch_then_calls_only_canonical_writer(self) -> None:
        store = _Store()
        expected = TOOL.CanonicalAdmissionWriteResult(
            state_path=(
                "projects/riemann_hypothesis/"
                "research_state.json"
            ),
            source_commit=RELEASE_SHA,
            decision_id=DECISION_ID,
            decision_digest=DECISION_DIGEST,
            disposition="proved",
            prestate_sha256="1" * 64,
            poststate_sha256="2" * 64,
            replayed=False,
        )
        stream = io.StringIO()
        with (
            patch.object(
                TOOL,
                "_read_authorizing_decision_from_readback",
                return_value=store.decision,
            ) as readback,
            patch.object(
                TOOL, "write_admitted_result", return_value=expected
            ) as writer,
            redirect_stdout(stream),
        ):
            exit_code = TOOL.main(
                [
                    "write-canonical-result",
                    "--root",
                    str(REPO_ROOT),
                    "--admission-readback",
                    "C:/readback/latest.json",
                    "--project-id",
                    TOOL.PROJECT_ID,
                    "--mission-id",
                    MISSION_ID,
                    "--admission-decision-id",
                    DECISION_ID,
                    "--admission-decision-digest",
                    DECISION_DIGEST,
                    "--source-commit",
                    RELEASE_SHA,
                    "--expected-prestate-sha256",
                    "1" * 64,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["verb"], "write-canonical-result")
        self.assertEqual(payload["data"], expected.to_payload())
        readback.assert_called_once()
        call = readback.call_args
        self.assertEqual(call.args, ("C:/readback/latest.json",))
        self.assertEqual(call.kwargs["project_id"], TOOL.PROJECT_ID)
        self.assertEqual(call.kwargs["mission_id"], MISSION_ID)
        self.assertEqual(call.kwargs["source_commit"], RELEASE_SHA)
        reference = call.kwargs["decision_ref"]
        self.assertEqual(reference.evidence_id, DECISION_ID)
        self.assertEqual(reference.revision, 1)
        self.assertEqual(reference.payload_sha256, DECISION_DIGEST)
        writer.assert_called_once_with(
            REPO_ROOT.resolve(),
            source_commit=RELEASE_SHA,
            expected_prestate_sha256="1" * 64,
            decision=store.decision,
        )

    def test_rebind_verifies_exact_inputs_and_calls_store_seam_once(self) -> None:
        store = _Store()
        target_snapshot = object()
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = str(Path(directory, "workspace").resolve())
            request_path = Path(directory, "request.json")
            request_path.write_text(
                json.dumps(_request(workspace_root)), encoding="utf-8"
            )
            expected = TOOL.AdmittedResultRebindResult(
                mission_id=MISSION_ID,
                project_id=TOOL.PROJECT_ID,
                workspace_root=workspace_root,
                selected_release_sha=RELEASE_SHA,
                admission_decision_id=DECISION_ID,
                admission_decision_digest=DECISION_DIGEST,
                source_canonical_authority_digest=SOURCE_DIGEST,
                target_canonical_authority_digest=TARGET_DIGEST,
                predecessor_checkpoint_ref="checkpoint:test",
                project_commit=42,
                root_digest="e" * 64,
                transition_digest="f" * 64,
                idempotent=False,
            )
            store.rebind_admitted_result = unittest.mock.Mock(return_value=expected)
            stream = io.StringIO()
            with (
                patch.object(
                    TOOL, "_verify_selected_release", return_value=target_snapshot
                ) as verify_release,
                patch.object(TOOL, "_open_store", return_value=store) as open_store,
                redirect_stdout(stream),
            ):
                exit_code = TOOL.main(
                    [
                        "rebind-canonical-result",
                        "--request",
                        str(request_path),
                        "--workspace-root",
                        workspace_root,
                        "--project-id",
                        TOOL.PROJECT_ID,
                        "--mission-id",
                        MISSION_ID,
                        "--format",
                        "json",
                    ]
                )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"], dict(expected.to_mapping()))
        verify_release.assert_called_once_with(REPO_ROOT, RELEASE_SHA)
        open_store.assert_called_once_with(workspace_root, TOOL.PROJECT_ID)
        self.assertEqual(len(store.read_refs), 1)
        reference = store.read_refs[0]
        store.rebind_admitted_result.assert_called_once_with(
            mission_id=MISSION_ID,
            admission_decision_ref=reference,
            source_canonical_authority_digest=SOURCE_DIGEST,
            target_canonical_snapshot=target_snapshot,
            selected_release_sha=RELEASE_SHA,
            dry_run=False,
        )

    def test_rebind_dry_run_uses_store_rollback_mode_and_reports_zero_effect(
        self,
    ) -> None:
        store = _Store()
        target_snapshot = object()
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = str(Path(directory, "workspace").resolve())
            request_path = Path(directory, "request.json")
            request_path.write_text(
                json.dumps(_request(workspace_root)), encoding="utf-8"
            )
            expected = TOOL.AdmittedResultRebindResult(
                mission_id=MISSION_ID,
                project_id=TOOL.PROJECT_ID,
                workspace_root=workspace_root,
                selected_release_sha=RELEASE_SHA,
                admission_decision_id=DECISION_ID,
                admission_decision_digest=DECISION_DIGEST,
                source_canonical_authority_digest=SOURCE_DIGEST,
                target_canonical_authority_digest=TARGET_DIGEST,
                predecessor_checkpoint_ref="checkpoint:test",
                project_commit=42,
                root_digest="e" * 64,
                transition_digest="f" * 64,
                idempotent=False,
            )
            store.rebind_admitted_result = unittest.mock.Mock(return_value=expected)
            stream = io.StringIO()
            with (
                patch.object(
                    TOOL, "_verify_selected_release", return_value=target_snapshot
                ),
                patch.object(TOOL, "_open_store", return_value=store),
                redirect_stdout(stream),
            ):
                exit_code = TOOL.main(
                    [
                        "rebind-canonical-result",
                        "--request",
                        str(request_path),
                        "--workspace-root",
                        workspace_root,
                        "--project-id",
                        TOOL.PROJECT_ID,
                        "--mission-id",
                        MISSION_ID,
                        "--dry-run",
                        "--format",
                        "json",
                    ]
                )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["data"]["dry_run"])
        self.assertEqual(payload["data"]["canonical_effect"], "none")
        self.assertEqual(payload["data"]["workspace_effect"], "none")
        store.rebind_admitted_result.assert_called_once()
        self.assertTrue(store.rebind_admitted_result.call_args.kwargs["dry_run"])

    def test_rebind_rejects_mismatched_command_binding_before_read_or_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = str(Path(directory, "workspace").resolve())
            request = _request(workspace_root)
            request["mission_id"] = "mission.rh.other"
            request_path = Path(directory, "request.json")
            request_path.write_text(json.dumps(request), encoding="utf-8")
            stream = io.StringIO()
            with (
                patch.object(TOOL, "_verify_selected_release") as verify_release,
                patch.object(TOOL, "_open_store") as open_store,
                redirect_stdout(stream),
            ):
                exit_code = TOOL.main(
                    [
                        "rebind-canonical-result",
                        "--request",
                        str(request_path),
                        "--workspace-root",
                        workspace_root,
                        "--project-id",
                        TOOL.PROJECT_ID,
                        "--mission-id",
                        MISSION_ID,
                        "--format",
                        "json",
                    ]
                )
        self.assertEqual(exit_code, 2)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["data"]["canonical_effect"], "none")
        verify_release.assert_not_called()
        open_store.assert_not_called()

    def test_selected_release_record_and_snapshot_are_both_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / TOOL.RELEASE_RECORD_RELATIVE_PATH).write_text(
                json.dumps(
                    {
                        "schema_version": TOOL.RELEASE_RECORD_SCHEMA,
                        "release_sha": RELEASE_SHA,
                    }
                ),
                encoding="utf-8",
            )
            sentinel = object()
            loaded = SimpleNamespace(ok=True, value=sentinel)
            with (
                patch.object(TOOL, "CanonicalStateSnapshot", object),
                patch.object(
                    TOOL, "load_canonical_snapshot", return_value=loaded
                ) as load_snapshot,
                patch.object(
                    TOOL, "verify_snapshot_integrity", return_value=True
                ) as verify_snapshot,
            ):
                result = TOOL._verify_selected_release(root, RELEASE_SHA)
        self.assertIs(result, sentinel)
        load_snapshot.assert_called_once_with(root, source_commit=RELEASE_SHA)
        verify_snapshot.assert_called_once_with(sentinel)

    def test_inactive_fetch_reconstructs_exact_authorizing_chain_without_sqlite(
        self,
    ) -> None:
        payload, expected = _authorizing_fetch()
        payload["mission_reconstruction"]["selected_readback"].append(
            {
                "retrieval_handle": "evidence:unrelated@1",
                "readback_kind": "current_owner_document",
                "current_reference": {
                    "kind": "evidence",
                    "identity": "unrelated",
                    "revision": 1,
                    "payload_sha256": "7" * 64,
                },
                "document": {"unrelated": True},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory, "latest.json")
            snapshot.write_text(json.dumps(payload), encoding="utf-8")
            actual = TOOL._read_authorizing_decision_from_readback(
                str(snapshot),
                decision_ref=AdmissionEvidenceRef(
                    expected.evidence.evidence_id,
                    1,
                    expected.payload_digest,
                ),
                project_id=TOOL.PROJECT_ID,
                mission_id=MISSION_ID,
                source_commit=RELEASE_SHA,
            )
        self.assertEqual(actual, expected)

    def test_inactive_fetch_accepts_frozen_open_a1_after_owner_advances(
        self,
    ) -> None:
        payload, expected = _authorizing_fetch(
            candidate_readback_kind="open_candidate_a1_document"
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory, "latest.json")
            snapshot.write_text(json.dumps(payload), encoding="utf-8")
            actual = TOOL._read_authorizing_decision_from_readback(
                str(snapshot),
                decision_ref=AdmissionEvidenceRef(
                    expected.evidence.evidence_id,
                    1,
                    expected.payload_digest,
                ),
                project_id=TOOL.PROJECT_ID,
                mission_id=MISSION_ID,
                source_commit=RELEASE_SHA,
            )
        self.assertEqual(actual, expected)

    def test_readback_missing_chain_member_or_quiescence_has_no_route(self) -> None:
        payload, decision = _authorizing_fetch()
        selected = payload["mission_reconstruction"]["selected_readback"]
        payload["mission_reconstruction"]["selected_readback"] = [
            item
            for item in selected
            if item.get("current_reference", {}).get("identity")
            != decision.review_ref.evidence_id
        ]
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory, "latest.json")
            snapshot.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(TOOL.AdmissionToolError) as missing:
                TOOL._read_authorizing_decision_from_readback(
                    str(snapshot),
                    decision_ref=AdmissionEvidenceRef(
                        decision.evidence.evidence_id,
                        1,
                        decision.payload_digest,
                    ),
                    project_id=TOOL.PROJECT_ID,
                    mission_id=MISSION_ID,
                    source_commit=RELEASE_SHA,
                )
            self.assertEqual(missing.exception.code, "admission_readback_invalid")

            payload, decision = _authorizing_fetch()
            payload["offline_transition"]["runtime_lock_present"] = True
            snapshot.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(TOOL.AdmissionToolError) as active:
                TOOL._read_authorizing_decision_from_readback(
                    str(snapshot),
                    decision_ref=AdmissionEvidenceRef(
                        decision.evidence.evidence_id,
                        1,
                        decision.payload_digest,
                    ),
                    project_id=TOOL.PROJECT_ID,
                    mission_id=MISSION_ID,
                    source_commit=RELEASE_SHA,
                )
            self.assertEqual(active.exception.code, "admission_readback_invalid")


if __name__ == "__main__":
    unittest.main()
