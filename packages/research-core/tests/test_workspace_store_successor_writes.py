from __future__ import annotations

import sys
import tempfile
import unittest
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core.evidence_store import EvidenceCAS  # noqa: E402
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    ResolvedOwnerRevision,
    prepare_direct_continuation_checkpoint,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    prepare_direct_executive_epoch_checkpointed_event,
    prepare_direct_executive_epoch_failed_before_checkpoint_event,
)
from research_core.workspace_store import (  # noqa: E402
    CommandConflictError,
    StaleCommandError,
    WorkspaceIntegrityError,
    WorkspaceStore,
    canonical_raw_capture_id,
)
from research_core.workspace_schema import (  # noqa: E402
    IdentityKind,
    TypedWorkspaceId,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402


def _direct_genesis_fixture(
    *,
    project_id: str = "project.rh",
    mission_id: str = "mission.1",
    branch_id: str = "branch.theta",
    strategy_id: str = "strategy.theta.1",
) -> dict[str, object]:
    seed = json.loads(
        (
            REPO_ROOT
            / "contracts"
            / "rh_autonomous_mission_seed.v1.json"
        ).read_text(encoding="utf-8")
    )
    seed["project_id"] = project_id
    seed["authority"]["project_id"] = project_id
    mission = seed["mission"]
    mission["project_id"] = project_id
    mission["mission_id"] = mission_id
    mission["strategy_ids"] = [strategy_id]
    branch = seed["opening_branch"]
    branch["project_id"] = project_id
    branch["mission_id"] = mission_id
    branch["branch_id"] = branch_id
    strategy = seed["strategy"]
    strategy["project_id"] = project_id
    strategy["mission_id"] = mission_id
    strategy["strategy_id"] = strategy_id
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
                        "identity": branch_id,
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


class SuccessorWorkspaceWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        self.assertTrue(loaded.ok, loaded.failure)
        assert loaded.value is not None
        self.canonical_snapshot = loaded.value
        self.store = WorkspaceStore.initialize_direct_mission_workspace(
            WorkspacePaths.from_root(Path(self.temporary.name) / "workspace"),
            project_id="project.rh",
            canonical_snapshot=self.canonical_snapshot,
            actor="successor-writer-test",
        )
        self.mission_id = "mission.1"
        self.executive_epoch_id = "epoch.successor-writer.1"
        metadata = self.store.read_metadata()
        self.authority_digest = str(metadata["canonical_authority_digest"])
        self.lease = self.store.claim_writer(
            owner="successor-writer-test",
            creation_basis="focused-root5-writer-test",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.store.initialize_direct_mission_genesis(
            _direct_genesis_fixture(mission_id=self.mission_id),
            mission_id=self.mission_id,
            canonical_snapshot=self.canonical_snapshot,
            lease=self.lease,
            command_id="successor-writer.direct-genesis",
            actor="successor-writer-test",
        )
        self._open_direct_epoch(self.executive_epoch_id)

    def _owner_ref(self, kind: IdentityKind, identity: str) -> OwnerRevisionRef:
        stored = self.store.get_head(TypedWorkspaceId(kind, identity))
        self.assertIsNotNone(stored)
        assert stored is not None
        return OwnerRevisionRef(
            kind.value,
            identity,
            stored.reference.revision,
            stored.payload_digest,
        )

    def _mission_host_store_cut(self) -> dict[str, object]:
        metadata = self.store.read_metadata()
        return {
            "project_commit": int(metadata["current_project_commit"]),
            "current_root_digest": str(metadata["current_root_digest"]),
            "transition_head_digest": metadata["transition_head_digest"],
            "canonical_authority_digest": str(
                metadata["canonical_authority_digest"]
            ),
        }

    def _open_direct_epoch(self, executive_epoch_id: str) -> None:
        mission_root = self._owner_ref(IdentityKind.MISSION, self.mission_id)
        authorized = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id=executive_epoch_id,
            mission_root=mission_root,
            predecessor_checkpoint=(
                None
                if self.store.read_continuation_checkpoint(
                    latest_mission_id=self.mission_id
                )
                is None
                else {
                    "checkpoint_id": self.store.read_continuation_checkpoint(
                        latest_mission_id=self.mission_id
                    )["checkpoint_id"],
                    "payload_sha256": self.store.read_continuation_checkpoint(
                        latest_mission_id=self.mission_id
                    )["payload_digest"],
                }
            ),
        )
        self.store.authorize_executive_epoch(
            mission_id=self.mission_id,
            executive_epoch_id=executive_epoch_id,
            event=authorized,
            lease=self.lease,
            command_id=f"{executive_epoch_id}.authorize",
            actor="successor-writer-test",
            expected_canonical_authority_digest=self.authority_digest,
            expected_mission_host_store_cut=self._mission_host_store_cut(),
        )
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=executive_epoch_id,
            mission_id=self.mission_id,
        )
        bound = prepare_direct_executive_epoch_bound_event(
            executive_epoch_id=executive_epoch_id,
            mission_id=self.mission_id,
            goal_thread_id="goal-thread:successor-writer",
            workspace_root=str(self.store.paths.root),
        )
        self.store.bind_executive_epoch(
            mission_id=self.mission_id,
            executive_epoch_id=executive_epoch_id,
            event=bound,
            expected_event_ordinal=int(chain[-1]["event_ordinal"]),
            expected_event_digest=str(chain[-1]["event_digest"]),
            lease=self.lease,
            command_id=f"{executive_epoch_id}.bind",
            actor="successor-writer-test",
            expected_canonical_authority_digest=self.authority_digest,
        )

    def _commit_raw_capture(self, *, suffix: str, epoch_id: str | None = None) -> str:
        cas = EvidenceCAS(self.store.paths)
        installed = cas.ingest_bytes(
            f"capture-{suffix}".encode(),
            original_name=f"{suffix}.txt",
            media_type="text/plain",
            encoding="utf-8",
        )
        observation_id = f"observation.{suffix}"
        capture_id = canonical_raw_capture_id(
            project_id=self.store.project_id,
            capture_kind="output",
            observation_id=observation_id,
        )
        self.store.commit_raw_capture(
            record={
                "capture_id": capture_id,
                "mission_id": self.mission_id,
                "executive_epoch_id": (
                    self.executive_epoch_id if epoch_id is None else epoch_id
                ),
                "capture_kind": "output",
                "observation_id": observation_id,
                "assignment_id": f"assignment.{suffix}",
                "provenance": {"kind": "focused_store_test"},
                "completion": {"lifecycle": "completed"},
            },
            artifacts=(
                {
                    "ordinal": 0,
                    "role": "result",
                    "logical_name": f"{suffix}.txt",
                    "blob_sha256": installed.record.sha256,
                },
            ),
            blobs=(replace(installed.record, availability_state="verified_available"),),
            lease=self.lease,
            command_id=f"capture.{suffix}",
            actor="successor-writer-test",
        )
        return capture_id

    def _commit_context(
        self,
        *,
        context_id: str,
        payload: dict[str, object],
        command_id: str,
        expected_revision: int | None,
        expected_digest: str | None,
        dependencies: dict[str, tuple[int, str]] | None = None,
    ):
        return self.store.commit_context_revision(
            executive_epoch_id=self.executive_epoch_id,
            context_id=context_id,
            payload={"mission_id": self.mission_id, **payload},
            expected_head_revision=expected_revision,
            expected_head_payload_digest=expected_digest,
            dependency_heads=dependencies,
            lease=self.lease,
            command_id=command_id,
            actor="successor-writer-test",
            expected_canonical_authority_digest=self.authority_digest,
        )

    def test_context_replay_noop_and_staleness_are_target_local(self) -> None:
        first_payload = {
            "purpose": "test one target-local Context owner",
            "decision_use": "choose between exact constructions",
        }
        first = self._commit_context(
            context_id="context.primary",
            payload=first_payload,
            command_id="context.primary.create",
            expected_revision=None,
            expected_digest=None,
        )
        replay = self._commit_context(
            context_id="context.primary",
            payload=first_payload,
            command_id="context.primary.create",
            expected_revision=None,
            expected_digest=None,
        )
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        first_head = self.store.read_context_revision("context.primary")
        self.assertEqual(
            tuple(
                item["context_id"]
                for item in self.store.list_mission_context_heads(self.mission_id)
            ),
            ("context.primary",),
        )
        self.assertEqual(
            self.store.list_mission_evidence_meaning_heads(self.mission_id),
            (),
        )

        with self.assertRaises(CommandConflictError):
            self._commit_context(
                context_id="context.primary",
                payload={**first_payload, "decision_use": "changed envelope"},
                command_id="context.primary.create",
                expected_revision=None,
                expected_digest=None,
            )

        metadata_before_noop = self.store.read_metadata()
        with self.store.snapshot_connection() as connection:
            journal_count_before = int(
                connection.execute("SELECT COUNT(*) FROM transition_journal").fetchone()[0]
            )
        semantic_noop = self._commit_context(
            context_id="context.primary",
            payload=first_payload,
            command_id="context.primary.same-semantic",
            expected_revision=int(first_head["revision"]),
            expected_digest=str(first_head["payload_digest"]),
        )
        metadata_after_noop = self.store.read_metadata()
        with self.store.snapshot_connection() as connection:
            journal_count_after = int(
                connection.execute("SELECT COUNT(*) FROM transition_journal").fetchone()[0]
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM command_result WHERE command_id = ?",
                    ("context.primary.same-semantic",),
                ).fetchone()
            )
        self.assertTrue(semantic_noop.result["semantic_noop"])
        self.assertEqual(
            metadata_after_noop["current_project_commit"],
            metadata_before_noop["current_project_commit"],
        )
        self.assertEqual(journal_count_after, journal_count_before)

        self._commit_context(
            context_id="context.dependency",
            payload={"purpose": "independent dependency owner"},
            command_id="context.dependency.create",
            expected_revision=None,
            expected_digest=None,
        )
        dependency_head = self.store.read_context_revision("context.dependency")

        updated_primary = self._commit_context(
            context_id="context.primary",
            payload={**first_payload, "known_omissions": ["one omitted branch"]},
            command_id="context.primary.update",
            expected_revision=int(first_head["revision"]),
            expected_digest=str(first_head["payload_digest"]),
        )
        self.assertFalse(updated_primary.replayed)
        primary_head = self.store.read_context_revision("context.primary")

        self._commit_context(
            context_id="context.dependency",
            payload={
                "purpose": "independent dependency owner",
                "material_delta": "new exact source",
            },
            command_id="context.dependency.update",
            expected_revision=int(dependency_head["revision"]),
            expected_digest=str(dependency_head["payload_digest"]),
        )
        with self.assertRaisesRegex(StaleCommandError, "dependency head"):
            self._commit_context(
                context_id="context.primary",
                payload={**first_payload, "known_omissions": []},
                command_id="context.primary.stale-dependency",
                expected_revision=int(primary_head["revision"]),
                expected_digest=str(primary_head["payload_digest"]),
                dependencies={
                    "context:context.dependency": (
                        int(dependency_head["revision"]),
                        str(dependency_head["payload_digest"]),
                    )
                },
            )

        report = self.store.verify_integrity()
        self.assertEqual(report.schema_version, 12)
        self.assertEqual(self.store.read_metadata()["root_digest_version"], 6)

    def test_direct_epoch_has_one_active_chain_and_exact_replay(self) -> None:
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=self.mission_id,
        )
        self.assertEqual(
            tuple(item["event_kind"] for item in chain),
            ("authorized", "bound"),
        )
        self.assertEqual(self.store.read_active_executive_epoch(self.mission_id), chain)
        self.assertEqual(
            self.store.read_executive_epoch_events_by_goal_thread(
                mission_id=self.mission_id,
                goal_thread_id="goal-thread:successor-writer",
            ),
            chain,
        )
        with self.assertRaisesRegex(TypeError, "issued event"):
            self.store.bind_executive_epoch(
                mission_id=self.mission_id,
                executive_epoch_id=self.executive_epoch_id,
                event=chain[-1]["event"],
                expected_event_ordinal=1,
                expected_event_digest=str(chain[0]["event_digest"]),
                lease=self.lease,
                command_id=f"{self.executive_epoch_id}.mapping-rejected",
                actor="successor-writer-test",
                expected_canonical_authority_digest=self.authority_digest,
            )
        replay = self.store.bind_executive_epoch(
            mission_id=self.mission_id,
            executive_epoch_id=self.executive_epoch_id,
            event=prepare_direct_executive_epoch_bound_event(
                executive_epoch_id=self.executive_epoch_id,
                mission_id=self.mission_id,
                goal_thread_id="goal-thread:successor-writer",
                workspace_root=str(self.store.paths.root),
            ),
            expected_event_ordinal=1,
            expected_event_digest=str(chain[0]["event_digest"]),
            lease=self.lease,
            command_id=f"{self.executive_epoch_id}.bind",
            actor="successor-writer-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        self.assertTrue(replay.replayed)

        other_id = "epoch.successor-writer.other"
        other = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id=other_id,
            mission_root=self._owner_ref(IdentityKind.MISSION, self.mission_id),
            predecessor_checkpoint=None,
        )
        with self.assertRaisesRegex(StaleCommandError, "nonterminal"):
            self.store.authorize_executive_epoch(
                mission_id=self.mission_id,
                executive_epoch_id=other_id,
                event=other,
                lease=self.lease,
                command_id=f"{other_id}.authorize",
                actor="successor-writer-test",
                expected_canonical_authority_digest=self.authority_digest,
                expected_mission_host_store_cut=self._mission_host_store_cut(),
            )
        self.assertEqual(
            self.store.read_executive_epoch_events(
                executive_epoch_id=other_id,
                mission_id=self.mission_id,
            ),
            (),
        )

        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute("PRAGMA user_version = 9")
            connection.execute(
                "UPDATE workspace_metadata SET schema_version = 9, "
                "root_digest_version = 5"
            )
            connection.commit()
        with self.assertRaises(WorkspaceIntegrityError):
            self.store.bind_executive_epoch(
                mission_id=self.mission_id,
                executive_epoch_id=self.executive_epoch_id,
                event=prepare_direct_executive_epoch_bound_event(
                    executive_epoch_id=self.executive_epoch_id,
                    mission_id=self.mission_id,
                    goal_thread_id="goal-thread:successor-writer",
                    workspace_root=str(self.store.paths.root),
                ),
                expected_event_ordinal=1,
                expected_event_digest=str(chain[0]["event_digest"]),
                lease=self.lease,
                command_id=f"{self.executive_epoch_id}.bind",
                actor="successor-writer-test",
                expected_canonical_authority_digest=self.authority_digest,
            )

    def test_goal_thread_identity_cannot_cross_executive_epochs(self) -> None:
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=self.mission_id,
        )
        failed = prepare_direct_executive_epoch_failed_before_checkpoint_event(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=self.mission_id,
            reconciliation={
                "stage": "goal_runtime",
                "failure_reason": "goal ended without a checkpoint",
            },
        )
        self.store.fail_executive_epoch_before_checkpoint(
            mission_id=self.mission_id,
            executive_epoch_id=self.executive_epoch_id,
            event=failed,
            expected_event_ordinal=int(chain[-1]["event_ordinal"]),
            expected_event_digest=str(chain[-1]["event_digest"]),
            lease=self.lease,
            command_id="epoch.successor-writer.1.fail-for-thread-test",
            actor="successor-writer-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        second_id = "epoch.successor-writer.reused-thread"
        with self.assertRaisesRegex(StaleCommandError, "Goal thread"):
            self._open_direct_epoch(second_id)
        second_chain = self.store.read_executive_epoch_events(
            executive_epoch_id=second_id,
            mission_id=self.mission_id,
        )
        self.assertEqual(
            tuple(item["event_kind"] for item in second_chain),
            ("authorized",),
        )

    def test_checkpoint_is_one_atomic_event_and_cut_transaction(self) -> None:
        capture_id = self._commit_raw_capture(suffix="checkpoint")
        self.assertEqual(
            tuple(
                item["record"]["capture_id"]
                for item in self.store.list_mission_raw_captures(self.mission_id)
            ),
            (capture_id,),
        )
        mission_stored = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, self.mission_id)
        )
        self.assertIsNotNone(mission_stored)
        assert mission_stored is not None
        strategy = self.store.read_mission_strategy_head(self.mission_id)
        mission_root = OwnerRevisionRef(
            "mission",
            self.mission_id,
            mission_stored.reference.revision,
            mission_stored.payload_digest,
        )
        strategy_root = OwnerRevisionRef(
            "strategy",
            str(strategy["strategy_id"]),
            int(strategy["revision"]),
            str(strategy["payload_digest"]),
        )
        branch_stored = self.store.get_head(
            TypedWorkspaceId(IdentityKind.BRANCH, "branch.theta")
        )
        self.assertIsNotNone(branch_stored)
        assert branch_stored is not None
        branch_root = OwnerRevisionRef(
            "branch",
            "branch.theta",
            branch_stored.reference.revision,
            branch_stored.payload_digest,
        )
        resolved = {
            mission_root.revision_identity: ResolvedOwnerRevision(
                reference=mission_root,
                mission_id=self.mission_id,
                validated_document=mission_stored.payload,
                is_current_head=True,
            ),
            strategy_root.revision_identity: ResolvedOwnerRevision(
                reference=strategy_root,
                mission_id=self.mission_id,
                validated_document=strategy["payload"],
                outgoing_refs=(branch_root,),
                is_current_head=True,
            ),
            branch_root.revision_identity: ResolvedOwnerRevision(
                reference=branch_root,
                mission_id=self.mission_id,
                validated_document=branch_stored.payload,
                is_current_head=True,
            ),
        }
        expected_commit = int(
            self.store.read_metadata()["current_project_commit"]
        ) + 1

        def checkpoint_for(capture: str | None):
            return prepare_direct_continuation_checkpoint(
                mission_root=mission_root,
                strategy_root=strategy_root,
                resolver=lambda reference: resolved.get(reference.revision_identity),
                pending_capture_locators=(
                    ()
                    if capture is None
                    else ({"capture_id": capture, "artifact_ordinal": 0},)
                ),
                predecessor_checkpoint=None,
                authoring_epoch_id=self.executive_epoch_id,
                project_commit=expected_commit,
                schema_version=1,
            )

        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=self.mission_id,
        )
        unscoped = checkpoint_for(None)
        unscoped_event = prepare_direct_executive_epoch_checkpointed_event(unscoped)
        with self.assertRaisesRegex(WorkspaceIntegrityError, "active write scope"):
            self.store.commit_direct_continuation_checkpoint(
                mission_id=self.mission_id,
                executive_epoch_id=self.executive_epoch_id,
                checkpoint_document=unscoped,
                event=unscoped_event,
                expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                expected_event_digest=str(chain[-1]["event_digest"]),
                lease=self.lease,
                command_id="epoch.checkpoint.unscoped",
                actor="successor-writer-test",
                expected_canonical_authority_digest=self.authority_digest,
            )
        with self.assertRaisesRegex(StaleCommandError, "pending [Cc]apture"):
            with self.store.direct_checkpoint_write_scope():
                bad_checkpoint = checkpoint_for("capture:absent")
                bad_event = prepare_direct_executive_epoch_checkpointed_event(
                    bad_checkpoint
                )
                self.store.commit_direct_continuation_checkpoint(
                    mission_id=self.mission_id,
                    executive_epoch_id=self.executive_epoch_id,
                    checkpoint_document=bad_checkpoint,
                    event=bad_event,
                    expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                    expected_event_digest=str(chain[-1]["event_digest"]),
                    lease=self.lease,
                    command_id="epoch.checkpoint.bad",
                    actor="successor-writer-test",
                    expected_canonical_authority_digest=self.authority_digest,
                )
        self.assertIsNone(
            self.store.read_continuation_checkpoint(
                executive_epoch_id=self.executive_epoch_id
            )
        )
        self.assertEqual(
            len(
                self.store.read_executive_epoch_events(
                    executive_epoch_id=self.executive_epoch_id,
                    mission_id=self.mission_id,
                )
            ),
            2,
        )

        with self.assertRaisesRegex(TypeError, "issued checkpoint"):
            with self.store.direct_checkpoint_write_scope():
                checkpoint = checkpoint_for(None)
                event = prepare_direct_executive_epoch_checkpointed_event(checkpoint)
                self.store.commit_direct_continuation_checkpoint(
                    mission_id=self.mission_id,
                    executive_epoch_id=self.executive_epoch_id,
                    checkpoint_document=checkpoint.document,
                    event=event,
                    expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                    expected_event_digest=str(chain[-1]["event_digest"]),
                    lease=self.lease,
                    command_id="epoch.checkpoint.mapping-rejected",
                    actor="successor-writer-test",
                    expected_canonical_authority_digest=self.authority_digest,
                )
        with self.store.direct_checkpoint_write_scope():
            checkpoint = checkpoint_for(None)
            event = prepare_direct_executive_epoch_checkpointed_event(checkpoint)
            outcome = self.store.commit_direct_continuation_checkpoint(
                mission_id=self.mission_id,
                executive_epoch_id=self.executive_epoch_id,
                checkpoint_document=checkpoint,
                event=event,
                expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                expected_event_digest=str(chain[-1]["event_digest"]),
                lease=self.lease,
                command_id="epoch.checkpoint.good",
                actor="successor-writer-test",
                expected_canonical_authority_digest=self.authority_digest,
            )
        self.assertEqual(outcome.project_commit, expected_commit)
        readback = self.store.read_continuation_checkpoint(
            executive_epoch_id=self.executive_epoch_id
        )
        self.assertIsNotNone(readback)
        assert readback is not None
        self.assertEqual(
            canonical_json_bytes(readback["document"]),
            canonical_json_bytes(checkpoint.document),
        )
        self.assertEqual(readback["payload_digest"], checkpoint.digest_sha256)
        self.assertIsNone(self.store.read_active_executive_epoch(self.mission_id))
        self.assertEqual(
            self.store.read_latest_executive_epoch_events(self.mission_id)[-1][
                "event_kind"
            ],
            "checkpointed",
        )
        self.store.verify_integrity()

    def test_v1_checkpoint_read_and_replay_authenticate_pending_capture_custody(
        self,
    ) -> None:
        capture_id = self._commit_raw_capture(suffix="checkpoint-custody")
        mission_stored = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, self.mission_id)
        )
        branch_stored = self.store.get_head(
            TypedWorkspaceId(IdentityKind.BRANCH, "branch.theta")
        )
        strategy = self.store.read_mission_strategy_head(self.mission_id)
        self.assertIsNotNone(mission_stored)
        self.assertIsNotNone(branch_stored)
        assert mission_stored is not None and branch_stored is not None
        mission_root = OwnerRevisionRef(
            "mission",
            self.mission_id,
            mission_stored.reference.revision,
            mission_stored.payload_digest,
        )
        strategy_root = OwnerRevisionRef(
            "strategy",
            str(strategy["strategy_id"]),
            int(strategy["revision"]),
            str(strategy["payload_digest"]),
        )
        branch_root = OwnerRevisionRef(
            "branch",
            "branch.theta",
            branch_stored.reference.revision,
            branch_stored.payload_digest,
        )
        resolved = {
            mission_root.revision_identity: ResolvedOwnerRevision(
                reference=mission_root,
                mission_id=self.mission_id,
                validated_document=mission_stored.payload,
                is_current_head=True,
            ),
            strategy_root.revision_identity: ResolvedOwnerRevision(
                reference=strategy_root,
                mission_id=self.mission_id,
                validated_document=strategy["payload"],
                outgoing_refs=(branch_root,),
                is_current_head=True,
            ),
            branch_root.revision_identity: ResolvedOwnerRevision(
                reference=branch_root,
                mission_id=self.mission_id,
                validated_document=branch_stored.payload,
                is_current_head=True,
            ),
        }
        expected_commit = int(
            self.store.read_metadata()["current_project_commit"]
        ) + 1
        checkpoint = prepare_direct_continuation_checkpoint(
            mission_root=mission_root,
            strategy_root=strategy_root,
            resolver=lambda reference: resolved.get(reference.revision_identity),
            pending_capture_locators=(
                {"capture_id": capture_id, "artifact_ordinal": 0},
            ),
            predecessor_checkpoint=None,
            authoring_epoch_id=self.executive_epoch_id,
            project_commit=expected_commit,
            schema_version=1,
        )
        event = prepare_direct_executive_epoch_checkpointed_event(checkpoint)
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=self.mission_id,
        )
        command_id = "epoch.checkpoint.capture-custody"
        with self.store.direct_checkpoint_write_scope():
            self.store.commit_direct_continuation_checkpoint(
                mission_id=self.mission_id,
                executive_epoch_id=self.executive_epoch_id,
                checkpoint_document=checkpoint,
                event=event,
                expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                expected_event_digest=str(chain[-1]["event_digest"]),
                lease=self.lease,
                command_id=command_id,
                actor="successor-writer-test",
                expected_canonical_authority_digest=self.authority_digest,
            )
        metadata_before = dict(self.store.read_metadata())

        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            row = connection.execute(
                "SELECT auxiliary_writes_json FROM transition_journal "
                "WHERE command_id = 'capture.checkpoint-custody'"
            ).fetchone()
            self.assertIsNotNone(row)
            auxiliary = json.loads(str(row[0]))
            capture_reference = next(
                item for item in auxiliary if item["table"] == "raw_capture"
            )
            capture_reference["row_digest"] = "f" * 64
            connection.execute(
                "UPDATE transition_journal SET auxiliary_writes_json = ? "
                "WHERE command_id = 'capture.checkpoint-custody'",
                (canonical_json_bytes(auxiliary).decode("utf-8"),),
            )
            connection.commit()

        with self.assertRaises(WorkspaceIntegrityError):
            self.store.read_continuation_checkpoint(
                executive_epoch_id=self.executive_epoch_id
            )
        with self.assertRaises(WorkspaceIntegrityError):
            with self.store.direct_checkpoint_write_scope():
                self.store.commit_direct_continuation_checkpoint(
                    mission_id=self.mission_id,
                    executive_epoch_id=self.executive_epoch_id,
                    checkpoint_document=checkpoint,
                    event=event,
                    expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                    expected_event_digest=str(chain[-1]["event_digest"]),
                    lease=self.lease,
                    command_id=command_id,
                    actor="successor-writer-test",
                    expected_canonical_authority_digest=self.authority_digest,
                )
        self.assertEqual(dict(self.store.read_metadata()), metadata_before)

    def test_failure_is_terminal_but_late_raw_capture_remains_capturable(self) -> None:
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=self.mission_id,
        )
        failed = prepare_direct_executive_epoch_failed_before_checkpoint_event(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=self.mission_id,
            reconciliation={
                "stage": "goal_runtime",
                "failure_reason": "goal ended without a checkpoint",
            },
        )
        self.store.fail_executive_epoch_before_checkpoint(
            mission_id=self.mission_id,
            executive_epoch_id=self.executive_epoch_id,
            event=failed,
            expected_event_ordinal=int(chain[-1]["event_ordinal"]),
            expected_event_digest=str(chain[-1]["event_digest"]),
            lease=self.lease,
            command_id="epoch.fail",
            actor="successor-writer-test",
            expected_canonical_authority_digest=self.authority_digest,
        )
        terminal_chain = self.store.read_executive_epoch_events(
            executive_epoch_id=self.executive_epoch_id,
            mission_id=self.mission_id,
        )
        self.assertEqual(
            self.store.read_executive_epoch_events_by_goal_thread(
                mission_id=self.mission_id,
                goal_thread_id="goal-thread:successor-writer",
            ),
            terminal_chain,
        )
        capture_id = self._commit_raw_capture(
            suffix="late-after-terminal",
            epoch_id=self.executive_epoch_id,
        )
        self.assertEqual(
            self.store.read_raw_capture(capture_id)["record"]["mission_id"],
            self.mission_id,
        )
        with self.assertRaisesRegex(StaleCommandError, "current bound"):
            self._commit_context(
                context_id="context.after-terminal",
                payload={"purpose": "must be rejected"},
                command_id="context.after-terminal.create",
                expected_revision=None,
                expected_digest=None,
            )


    def test_first_class_context_does_not_stale_executive_authority(self) -> None:
        authority_before = self.store.read_active_executive_epoch(self.mission_id)
        self.assertIsNotNone(authority_before)
        self._commit_context(
            context_id="context.successor-authority",
            payload={"purpose": "advance an unrelated first-class owner"},
            command_id="context.successor-authority.create",
            expected_revision=None,
            expected_digest=None,
        )
        self.assertEqual(
            self.store.read_active_executive_epoch(self.mission_id),
            authority_before,
        )


if __name__ == "__main__":
    unittest.main()
