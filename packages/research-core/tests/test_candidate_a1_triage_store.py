from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for candidate in (PACKAGE_ROOT, Path(__file__).resolve().parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core import workspace_store as store_module  # noqa: E402
from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
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
    prepare_candidate_a1_triage,
)
from research_core.candidate_revision import (  # noqa: E402
    candidate_a1_requirement,
    candidate_schema_digest_v4,
)
from research_core.evidence_store import EvidenceCAS, EvidenceItemRevision  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_evidence import (  # noqa: E402
    RawCaptureArtifactInput,
    commit_raw_capture,
    prepare_raw_capture,
)
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_schema import (  # noqa: E402
    IdentityKind,
    TypedWorkspaceId,
    open_snapshot_connection,
)
from research_core.workspace_store import (  # noqa: E402
    CommandConflictError,
    StaleCommandError,
    WorkspaceIntegrityError,
    WorkspaceStore,
)


def _direct_genesis_fixture() -> dict[str, object]:
    seed = json.loads(
        (
            REPO_ROOT
            / "contracts"
            / "rh_autonomous_mission_seed.v1.json"
        ).read_text(encoding="utf-8")
    )
    seed["project_id"] = "project.rh"
    seed["authority"]["project_id"] = "project.rh"
    mission = seed["mission"]
    mission["project_id"] = "project.rh"
    mission["mission_id"] = "mission.1"
    mission["strategy_ids"] = ["strategy.theta.1"]
    branch = seed["opening_branch"]
    branch["project_id"] = "project.rh"
    branch["mission_id"] = "mission.1"
    branch["branch_id"] = "branch.theta"
    strategy = seed["strategy"]
    strategy["project_id"] = "project.rh"
    strategy["mission_id"] = "mission.1"
    strategy["strategy_id"] = "strategy.theta.1"
    branch_digest = hashlib.sha256(canonical_json_bytes(branch)).hexdigest()

    def rebind_references(value: object) -> None:
        if isinstance(value, dict):
            if set(value) == {
                "kind",
                "identity",
                "revision",
                "payload_sha256",
            }:
                value.update(
                    {
                        "kind": "branch",
                        "identity": "branch.theta",
                        "revision": 1,
                        "payload_sha256": branch_digest,
                    }
                )
                return
            for child in value.values():
                rebind_references(child)
        elif isinstance(value, list):
            for child in value:
                rebind_references(child)

    rebind_references(strategy)
    return seed


class CandidateA1TriageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        self.assertTrue(loaded.ok, loaded.failure)
        assert loaded.value is not None
        self.store = WorkspaceStore.initialize_direct_mission_workspace(
            WorkspacePaths.from_root(Path(self.temporary.name) / "workspace"),
            project_id="project.rh",
            canonical_snapshot=loaded.value,
            actor="candidate-a1-triage-store-test",
        )
        metadata = self.store.read_metadata()
        self.authority_digest = str(metadata["canonical_authority_digest"])
        self.lease = self.store.claim_writer(
            owner="candidate-a1-triage-store-test",
            creation_basis="focused-candidate-a1-triage-store-test",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.store.initialize_direct_mission_genesis(
            _direct_genesis_fixture(),
            mission_id="mission.1",
            canonical_snapshot=loaded.value,
            lease=self.lease,
            command_id="candidate-a1-triage.direct-genesis",
            actor="candidate-a1-triage-store-test",
        )
        mission = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        )
        self.assertIsNotNone(mission)
        assert mission is not None
        self.executive_epoch_id = "epoch.candidate-a1-triage.1"
        self.root_thread_id = "thread:root.1"
        authorized = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id=self.executive_epoch_id,
            mission_root=OwnerRevisionRef(
                "mission",
                "mission.1",
                mission.reference.revision,
                mission.payload_digest,
            ),
            predecessor_checkpoint=None,
        )
        metadata = self.store.read_metadata()
        self.store.authorize_executive_epoch(
            mission_id="mission.1",
            executive_epoch_id=self.executive_epoch_id,
            event=authorized,
            lease=self.lease,
            command_id=f"{self.executive_epoch_id}.authorize",
            actor="candidate-a1-triage-store-test",
            expected_canonical_authority_digest=self.authority_digest,
            expected_mission_host_store_cut={
                "project_commit": int(metadata["current_project_commit"]),
                "current_root_digest": str(metadata["current_root_digest"]),
                "transition_head_digest": metadata["transition_head_digest"],
                "canonical_authority_digest": str(
                    metadata["canonical_authority_digest"]
                ),
            },
        )
        bound = prepare_direct_executive_epoch_bound_event(
            executive_epoch_id=self.executive_epoch_id,
            mission_id="mission.1",
            goal_thread_id=self.root_thread_id,
            workspace_root=str(self.store.paths.root),
        )
        self.store.bind_executive_epoch(
            mission_id="mission.1",
            executive_epoch_id=self.executive_epoch_id,
            event=bound,
            expected_event_ordinal=1,
            expected_event_digest=authorized.digest_sha256,
            lease=self.lease,
            command_id=f"{self.executive_epoch_id}.bind",
            actor="candidate-a1-triage-store-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.executive_epoch_authority_sha256 = (
            reissue_direct_executive_epoch_authority(
                event_readbacks=self.store.read_active_executive_epoch("mission.1"),
                project_id="project.rh",
                root_identity=self.store.paths.root_identity,
                canonical_authority_digest=self.authority_digest,
            ).authority_sha256
        )
        self._commit_review_context()

    def _commit_review_context(self) -> None:
        self.store.commit_context_revision(
            executive_epoch_id=self.executive_epoch_id,
            context_id="a1-review",
            payload={
                "mission_id": "mission.1",
                "context_id": "context:a1-review",
                "purpose": "Independently review one frozen Candidate A1.",
            },
            expected_head_revision=None,
            expected_head_payload_digest=None,
            lease=self.lease,
            command_id="context.a1-review.create",
            actor="candidate-a1-triage-store-test",
            expected_canonical_authority_digest=self.authority_digest,
        )

    def _full_rh_candidate_payload(
        self,
        candidate_id: str,
        *,
        supporting_refs: tuple[EvidenceRevisionRef, ...] = (),
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 4,
            "kind": "candidate_revision",
            "contract_schema_sha256": candidate_schema_digest_v4(REPO_ROOT),
            "candidate_id": candidate_id,
            "mission_id": "mission.1",
            "proposal_kind": "mathematical_statement",
            "exact_statement": "This Candidate supplies a complete proof of RH.",
            "scope_and_reach": "complete unconditional proof of RH",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            "standing": {
                "status": "open",
                "basis": "unverified complete-target Candidate",
            },
            "provenance": {
                "kind": "native_executive_epoch",
                "executive_epoch_id": self.executive_epoch_id,
                "executive_epoch_authority_sha256": (
                    self.executive_epoch_authority_sha256
                ),
            },
            "authority_class": "candidate_only",
            "canonical_effect": "none",
        }
        if supporting_refs:
            payload["supporting_refs"] = [
                {
                    "kind": "evidence",
                    "id": item.evidence_id,
                    "revision": item.revision,
                    "digest_sha256": item.digest_sha256,
                }
                for item in supporting_refs
            ]
        return payload

    def _commit_full_rh_candidate(
        self,
        candidate_id: str,
        *,
        supporting_refs: tuple[EvidenceRevisionRef, ...] = (),
    ) -> dict[str, object]:
        payload = self._full_rh_candidate_payload(
            candidate_id,
            supporting_refs=supporting_refs,
        )
        requirement = candidate_a1_requirement(payload)
        self.assertIsNotNone(requirement)
        self.store.commit_candidate_revision(
            executive_epoch_id=self.executive_epoch_id,
            mission_id="mission.1",
            candidate_id=candidate_id,
            payload=payload,
            expected_head_revision=None,
            expected_head_payload_digest=None,
            a1_requirement=requirement,
            lease=self.lease,
            command_id=f"{candidate_id}.create",
            actor="candidate-a1-triage-store-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        stored = dict(
            self.store.read_candidate_revision(
                mission_id="mission.1",
                candidate_id=candidate_id,
            )
        )
        return stored

    def _capture_output(
        self,
        *,
        observation_id: str,
        assignment_id: str,
        provenance: dict[str, object],
        content: bytes,
    ) -> str:
        record = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=self.executive_epoch_id,
            capture_kind="output",
            observation_id=observation_id,
            assignment_id=assignment_id,
            provenance=provenance,
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactInput(
                    role="review_material",
                    logical_name=f"{observation_id}.json",
                    content_bytes=content,
                    media_type="application/json",
                    encoding="utf-8",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=EvidenceCAS(self.store.paths),
            record=record,
            lease=self.lease,
            actor="candidate-a1-triage-store-test",
            command_id=f"capture.{observation_id}",
        )
        return record.capture_id

    def _commit_supporting_evidence(
        self,
        suffix: str,
        *,
        subtype: str = "evidence_meaning",
    ) -> EvidenceRevisionRef:
        capture_id = self._capture_output(
            observation_id=f"support.{suffix}",
            assignment_id=f"thread:support.{suffix}",
            provenance={"kind": "focused_supporting_evidence"},
            content=b'{"defect":"the contour crosses an uncancelled pole"}',
        )
        evidence_id = f"evidence.support.{suffix}"
        evidence = EvidenceItemRevision(
            evidence_id=evidence_id,
            revision=1,
            subtype=subtype,
            subject={
                "mission_id": "mission.1",
                "finding": "The claimed contour shift crosses an uncancelled pole.",
            },
            exact_scope="the exact contour step in the frozen Candidate",
            rigor="independent_reconstruction",
            limitations=("limited to the cited contour step",),
            non_inferences=("does not establish or disprove RH",),
            security_classification="workspace_internal",
            retention="mission",
            blob_roles=(),
            availability_state="verified_available",
        )
        self.store.commit_evidence_meaning_revision(
            executive_epoch_id=self.executive_epoch_id,
            record=evidence,
            sources=(
                {
                    "source_ordinal": 0,
                    "capture_id": capture_id,
                    "artifact_ordinal": 0,
                    "exact_scope": {"kind": "independent_contour_check"},
                },
            ),
            expected_head_revision=None,
            expected_head_payload_digest=None,
            lease=self.lease,
            command_id=f"{evidence_id}.create",
            actor="candidate-a1-triage-store-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        stored = self.store.read_evidence_meaning_revision(evidence_id)
        return EvidenceRevisionRef(
            mission_id="mission.1",
            evidence_id=evidence_id,
            revision=1,
            digest_sha256=str(stored["payload_digest"]),
        )

    def _reviewer(self, suffix: str) -> CandidateA1ReviewerProvenance:
        context = self.store.read_context_revision("a1-review")
        return CandidateA1ReviewerProvenance(
            reviewer_identity=f"worker.independent-reviewer.{suffix}",
            reviewer_thread_id=f"thread:reviewer.{suffix}",
            root_thread_id=self.root_thread_id,
            parent_thread_id=self.root_thread_id,
            assignment_id=f"assignment.a1-review.{suffix}",
            context_id="context:a1-review",
            context_revision=int(context["revision"]),
            context_digest_sha256=str(context["payload_digest"]),
            grant_id=f"grant.a1-review.{suffix}",
            grant_digest_sha256=hashlib.sha256(
                f"grant.a1-review.{suffix}".encode()
            ).hexdigest(),
        )

    def _triage_source(
        self,
        *,
        record: CandidateA1TriageRecord,
        suffix: str,
    ) -> dict[str, object]:
        reviewer = record.reviewer
        candidate = record.candidate_ref
        capture_id = self._capture_output(
            observation_id=f"triage.{suffix}",
            assignment_id=reviewer.reviewer_thread_id,
            provenance={
                "kind": "candidate_a1_triage_submission",
                "bridge": "native_host_observation",
                "material_kind": "output",
                "root_thread_id": reviewer.root_thread_id,
                "parent_thread_id": reviewer.parent_thread_id,
                "child_thread_id": reviewer.reviewer_thread_id,
                "grant_id": reviewer.grant_id,
            },
            content=canonical_json_bytes(record.subject_payload()),
        )
        return {
            "capture_id": capture_id,
            "artifact_ordinal": 0,
            "exact_scope": {
                "kind": "candidate_a1_triage_submission",
                "candidate_id": candidate.candidate_id,
                "candidate_revision": candidate.revision,
                "candidate_digest": candidate.digest_sha256,
                "grant_id": reviewer.grant_id,
            },
        }

    def _candidate_ref(self, stored: dict[str, object]) -> CandidateA1Ref:
        return CandidateA1Ref(
            mission_id="mission.1",
            candidate_id=str(stored["candidate_id"]),
            revision=int(stored["revision"]),
            digest_sha256=str(stored["payload_digest"]),
        )

    def _historical_a1_binding(
        self,
        store: WorkspaceStore,
        candidate_ref: CandidateA1Ref,
    ):
        matches = tuple(
            item
            for item in store.list_mission_candidate_a1_bindings(
                candidate_ref.mission_id
            )
            if (
                item.candidate_id == candidate_ref.candidate_id
                and item.candidate_revision == candidate_ref.revision
                and item.candidate_digest == candidate_ref.digest_sha256
            )
        )
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_current_projection_authenticates_open_membership_and_nonmembership(
        self,
    ) -> None:
        with patch.object(
            store_module,
            "_derive_current_dependency_entries",
            side_effect=AssertionError("routine A1 lookup ran full rederivation"),
        ):
            self.assertEqual(
                self.store._list_open_mission_candidate_a1_bindings("mission.1"),
                (),
            )

        stored = self._commit_full_rh_candidate("candidate.projected")
        statements: list[str] = []
        connect = sqlite3.connect

        def instrumented_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with (
            patch.object(store_module.sqlite3, "connect", instrumented_connect),
            patch.object(
                store_module,
                "_derive_current_dependency_entries",
                side_effect=AssertionError("routine A1 lookup ran full rederivation"),
            ),
        ):
            projected = self.store._list_open_mission_candidate_a1_bindings(
                "mission.1"
            )
        self.assertEqual(
            tuple((item.candidate_id, item.candidate_revision) for item in projected),
            (("candidate.projected", int(stored["revision"])),),
        )
        current_dependency_lookup = store_module._current_dependency_lookup

        def omit_exact_a1_route(connection, **kwargs):
            if kwargs.get("family") == "candidate_a1":
                return None
            return current_dependency_lookup(connection, **kwargs)

        with patch.object(
            store_module,
            "_current_dependency_lookup",
            side_effect=omit_exact_a1_route,
        ):
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "exact authenticated projection",
            ):
                self.store.read_candidate_a1_binding(
                    candidate_id="candidate.projected",
                    candidate_revision=int(stored["revision"]),
                    candidate_digest=str(stored["payload_digest"]),
                )
        candidate_reads = tuple(
            statement
            for statement in statements
            if "FROM candidate_revision" in statement
        )
        self.assertTrue(candidate_reads)
        self.assertTrue(
            all(
                "WHERE object_id =" in statement and "revision =" in statement
                for statement in candidate_reads
            ),
            candidate_reads,
        )
        node_reads = tuple(
            statement
            for statement in statements
            if "FROM current_dependency_node" in statement
        )
        self.assertTrue(node_reads)
        locator_reads = tuple(
            statement
            for statement in node_reads
            if "WHERE family = 'candidate_a1'" in statement
        )
        self.assertEqual(len(locator_reads), 1)
        self.assertTrue(
            all(
                "WHERE scope_key =" in statement
                for statement in node_reads
                if statement not in locator_reads
            ),
            node_reads,
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            locator_plan = " ".join(
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT scope_json "
                    "FROM current_dependency_node "
                    "WHERE family = 'candidate_a1' "
                    "AND json_extract(scope_json, '$.mission_id') = ? "
                    "ORDER BY scope_key",
                    ("mission.1",),
                )
            )
        self.assertIn(
            "USING INDEX current_dependency_candidate_a1_mission",
            locator_plan,
        )

        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            changed = connection.execute(
                "UPDATE current_dependency_node SET state_json = ? "
                "WHERE family = 'candidate_a1'",
                ('{}',),
            ).rowcount
            self.assertEqual(changed, 1)
            connection.commit()
        with self.assertRaises(WorkspaceIntegrityError):
            self.store._list_open_mission_candidate_a1_bindings("mission.1")

    def test_current_projection_binds_the_exact_alert_row(self) -> None:
        stored = self._commit_full_rh_candidate("candidate.projected-alert-row")
        binding = self.store.read_candidate_a1_binding(
            candidate_id=str(stored["candidate_id"]),
            candidate_revision=int(stored["revision"]),
            candidate_digest=str(stored["payload_digest"]),
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM alert_event WHERE alert_id = ?",
                (binding.alert_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            spec = store_module._AUXILIARY_SPECS[
                store_module.AuxiliaryTable.ALERT_EVENT
            ]
            values = {column: row[column] for column in spec.columns}
            values["created_at"] = "2030-01-01T00:00:00Z"
            connection.execute(
                "UPDATE alert_event SET created_at = ? WHERE alert_id = ?",
                (values["created_at"], binding.alert_id),
            )
            connection.commit()

        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "current auxiliary row digest mismatch|exact authenticated projection",
        ):
            self.store.read_candidate_a1_binding(
                candidate_id=str(stored["candidate_id"]),
                candidate_revision=int(stored["revision"]),
                candidate_digest=str(stored["payload_digest"]),
            )

    def test_ordinary_evidence_writer_rejects_reserved_subtype_and_id(self) -> None:
        ordinary_fields = {
            "revision": 1,
            "subject": {"mission_id": "mission.1"},
            "exact_scope": "one ordinary Evidence meaning",
            "rigor": "direct",
            "limitations": (),
            "non_inferences": ("does not establish RH",),
            "security_classification": "workspace_internal",
            "retention": "mission",
            "blob_roles": (),
            "availability_state": "verified_available",
        }
        reserved_subtype = EvidenceItemRevision(
            evidence_id="evidence.ordinary-id",
            subtype=CANDIDATE_A1_TRIAGE_EVIDENCE_SUBTYPE,
            **ordinary_fields,
        )
        with self.assertRaisesRegex(ValueError, "dedicated role-separated route"):
            self.store.commit_evidence_meaning_revision(
                executive_epoch_id=self.executive_epoch_id,
                record=reserved_subtype,
                sources=(),
                expected_head_revision=None,
                expected_head_payload_digest=None,
                lease=self.lease,
                command_id="reserved-subtype.rejected",
                actor="candidate-a1-triage-store-test",
                expected_canonical_authority_digest=self.authority_digest,
            )

        purported_subtype = EvidenceItemRevision(
            evidence_id="evidence.ordinary-purported-subtype",
            subtype=store_module._PURPORTED_COMPLETE_EVIDENCE_SUBTYPE,
            **ordinary_fields,
        )
        with self.assertRaisesRegex(ValueError, "dedicated role-separated route"):
            self.store.commit_evidence_meaning_revision(
                executive_epoch_id=self.executive_epoch_id,
                record=purported_subtype,
                sources=(),
                expected_head_revision=None,
                expected_head_payload_digest=None,
                lease=self.lease,
                command_id="purported-subtype.rejected",
                actor="candidate-a1-triage-store-test",
                expected_canonical_authority_digest=self.authority_digest,
            )

        purported_id = EvidenceItemRevision(
            evidence_id=(
                f"{store_module._PURPORTED_COMPLETE_EVIDENCE_PREFIX}{'b' * 64}"
            ),
            subtype="evidence_meaning",
            **ordinary_fields,
        )
        with self.assertRaisesRegex(ValueError, "dedicated role-separated route"):
            self.store.commit_evidence_meaning_revision(
                executive_epoch_id=self.executive_epoch_id,
                record=purported_id,
                sources=(),
                expected_head_revision=None,
                expected_head_payload_digest=None,
                lease=self.lease,
                command_id="purported-id.rejected",
                actor="candidate-a1-triage-store-test",
                expected_canonical_authority_digest=self.authority_digest,
            )

        reserved_id = EvidenceItemRevision(
            evidence_id=f"{CANDIDATE_A1_TRIAGE_EVIDENCE_ID_PREFIX}{'a' * 64}",
            subtype="evidence_meaning",
            **ordinary_fields,
        )
        with self.assertRaisesRegex(ValueError, "dedicated role-separated route"):
            self.store.commit_evidence_meaning_revision(
                executive_epoch_id=self.executive_epoch_id,
                record=reserved_id,
                sources=(),
                expected_head_revision=None,
                expected_head_payload_digest=None,
                lease=self.lease,
                command_id="reserved-id.rejected",
                actor="candidate-a1-triage-store-test",
                expected_canonical_authority_digest=self.authority_digest,
            )

    def test_triage_write_rejects_missing_selected_cited_evidence_bytes_without_commit(
        self,
    ) -> None:
        support = self._commit_supporting_evidence("missing-cited-bytes")
        stored = self._commit_full_rh_candidate(
            "candidate.missing-cited-bytes",
            supporting_refs=(support,),
        )
        record = prepare_candidate_a1_triage(
            candidate_ref=self._candidate_ref(stored),
            disposition=CANDIDATE_A1_TRIAGE_ADMISSION_READY,
            reviewer=self._reviewer("missing-cited-bytes"),
            review_finding="The exact cited basis supports the frozen Candidate.",
            cited_basis=(support,),
            no_remaining_material_objection=True,
            limitations=("Admission remains a separate owner decision.",),
        )
        source = self._triage_source(
            record=record,
            suffix="missing-cited-bytes",
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            row = connection.execute(
                "SELECT artifact.blob_sha256 "
                "FROM evidence_capture_source AS source "
                "JOIN raw_capture_artifact AS artifact "
                "ON artifact.capture_id = source.capture_id "
                "AND artifact.ordinal = source.artifact_ordinal "
                "WHERE source.evidence_id = ? AND source.evidence_revision = ?",
                (support.evidence_id, support.revision),
            ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        cited_blob_path = EvidenceCAS(self.store.paths).path_for_digest(str(row[0]))
        cited_blob_path.chmod(0o600)
        cited_blob_path.unlink()
        before = dict(self.store.read_metadata())

        with self.assertRaisesRegex(
            StaleCommandError,
            "Blob bytes are unavailable or corrupt",
        ):
            self.store.commit_candidate_a1_triage_revision(
                executive_epoch_id=self.executive_epoch_id,
                record=record,
                source=source,
                lease=self.lease,
                command_id="candidate.missing-cited-bytes.triage",
                actor="worker.independent-reviewer.missing-cited-bytes",
                expected_canonical_authority_digest=self.authority_digest,
            )

        self.assertEqual(before, dict(self.store.read_metadata()))
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM evidence_item_revision WHERE evidence_id = ?",
                    (record.evidence.evidence_id,),
                ).fetchone()
            )

    def test_invalidated_is_replayable_resolves_exact_a1_and_survives_restart(
        self,
    ) -> None:
        stored = self._commit_full_rh_candidate("candidate.invalidated")
        support = self._commit_supporting_evidence("invalidated")
        alternate_subtype = self._commit_supporting_evidence(
            "alternate-subtype",
            subtype="synthesis_derivation",
        )
        reviewer = self._reviewer("invalidated")
        record = prepare_candidate_a1_triage(
            candidate_ref=self._candidate_ref(stored),
            disposition=CANDIDATE_A1_TRIAGE_INVALIDATED,
            reviewer=reviewer,
            review_finding="Independent reconstruction found one exact defeating defect.",
            concrete_defects=(
                CandidateA1ConcreteDefect(
                    exact_defect="The contour shift crosses an uncancelled pole.",
                    affected_scope="the equality implying the complete RH conclusion",
                    sufficiency_basis=(
                        "The omitted residue defeats the equality required by the "
                        "complete-target implication."
                    ),
                ),
            ),
            no_remaining_material_objection=False,
            limitations=("review is limited to this exact Candidate revision",),
        )
        source = self._triage_source(record=record, suffix="invalidated")
        current_dependency_lookup = store_module._current_dependency_lookup
        projected_scope = {
            "mission_id": record.candidate_ref.mission_id,
            "candidate_id": record.candidate_ref.candidate_id,
            "candidate_revision": record.candidate_ref.revision,
            "candidate_digest": record.candidate_ref.digest_sha256,
        }
        with closing(open_snapshot_connection(self.store.paths.database)) as connection:
            stale_projected_descriptor = current_dependency_lookup(
                connection,
                project_id="project.rh",
                family="candidate_a1",
                scope=projected_scope,
            )
        self.assertIsNotNone(stale_projected_descriptor)

        def omit_exact_a1_route(connection, **kwargs):
            if kwargs.get("family") == "candidate_a1":
                return None
            return current_dependency_lookup(connection, **kwargs)

        with patch.object(
            store_module,
            "_current_dependency_lookup",
            side_effect=omit_exact_a1_route,
        ):
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "exact authenticated projection",
            ):
                self.store.commit_candidate_a1_triage_revision(
                    executive_epoch_id=self.executive_epoch_id,
                    record=record,
                    source=source,
                    lease=self.lease,
                    command_id="candidate.invalidated.missing-projection",
                    actor="worker.independent-reviewer.invalidated",
                    expected_canonical_authority_digest=self.authority_digest,
                )
        first = self.store.commit_candidate_a1_triage_revision(
            executive_epoch_id=self.executive_epoch_id,
            record=record,
            source=source,
            lease=self.lease,
            command_id="candidate.invalidated.triage",
            actor="worker.independent-reviewer.invalidated",
            expected_canonical_authority_digest=self.authority_digest,
        )
        replay = self.store.commit_candidate_a1_triage_revision(
            executive_epoch_id=self.executive_epoch_id,
            record=record,
            source=source,
            lease=self.lease,
            command_id="candidate.invalidated.triage",
            actor="worker.independent-reviewer.invalidated",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)

        binding = self._historical_a1_binding(self.store, record.candidate_ref)
        self.assertEqual(binding.hold_lifecycle, "resolved")
        self.assertEqual(binding.triage_disposition, CANDIDATE_A1_TRIAGE_INVALIDATED)
        self.assertEqual(binding.triage_evidence_id, record.evidence.evidence_id)
        self.assertEqual(binding.canonical_effect, "none")
        self.assertEqual(binding.mathematical_effect, "none")
        preserved = self.store.read_evidence_meaning_revision(
            binding.evidence_id,
            binding.evidence_revision,
        )
        self.assertEqual(
            preserved["record"]["subtype"],
            store_module._PURPORTED_COMPLETE_EVIDENCE_SUBTYPE,
        )
        self.assertNotIn("mission_id", preserved["record"]["subject"])
        self.assertEqual(
            preserved["payload_digest"],
            binding.evidence_payload_digest,
        )
        self.assertFalse(
            any(
                item.candidate_id == record.candidate_ref.candidate_id
                for item in self.store._list_open_mission_candidate_a1_bindings(
                    "mission.1"
                )
            )
        )

        def retain_terminal_a1_route(connection, **kwargs):
            if (
                kwargs.get("family") == "candidate_a1"
                and kwargs.get("scope") == projected_scope
            ):
                return stale_projected_descriptor
            return current_dependency_lookup(connection, **kwargs)

        with patch.object(
            store_module,
            "_current_dependency_lookup",
            side_effect=retain_terminal_a1_route,
        ):
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "terminal Candidate A1 route remains",
            ):
                self.store._read_candidate_a1_binding(
                    candidate_id=record.candidate_ref.candidate_id,
                    candidate_revision=record.candidate_ref.revision,
                    candidate_digest=record.candidate_ref.digest_sha256,
                    require_current=False,
                    require_open=False,
                )

        ordinary_heads = self.store.list_mission_evidence_meaning_heads("mission.1")
        self.assertCountEqual(
            tuple(item["record"]["evidence_id"] for item in ordinary_heads),
            (support.evidence_id, alternate_subtype.evidence_id),
        )
        self.assertNotIn(
            record.evidence.evidence_id,
            tuple(item["record"]["evidence_id"] for item in ordinary_heads),
        )

        changed = prepare_candidate_a1_triage(
            candidate_ref=record.candidate_ref,
            disposition=CANDIDATE_A1_TRIAGE_ADMISSION_READY,
            reviewer=reviewer,
            review_finding="A changed second outcome must not replace revision one.",
            cited_basis=(support,),
            no_remaining_material_objection=True,
            limitations=("Admission remains separate",),
        )
        with self.assertRaises(
            (CommandConflictError, StaleCommandError, WorkspaceIntegrityError)
        ):
            self.store.commit_candidate_a1_triage_revision(
                executive_epoch_id=self.executive_epoch_id,
                record=changed,
                source=source,
                lease=self.lease,
                command_id="candidate.invalidated.changed-outcome",
                actor="worker.independent-reviewer.invalidated",
                expected_canonical_authority_digest=self.authority_digest,
            )

        reopened = WorkspaceStore.open(
            self.store.paths,
            expected_project_id="project.rh",
        )
        reconstructed = self._historical_a1_binding(reopened, record.candidate_ref)
        reconstructed.verify_issued()
        self.assertEqual(reconstructed.hold_lifecycle, "resolved")
        self.assertEqual(
            reconstructed.triage_disposition,
            CANDIDATE_A1_TRIAGE_INVALIDATED,
        )
        reopened.verify_integrity()
        self.assertEqual(
            reopened.read_candidate_a1_triage_revision(
                mission_id="mission.1",
                candidate_id=record.candidate_ref.candidate_id,
                candidate_revision=record.candidate_ref.revision,
                candidate_digest=record.candidate_ref.digest_sha256,
            ),
            record,
        )

    def test_admission_ready_stays_open_after_author_withdrawal(self) -> None:
        candidate_id = "candidate.admission-ready"
        support = self._commit_supporting_evidence("admission-ready")
        stored = self._commit_full_rh_candidate(
            candidate_id,
            supporting_refs=(support,),
        )
        reviewer = self._reviewer("admission-ready")
        record = prepare_candidate_a1_triage(
            candidate_ref=self._candidate_ref(stored),
            disposition=CANDIDATE_A1_TRIAGE_ADMISSION_READY,
            reviewer=reviewer,
            review_finding=(
                "Independent reconstruction leaves no material objection to the "
                "exact frozen complete claim."
            ),
            cited_basis=(support,),
            no_remaining_material_objection=True,
            limitations=("Admission authority remains separate",),
        )
        source = self._triage_source(record=record, suffix="admission-ready")
        self.store.commit_candidate_a1_triage_revision(
            executive_epoch_id=self.executive_epoch_id,
            record=record,
            source=source,
            lease=self.lease,
            command_id="candidate.admission-ready.triage",
            actor="worker.independent-reviewer.admission-ready",
            expected_canonical_authority_digest=self.authority_digest,
        )
        before_withdrawal = self.store.read_candidate_a1_binding(
            candidate_id=candidate_id,
            candidate_revision=record.candidate_ref.revision,
            candidate_digest=record.candidate_ref.digest_sha256,
        )
        self.assertEqual(before_withdrawal.hold_lifecycle, "open")
        self.assertEqual(
            before_withdrawal.triage_disposition,
            CANDIDATE_A1_TRIAGE_ADMISSION_READY,
        )
        self.assertEqual(
            tuple(
                item.candidate_id
                for item in self.store._list_open_mission_candidate_a1_bindings(
                    "mission.1"
                )
            ),
            (candidate_id,),
        )

        withdrawn = deep_thaw(stored["payload"])
        del withdrawn["complete_target_claim"]
        withdrawn["standing"] = {
            "status": "withdrawn",
            "basis": "the author retracts the complete-target assertion",
        }
        self.store.commit_candidate_revision(
            executive_epoch_id=self.executive_epoch_id,
            mission_id="mission.1",
            candidate_id=candidate_id,
            payload=withdrawn,
            expected_head_revision=int(stored["revision"]),
            expected_head_payload_digest=str(stored["payload_digest"]),
            a1_requirement=None,
            lease=self.lease,
            command_id=f"{candidate_id}.withdraw",
            actor="candidate-a1-triage-store-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        current = self.store.read_candidate_revision(
            mission_id="mission.1",
            candidate_id=candidate_id,
        )
        self.assertEqual(current["revision"], int(stored["revision"]) + 1)
        self.assertNotIn("complete_target_claim", current["payload"])

        retained = self._historical_a1_binding(self.store, record.candidate_ref)
        self.assertEqual(retained.hold_lifecycle, "open")
        self.assertEqual(
            retained.triage_disposition,
            CANDIDATE_A1_TRIAGE_ADMISSION_READY,
        )
        self.assertEqual(
            [
                (item.candidate_id, item.candidate_revision, item.hold_lifecycle)
                for item in self.store._list_open_mission_candidate_a1_bindings(
                    "mission.1"
                )
                if item.candidate_id == candidate_id
            ],
            [(candidate_id, record.candidate_ref.revision, "open")],
        )


if __name__ == "__main__":
    unittest.main()
