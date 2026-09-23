from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSTATION_PACKAGES_ROOT = REPO_ROOT / "packages"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(WORKSTATION_PACKAGES_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSTATION_PACKAGES_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core.candidate_revision import (  # noqa: E402
    candidate_a1_requirement,
    candidate_schema_digest_v4,
)
from research_core.evidence_store import EvidenceCAS  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    ResolvedOwnerRevision,
    prepare_direct_continuation_checkpoint,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    prepare_direct_executive_epoch_checkpointed_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.observability import (  # noqa: E402
    CaptureWorkspaceObserver,
    EventKind,
    ObservationEnvironment,
)
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_paths import (  # noqa: E402
    attest_current_principal,
    stable_principal_owner_binding,
)
from research_core.workspace_recovery import (  # noqa: E402
    BACKUP_ID_RE,
    CanonicalBinding,
    PortableBackupVerificationReport,
    RecoveryError,
    RecoveryLifecycle,
    StagingPortableImportReport,
    StagingPortableMigrationResult,
    TrustedSourceControlVerification,
    WriterFenceProof,
    _LEGACY_CLOSURE_CAS_INVENTORY,
    _COMMITTED_BLOB_CAS_INVENTORY,
    _acquire_rebackup_lease,
    _canonical_binding_from_metadata,
    _closed_source_unit_readback,
    _deletion_fence_payload,
    _newest_journaled_closure_from_snapshot,
    _write_bit_present,
    _SelectedJournaledClosure,
    _execute_restore_copy,
    _existing_rebackup_stage,
    _fixed_target_restore_artifacts,
    _issue_verified_backup_integrity_authority,
    _issue_source_incarnation_fence,
    _load_persisted_backup_evidence,
    _protect_closed_tree,
    _rebackup_stage_document,
    _rebackup_stage_path,
    _restore_test_record,
    _remove_tree,
    _remove_windows_write_deny,
    _sqlite_integrity,
    _store_mode_binding_from_metadata,
    _verify_installed_release_canonical_binding,
    activate_portable_checkpoint_source_takeover_from_owner,
    _select_newest_journaled_closure,
    _verify_backup_tree_contents,
    _verify_recovery_snapshot_semantics,
    _verify_trusted_source_control,
    begin_recovery,
    confirm_explicit_takeover,
    create_bound_backup,
    create_installed_current_backup,
    derive_staging_portable_migration_operation_id,
    inspect_installed_current_backup,
    import_portable_backup_to_staging,
    migrate_portable_backup_to_staging,
    protect_portable_backup_materialization,
    dispose_portable_backup_materialization,
    prepare_offline_migration,
    prepare_side_by_side_restore,
    prepare_portable_side_by_side_restore,
    prepare_portable_fixed_target_restore,
    prepare_takeover,
    transition_recovery,
    verify_backup_set,
    verify_restored_workspace,
    verify_portable_backup_set,
    verify_live_canonical_binding,
)
from research_core.workspace_schema import (  # noqa: E402
    IdentityKind,
    TypedWorkspaceId,
    open_snapshot_connection,
)
from research_core.workspace_store import (  # noqa: E402
    CheckpointSourceError,
    CheckpointSourceFailureWithSafeWriterError,
    CheckpointSourceOperationTeardownError,
    CheckpointSourceReacquisitionError,
    StaleWriterError,
    WorkspaceIntegrityError,
    WorkspaceStore,
)
from research_core_test_support import resolve_verified_test_source_commit  # noqa: E402


def digest(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def structured_digest(payload) -> str:
    return digest(canonical_json_bytes(payload))


def direct_genesis_fixture(
    *,
    project_id: str,
    mission_id: str = "mission.recovery",
    branch_id: str = "branch.recovery",
    strategy_id: str = "strategy.recovery",
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
    branch_digest = digest(canonical_json_bytes(branch))

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






class SQLiteIntegrityConnectionTests(unittest.TestCase):
    def test_backup_integrity_connection_has_no_busy_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "backup.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE probe(value TEXT)")
                connection.commit()
            finally:
                connection.close()
            real_connect = sqlite3.connect
            with patch(
                "research_core.workspace_recovery.sqlite3.connect",
                wraps=real_connect,
            ) as opened:
                _sqlite_integrity(database)
        opened.assert_called_once_with(
            f"file:{database.as_posix()}?mode=ro",
            uri=True,
            timeout=0,
        )


class BackupIdentityTests(unittest.TestCase):
    def test_portable_backup_identity_has_no_arbitrary_character_ceiling(self) -> None:
        self.assertIsNotNone(BACKUP_ID_RE.fullmatch("backup." + "a" * 512))

class RebackupStageOwnershipTests(unittest.TestCase):
    backup_id = "crash-owned-rebackup"
    source_manifest_sha256 = "a" * 64
    migration_result_sha256 = "b" * 64

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-rh-rebackup-stage-tests-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.paths = WorkspacePaths.from_root(self.root / "workspace")
        self.paths.backups.mkdir(parents=True)
        self.paths.recovery.mkdir(parents=True)
        self.addCleanup(
            _remove_tree,
            self.paths.root,
            owned_parent=self.root,
            ignore_errors=True,
        )

    def stage_document(self) -> dict:
        return _rebackup_stage_document(
            backup_id=self.backup_id,
            source_manifest_sha256=self.source_manifest_sha256,
            migration_result_sha256=self.migration_result_sha256,
        )

    def crash_stage_owner(
        self,
        *,
        source_manifest_sha256: str,
        create_stage: bool = True,
    ) -> None:
        script = r"""
import os
import sys
from pathlib import Path

package_root = Path(sys.argv[1]).resolve()
if str(package_root) not in sys.path:
    sys.path.insert(0, str(package_root))

from research_core.workspace_paths import WorkspacePaths
from research_core.workspace_recovery import (
    _acquire_rebackup_lease,
    _fsync_directory,
    _rebackup_stage_document,
    _rebackup_stage_path,
)

paths = WorkspacePaths.from_root(Path(sys.argv[2]).resolve())
backup_id, source_digest, result_digest = sys.argv[3:6]
create_stage = sys.argv[6] == "1"
document = _rebackup_stage_document(
    backup_id=backup_id,
    source_manifest_sha256=source_digest,
    migration_result_sha256=result_digest,
)
with _acquire_rebackup_lease(
    paths,
    backup_id=backup_id,
    source_manifest_sha256=source_digest,
    migration_result_sha256=result_digest,
) as lease:
    if create_stage:
        stage = _rebackup_stage_path(
            paths,
            stage_document=document,
            owner_lease_id=lease.lease_id,
        )
        stage.mkdir(mode=0o700)
        _fsync_directory(paths.backups)
    os._exit(86)
"""
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(PACKAGE_ROOT),
                str(self.paths.root),
                self.backup_id,
                source_manifest_sha256,
                self.migration_result_sha256,
                "1" if create_stage else "0",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            86,
            f"hard-crash fixture failed: stdout={completed.stdout!r} "
            f"stderr={completed.stderr!r}",
        )

    def test_exact_abandoned_owner_survives_repeated_pre_adoption_crashes(
        self,
    ) -> None:
        self.crash_stage_owner(
            source_manifest_sha256=self.source_manifest_sha256
        )
        predecessor_stage = next(
            self.paths.backups.glob(f".stage-{self.backup_id}-*")
        )
        document = self.stage_document()
        operation_prefix = (
            f".stage-{self.backup_id}-{document['binding_sha256'][:32]}-"
        )
        predecessor_owner = predecessor_stage.name.removeprefix(operation_prefix)
        self.crash_stage_owner(
            source_manifest_sha256=self.source_manifest_sha256,
            create_stage=False,
        )

        with _acquire_rebackup_lease(
            self.paths,
            backup_id=self.backup_id,
            source_manifest_sha256=self.source_manifest_sha256,
            migration_result_sha256=self.migration_result_sha256,
        ) as lease:
            self.assertTrue(lease.prior_abandoned)
            self.assertEqual(lease.predecessor_lease_id, predecessor_owner)
            self.assertEqual(lease.predecessor_scope_sha256, lease.scope_sha256)
            adopted, state = _existing_rebackup_stage(
                self.paths,
                backup_id=self.backup_id,
                stage_document=document,
                inventory=(),
                lease=lease,
            )
            self.assertEqual(state, "partial")
            self.assertEqual(adopted, predecessor_stage)
            self.assertTrue(adopted.is_dir())

    def test_unowned_foreign_and_different_scope_stages_fail_closed(self) -> None:
        document = self.stage_document()
        operation_prefix = (
            f".stage-{self.backup_id}-{document['binding_sha256'][:32]}"
        )
        candidates = (
            self.paths.backups / operation_prefix,
            _rebackup_stage_path(
                self.paths,
                stage_document=document,
                owner_lease_id="lease-" + "f" * 32,
            ),
        )
        for candidate in candidates:
            with self.subTest(stage=candidate.name):
                candidate.mkdir()
                with _acquire_rebackup_lease(
                    self.paths,
                    backup_id=self.backup_id,
                    source_manifest_sha256=self.source_manifest_sha256,
                    migration_result_sha256=self.migration_result_sha256,
                ) as lease:
                    with self.assertRaises(RecoveryError) as captured:
                        _existing_rebackup_stage(
                            self.paths,
                            backup_id=self.backup_id,
                            stage_document=document,
                            inventory=(),
                            lease=lease,
                        )
                    self.assertEqual(
                        captured.exception.code,
                        "rebackup_stage_collision",
                    )
                    self.assertTrue(candidate.exists())
                _remove_tree(candidate, owned_parent=self.paths.backups)

        self.crash_stage_owner(source_manifest_sha256="c" * 64)
        mismatched = next(
            self.paths.backups.glob(f".stage-{self.backup_id}-*")
        )
        with _acquire_rebackup_lease(
            self.paths,
            backup_id=self.backup_id,
            source_manifest_sha256=self.source_manifest_sha256,
            migration_result_sha256=self.migration_result_sha256,
        ) as lease:
            self.assertTrue(lease.prior_abandoned)
            self.assertIsNone(lease.predecessor_scope_sha256)
            with self.assertRaises(RecoveryError) as captured:
                _existing_rebackup_stage(
                    self.paths,
                    backup_id=self.backup_id,
                    stage_document=document,
                    inventory=(),
                    lease=lease,
                )
            self.assertEqual(captured.exception.code, "rebackup_stage_collision")
            self.assertTrue(mismatched.exists())


class WorkspaceRecoveryTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-rh-workspace-tests-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = WorkspacePaths.from_root(self.root / "workspace")
        self.addCleanup(
            _remove_tree,
            self.paths.root,
            owned_parent=self.root,
            ignore_errors=True,
        )
        source_commit, archive_bound = resolve_verified_test_source_commit(REPO_ROOT)
        if archive_bound:
            git_head = patch(
                "research_core.workspace_recovery._git_head",
                return_value=source_commit,
            )
            git_head.start()
            self.addCleanup(git_head.stop)
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit=source_commit)
        self.assertTrue(loaded.ok, loaded.failure)
        self.snapshot = loaded.value
        assert self.snapshot is not None
        self.canonical_path = self.snapshot.authorized_path
        self.canonical_bytes = self.canonical_path.read_bytes()
        project_id = getattr(
            self,
            "fixture_project_id",
            "project.riemann_hypothesis",
        )
        mission_id = getattr(self, "fixture_mission_id", "mission.recovery")
        self.project_id = project_id
        self.mission_id = mission_id
        self.store = WorkspaceStore.initialize_direct_mission_workspace(
            self.paths,
            project_id=project_id,
            canonical_snapshot=self.snapshot,
            actor="test.fixture",
        )
        self.paths = self.store.paths
        self.cas = EvidenceCAS(self.paths)
        metadata = self.store.read_metadata()
        authority_digest = str(metadata["canonical_authority_digest"])
        self.authority_digest = authority_digest
        lease = self.store.claim_writer(
            owner="source-writer",
            creation_basis="persist direct recovery fixture",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=authority_digest,
        )
        self.store.initialize_direct_mission_genesis(
            direct_genesis_fixture(project_id=project_id, mission_id=mission_id),
            mission_id=mission_id,
            canonical_snapshot=self.snapshot,
            lease=lease,
            command_id="recovery.direct-genesis",
            actor="test.fixture",
        )
        mission = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, mission_id)
        )
        self.assertIsNotNone(mission)
        assert mission is not None
        epoch_id = "epoch.recovery.fixture"
        self.epoch_id = epoch_id
        authorized = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id=epoch_id,
            mission_root=OwnerRevisionRef(
                "mission",
                mission_id,
                mission.reference.revision,
                mission.payload_digest,
            ),
            predecessor_checkpoint=None,
        )
        authorization_metadata = self.store.read_metadata()
        self.store.authorize_executive_epoch(
            mission_id=mission_id,
            executive_epoch_id=epoch_id,
            event=authorized,
            lease=lease,
            command_id="recovery.epoch.authorize",
            actor="test.fixture",
            expected_canonical_authority_digest=authority_digest,
            expected_mission_host_store_cut={
                "project_commit": int(authorization_metadata["current_project_commit"]),
                "current_root_digest": str(authorization_metadata["current_root_digest"]),
                "transition_head_digest": authorization_metadata["transition_head_digest"],
                "canonical_authority_digest": str(
                    authorization_metadata["canonical_authority_digest"]
                ),
            },
        )
        bound = prepare_direct_executive_epoch_bound_event(
            executive_epoch_id=epoch_id,
            mission_id=mission_id,
            goal_thread_id="thread:recovery-fixture",
            workspace_root=str(self.paths.root),
        )
        self.store.bind_executive_epoch(
            mission_id=mission_id,
            executive_epoch_id=epoch_id,
            event=bound,
            expected_event_ordinal=1,
            expected_event_digest=authorized.digest_sha256,
            lease=lease,
            command_id="recovery.epoch.bind",
            actor="test.fixture",
            expected_canonical_authority_digest=authority_digest,
        )
        events = self.store.read_active_executive_epoch(mission_id)
        self.assertIsNotNone(events)
        assert events is not None
        authority = reissue_direct_executive_epoch_authority(
            event_readbacks=events,
            project_id=project_id,
            root_identity=self.paths.root_identity,
            canonical_authority_digest=authority_digest,
        )
        self.epoch_authority = authority
        candidate_id = "candidate.recovery-closure"
        candidate = {
            "schema_version": 4,
            "kind": "candidate_revision",
            "contract_schema_sha256": candidate_schema_digest_v4(REPO_ROOT),
            "candidate_id": candidate_id,
            "mission_id": mission_id,
            "proposal_kind": "mathematical_statement",
            "exact_statement": (
                "This Candidate proves the Riemann Hypothesis and supplies a "
                "complete unconditional proof."
            ),
            "scope_and_reach": "complete unconditional proof of RH",
            "complete_target_claim": {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            "standing": {
                "status": "open",
                "basis": "recovery fixture preserving one exact A1 closure",
            },
            "provenance": {
                "kind": "native_executive_epoch",
                "executive_epoch_id": epoch_id,
                "executive_epoch_authority_sha256": authority.authority_sha256,
            },
            "authority_class": "candidate_only",
            "canonical_effect": "none",
        }
        self.candidate_payload = candidate
        requirement = candidate_a1_requirement(candidate)
        self.assertIsNotNone(requirement)
        self.store.commit_candidate_revision(
            executive_epoch_id=epoch_id,
            mission_id=mission_id,
            candidate_id=candidate_id,
            payload=candidate,
            expected_head_revision=None,
            expected_head_payload_digest=None,
            a1_requirement=requirement,
            lease=lease,
            command_id="recovery.candidate.a1",
            actor="test.fixture",
            expected_canonical_authority_digest=authority_digest,
        )
        stored = self.store.read_candidate_revision(
            mission_id=mission_id,
            candidate_id=candidate_id,
        )
        binding = self.store.read_candidate_a1_binding(
            candidate_id=candidate_id,
            candidate_revision=int(stored["revision"]),
            candidate_digest=str(stored["payload_digest"]),
        )
        closure = self.store.load_verified_closure_bundle(
            cas=self.cas,
            closure_id=binding.closure_id,
        )
        self.closure = closure.manifest
        self.blob = closure.blob_records[binding.artifact_digest]
        self.fence_reference = WriterFenceProof(
            binding.evidence_id,
            binding.evidence_revision,
            binding.evidence_payload_digest,
        )
        post = self.store.read_metadata()
        self.store.release_writer(
            lease,
            expected_project_commit=int(post["current_project_commit"]),
            expected_root_digest=str(post["current_root_digest"]),
            expected_canonical_authority_digest=str(
                post["canonical_authority_digest"]
            ),
        )

    def create_backup(self, backup_id: str = "backup-1"):
        return create_bound_backup(
            store=self.store,
            cas=self.cas,
            backup_id=backup_id,
            authority_repo_root=REPO_ROOT,
            closure_id=self.closure.closure_id,
        )

    def portable_copy(self, backup, name: str) -> Path:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup.path, target)
        _protect_closed_tree(target, deny_delete=False)
        self.addCleanup(
            _remove_tree,
            target,
            owned_parent=target.parent,
            ignore_errors=True,
        )
        return target

    def complete_checkpoint_and_keep_writer(
        self,
        *,
        writer_owner: str = "asr0a-source-writer",
    ):
        metadata = self.store.read_metadata()
        lease = self.store.claim_writer(
            owner=writer_owner,
            creation_basis="retain active writer while testing current backup",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=self.authority_digest,
        )
        mission = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, self.mission_id)
        )
        strategy = self.store.get_head(
            TypedWorkspaceId(IdentityKind.STRATEGY, "strategy.recovery")
        )
        branch = self.store.get_head(
            TypedWorkspaceId(IdentityKind.BRANCH, "branch.recovery")
        )
        assert mission is not None and strategy is not None and branch is not None
        mission_root = OwnerRevisionRef(
            "mission",
            self.mission_id,
            mission.reference.revision,
            mission.payload_digest,
        )
        strategy_root = OwnerRevisionRef(
            "strategy",
            "strategy.recovery",
            strategy.reference.revision,
            strategy.payload_digest,
        )
        branch_root = OwnerRevisionRef(
            "branch",
            "branch.recovery",
            branch.reference.revision,
            branch.payload_digest,
        )
        resolved = {
            mission_root.revision_identity: ResolvedOwnerRevision(
                reference=mission_root,
                mission_id=self.mission_id,
                validated_document=mission.payload,
                is_current_head=True,
            ),
            strategy_root.revision_identity: ResolvedOwnerRevision(
                reference=strategy_root,
                mission_id=self.mission_id,
                validated_document=strategy.payload,
                outgoing_refs=(branch_root,),
                is_current_head=True,
            ),
            branch_root.revision_identity: ResolvedOwnerRevision(
                reference=branch_root,
                mission_id=self.mission_id,
                validated_document=branch.payload,
                is_current_head=True,
            ),
        }
        current = self.store.read_metadata()
        checkpoint = prepare_direct_continuation_checkpoint(
            mission_root=mission_root,
            strategy_root=strategy_root,
            resolver=lambda reference: resolved.get(reference.revision_identity),
            predecessor_checkpoint=None,
            authoring_epoch_id=self.epoch_id,
            project_commit=int(current["current_project_commit"]) + 1,
            schema_version=1,
        )
        chain = self.store.read_executive_epoch_events(
            executive_epoch_id=self.epoch_id,
            mission_id=self.mission_id,
        )
        event = prepare_direct_executive_epoch_checkpointed_event(checkpoint)
        with self.store.direct_checkpoint_write_scope():
            self.store.commit_direct_continuation_checkpoint(
                mission_id=self.mission_id,
                executive_epoch_id=self.epoch_id,
                checkpoint_document=checkpoint,
                event=event,
                expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                expected_event_digest=str(chain[-1]["event_digest"]),
                lease=lease,
                command_id="asr0a.fixture.checkpoint",
                actor="test.fixture",
                expected_canonical_authority_digest=self.authority_digest,
            )
        return lease, checkpoint

    def publish_checkpoint_source(self, lease, checkpoint, *, fault_hook=None):
        metadata = self.store.read_metadata()
        return self.store.publish_checkpoint_source_handoff(
            lease=lease,
            mission_id=self.mission_id,
            checkpoint_id=str(checkpoint.document["checkpoint_id"]),
            checkpoint_sha256=checkpoint.digest_sha256,
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=self.authority_digest,
            successor_owner=lease.owner,
            successor_creation_basis="test checkpoint-source successor",
            fault_hook=fault_hook,
        )

    @staticmethod
    def checkpoint_source_claim_id(source) -> str:
        return (
            f"asr0a.checkpoint-{source.checkpoint_project_commit}-"
            f"{source.checkpoint_sha256}"
        )

    def create_claimed_current_backup(
        self,
        *,
        writer_owner: str = "asr0a-source-writer",
    ):
        lease, checkpoint = self.complete_checkpoint_and_keep_writer(
            writer_owner=writer_owner
        )
        handoff = self.publish_checkpoint_source(lease, checkpoint)
        assert handoff.source is not None
        claim_id = self.checkpoint_source_claim_id(handoff.source)
        claimed = self.store.claim_checkpoint_source_for_pending(
            mission_id=self.mission_id,
            claim_id=claim_id,
        )
        release_root, release_sha = self.installed_release_fixture()
        backup, report = create_installed_current_backup(
            store=self.store,
            cas=self.cas,
            backup_id=claim_id,
            mission_id=self.mission_id,
            project_id=self.project_id,
            installed_release_root=release_root,
            executing_release_sha=release_sha,
            checkpoint_source=claimed,
        )
        return handoff.successor_lease, checkpoint, claimed, backup, report

    def start_successor_epoch(self, lease, checkpoint) -> str:
        epoch_id = "epoch.recovery.after-checkpoint"
        mission = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, self.mission_id)
        )
        assert mission is not None
        authorized = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id=epoch_id,
            mission_root=OwnerRevisionRef(
                "mission",
                self.mission_id,
                mission.reference.revision,
                mission.payload_digest,
            ),
            predecessor_checkpoint=checkpoint.to_reference(),
        )
        authorization_metadata = self.store.read_metadata()
        self.store.authorize_executive_epoch(
            mission_id=self.mission_id,
            executive_epoch_id=epoch_id,
            event=authorized,
            lease=lease,
            command_id="asr0a.successor.authorize",
            actor="test.fixture",
            expected_canonical_authority_digest=self.authority_digest,
            expected_mission_host_store_cut={
                "project_commit": int(authorization_metadata["current_project_commit"]),
                "current_root_digest": str(authorization_metadata["current_root_digest"]),
                "transition_head_digest": authorization_metadata["transition_head_digest"],
                "canonical_authority_digest": str(
                    authorization_metadata["canonical_authority_digest"]
                ),
            },
        )
        bound = prepare_direct_executive_epoch_bound_event(
            executive_epoch_id=epoch_id,
            mission_id=self.mission_id,
            goal_thread_id="thread:asr0a-active-goal",
            workspace_root=str(self.paths.root),
        )
        self.store.bind_executive_epoch(
            mission_id=self.mission_id,
            executive_epoch_id=epoch_id,
            event=bound,
            expected_event_ordinal=1,
            expected_event_digest=authorized.digest_sha256,
            lease=lease,
            command_id="asr0a.successor.bind",
            actor="test.fixture",
            expected_canonical_authority_digest=self.authority_digest,
        )
        return epoch_id

    def advance_live_branch(self, lease, epoch_id: str) -> int:
        branch = self.store.get_head(
            TypedWorkspaceId(IdentityKind.BRANCH, "branch.recovery")
        )
        assert branch is not None
        payload = deep_thaw(branch.payload)
        payload["question"] = "What changed after the immutable ASR-0A cut?"
        outcome = self.store.commit_branch_revision(
            executive_epoch_id=epoch_id,
            mission_id=self.mission_id,
            branch_id="branch.recovery",
            payload=payload,
            expected_head_revision=branch.reference.revision,
            expected_head_payload_digest=branch.payload_digest,
            lease=lease,
            command_id="asr0a.live.advance",
            actor="test.fixture",
            expected_canonical_authority_digest=self.authority_digest,
        )
        return outcome.project_commit

    def release_writer(self, lease) -> None:
        metadata = self.store.read_metadata()
        if metadata["current_writer_epoch"] == lease.epoch:
            self.store.release_writer(
                lease,
                expected_project_commit=int(metadata["current_project_commit"]),
                expected_root_digest=str(metadata["current_root_digest"]),
                expected_canonical_authority_digest=self.authority_digest,
            )

    def installed_release_fixture(self) -> tuple[Path, str]:
        release_sha = "f" * 40
        root = self.root / "installed" / release_sha
        canonical = root / self.snapshot.authority_vector.canonical_state_path
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(self.canonical_bytes)
        record = {
            "schema_version": "wc.rh_mission_host_release.v1",
            "release_sha": release_sha,
            "bundle_sha256": digest("installed-bundle"),
            "bundle_size": 1,
            "node_version": "test-node",
            "pnpm_version": "test-pnpm",
            "codex_version": "test-codex",
            "service_package": "@workstation-control/rh-mission-host",
        }
        (root / ".mathematical-research-release.json").write_bytes(
            canonical_json_bytes(record)
        )
        return root, release_sha

    def rebind_manifest_to_database(self, root: Path) -> None:
        database = root / "workspace.sqlite3"
        sqlite_raw = database.read_bytes()
        sqlite_sha256 = digest(sqlite_raw)
        sqlite_length = len(sqlite_raw)

        def bind(payload: dict) -> None:
            self.assertEqual(payload["deletion_directives"], [])
            self.assertEqual(payload["tombstones"], [])
            payload["sqlite_sha256"] = sqlite_sha256
            payload["sqlite_length"] = sqlite_length
            fence_raw = canonical_json_bytes(_deletion_fence_payload((), ()))
            entries = [
                {
                    "path": "workspace.sqlite3",
                    "sha256": sqlite_sha256,
                    "length": sqlite_length,
                },
                {
                    "path": "deletion_fence.json",
                    "sha256": digest(fence_raw),
                    "length": len(fence_raw),
                },
                *(
                    {
                        "path": item["logical_path"],
                        "sha256": item["sha256"],
                        "length": item["length"],
                    }
                    for item in payload["evidence_inventory"]
                ),
            ]
            restored_root = digest(
                canonical_json_bytes(
                    sorted(entries, key=lambda item: str(item["path"]))
                )
            )
            protected = sorted(
                [
                    "workspace.sqlite3",
                    "deletion_fence.json",
                    *(item["logical_path"] for item in payload["evidence_inventory"]),
                ]
            )
            receipt = payload["restore_test"]
            receipt["restored_root_sha256"] = restored_root
            receipt["read_only_seal_sha256"] = digest(
                canonical_json_bytes(
                    {
                        "format": "research-workspace-read-only-v1",
                        "lifecycle": "verified_read_only",
                        "protected_files": protected,
                        "protected_root_sha256": restored_root,
                        "writable": False,
                        "mission_auto_resume": False,
                        "canonical_effect": "none",
                    }
                )
            )
            receipt["receipt_sha256"] = digest(
                canonical_json_bytes(
                    {
                        key: value
                        for key, value in receipt.items()
                        if key != "receipt_sha256"
                    }
                )
            )

        self.rewrite_manifest(root, bind)
        _protect_closed_tree(root, deny_delete=False)

    def test_checkpoint_source_latest_replacement_and_preflight_finalize_replay(
        self,
    ) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        first = self.publish_checkpoint_source(lease, checkpoint)
        self.assertEqual(first.status, "published")
        self.assertGreater(first.database_bytes, 0)
        self.assertGreaterEqual(first.handoff_elapsed_ms, 0)
        self.assertIsNotNone(first.source)
        assert first.source is not None
        first_path = first.source.path
        first_incarnation = first.source.source_incarnation
        with first_path.open("rb") as checkpoint_source:
            self.assertEqual(checkpoint_source.read(16), b"SQLite format 3\x00")
            self.assertEqual(checkpoint_source.read(4)[2:4], b"\x01\x01")
        self.assertFalse(first_path.with_name(first_path.name + "-wal").exists())
        self.assertFalse(first_path.with_name(first_path.name + "-shm").exists())

        second = self.publish_checkpoint_source(first.successor_lease, checkpoint)
        try:
            self.assertEqual(second.status, "published")
            self.assertIsNotNone(second.source)
            assert second.source is not None
            self.assertEqual(second.source.path, first_path)
            self.assertNotEqual(second.source.source_incarnation, first_incarnation)
            claim_id = self.checkpoint_source_claim_id(second.source)
            projected = self.store.inspect_checkpoint_source_preflight(
                mission_id=self.mission_id,
                pending_claim_id=None,
            )
            self.assertEqual(
                set(projected),
                {
                    "schema_version",
                    "mission_id",
                    "current",
                    "latest_checkpoint",
                    "unclaimed_source",
                    "claimed_source",
                },
            )
            self.assertEqual(projected["unclaimed_source"], projected["latest_checkpoint"])
            self.assertIsNone(projected["claimed_source"])
            with self.assertRaises(CheckpointSourceError) as mismatched:
                self.store.inspect_checkpoint_source_preflight(
                    mission_id=self.mission_id,
                    pending_claim_id="asr0a.checkpoint-1-" + "0" * 64,
                )
            self.assertEqual(
                mismatched.exception.code,
                "checkpoint_source_pending_conflict",
            )

            claimed = self.store.claim_checkpoint_source_for_pending(
                mission_id=self.mission_id,
                claim_id=claim_id,
            )
            claimed_bytes = claimed.path.read_bytes()
            replayed = self.store.claim_checkpoint_source_for_pending(
                mission_id=self.mission_id,
                claim_id=claim_id,
            )
            self.assertEqual(replayed.source_binding_payload(), claimed.source_binding_payload())
            self.assertEqual(replayed.path.read_bytes(), claimed_bytes)
            self.assertFalse(
                stat.S_IMODE(os.lstat(claimed.path).st_mode)
                & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            )
            orphaned = self.store.inspect_checkpoint_source_preflight(
                mission_id=self.mission_id,
                pending_claim_id=None,
            )
            self.assertIsNone(orphaned["unclaimed_source"])
            self.assertEqual(orphaned["claimed_source"], orphaned["latest_checkpoint"])
            with self.assertRaises(CheckpointSourceError) as wrong_claim:
                self.store.inspect_checkpoint_source_preflight(
                    mission_id=self.mission_id,
                    pending_claim_id="asr0a.checkpoint-1-" + "0" * 64,
                )
            self.assertEqual(
                wrong_claim.exception.code,
                "checkpoint_source_pending_conflict",
            )
            self.assertEqual(
                self.store.finalize_claimed_checkpoint_source(
                    mission_id=self.mission_id,
                    expected_source_incarnation=claimed.source_incarnation,
                    claim_id=claim_id,
                ),
                "finalized",
            )
            self.assertEqual(
                self.store.finalize_claimed_checkpoint_source(
                    mission_id=self.mission_id,
                    expected_source_incarnation=claimed.source_incarnation,
                    claim_id=claim_id,
                ),
                "already_absent",
            )
            cleaned = self.store.inspect_checkpoint_source_preflight(
                mission_id=self.mission_id,
                pending_claim_id=claim_id,
            )
            self.assertIsNone(cleaned["unclaimed_source"])
            self.assertIsNone(cleaned["claimed_source"])
        finally:
            self.release_writer(second.successor_lease)

    def test_checkpoint_source_capture_failure_reacquires_and_reports_measurement(
        self,
    ) -> None:
        observer = CaptureWorkspaceObserver()
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        self.store = WorkspaceStore.open(
            self.paths,
            expected_project_id=self.project_id,
            observer=observer,
        )

        def fail_after_quiesce(phase, _path, _value):
            if phase == "after_checkpoint_source_quiesce":
                raise OSError(errno.EIO, "forced capture failure")

        handoff = self.publish_checkpoint_source(
            lease,
            checkpoint,
            fault_hook=fail_after_quiesce,
        )
        try:
            self.assertEqual(handoff.status, "capture_failed_writer_reacquired")
            self.assertTrue(handoff.checkpoint_completed)
            self.assertIsNone(handoff.source)
            self.assertGreater(handoff.database_bytes, 0)
            self.assertGreaterEqual(handoff.handoff_elapsed_ms, 0)
            self.assertEqual(
                self.store.read_metadata()["current_writer_epoch"],
                handoff.successor_lease.epoch,
            )
            warning = [
                event
                for event in observer.events
                if event.kind is EventKind.BACKUP_CHANGED
                and event.attributes.get("status")
                == "checkpoint_source_capture_failed_writer_reacquired"
            ]
            self.assertEqual(len(warning), 1)
            self.assertEqual(
                warning[0].attributes["byte_count"],
                handoff.database_bytes,
            )
            self.assertEqual(
                warning[0].attributes["duration_seconds"],
                handoff.handoff_elapsed_ms / 1000,
            )
        finally:
            self.release_writer(handoff.successor_lease)

    def test_checkpoint_source_contention_skips_snapshot_and_preserves_writer(
        self,
    ) -> None:
        observer = CaptureWorkspaceObserver()
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        first = self.publish_checkpoint_source(lease, checkpoint)
        self.assertEqual(first.status, "published")
        self.assertIsNotNone(first.source)
        self.assertIsNotNone(first.successor_lease)
        assert first.source is not None
        assert first.successor_lease is not None
        prior_source_bytes = first.source.path.read_bytes()
        prior_source_incarnation = first.source.source_incarnation
        prior_source_projection = self.store.inspect_checkpoint_source_preflight(
            mission_id=self.mission_id,
            pending_claim_id=None,
        )["unclaimed_source"]

        self.store = WorkspaceStore.open(
            self.paths,
            expected_project_id=self.project_id,
            observer=observer,
        )
        contender = WorkspaceStore.open(
            self.paths,
            expected_project_id=self.project_id,
        )
        metadata_before = self.store.read_metadata()
        with contender._checkpoint_source_operation() as operation_acquired:
            self.assertTrue(operation_acquired)
            skipped = self.publish_checkpoint_source(
                first.successor_lease,
                checkpoint,
            )

        self.assertEqual(skipped.status, "snapshot_skipped_writer_preserved")
        self.assertTrue(skipped.checkpoint_completed)
        self.assertIsNone(skipped.source)
        self.assertIsNone(skipped.successor_lease)
        self.assertEqual(skipped.preserved_lease, first.successor_lease)
        self.assertEqual(skipped.capture_failure_code, "checkpoint_source_busy")
        self.assertEqual(
            self.store.read_metadata()["current_writer_epoch"],
            metadata_before["current_writer_epoch"],
        )
        self.assertEqual(first.source.path.read_bytes(), prior_source_bytes)
        unchanged = self.store.inspect_checkpoint_source_preflight(
            mission_id=self.mission_id,
            pending_claim_id=None,
        )
        self.assertEqual(unchanged["unclaimed_source"], prior_source_projection)
        warning = [
            event
            for event in observer.events
            if event.kind is EventKind.BACKUP_CHANGED
            and event.attributes.get("status")
            == "checkpoint_source_snapshot_skipped_writer_preserved"
        ]
        self.assertEqual(len(warning), 1)
        self.assertEqual(
            warning[0].attributes["reason_code"],
            "checkpoint_source_busy",
        )

        retried = self.publish_checkpoint_source(
            first.successor_lease,
            checkpoint,
        )
        try:
            self.assertEqual(retried.status, "published")
            self.assertIsNotNone(retried.source)
            assert retried.source is not None
            self.assertNotEqual(
                retried.source.source_incarnation,
                prior_source_incarnation,
            )
        finally:
            self.assertIsNotNone(retried.successor_lease)
            assert retried.successor_lease is not None
            self.release_writer(retried.successor_lease)

    def test_checkpoint_source_measurement_failure_preserves_original_writer(
        self,
    ) -> None:
        observer = CaptureWorkspaceObserver()
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        self.store = WorkspaceStore.open(
            self.paths,
            expected_project_id=self.project_id,
            observer=observer,
        )
        with patch.object(
            self.store,
            "_checkpoint_source_database_bytes",
            side_effect=OSError(errno.EIO, "forced database measurement failure"),
        ):
            skipped = self.publish_checkpoint_source(lease, checkpoint)

        try:
            self.assertEqual(skipped.status, "snapshot_skipped_writer_preserved")
            self.assertEqual(
                skipped.capture_failure_code,
                "checkpoint_source_measurement_unavailable",
            )
            self.assertIsNone(skipped.database_bytes)
            self.assertEqual(skipped.preserved_lease, lease)
            self.assertEqual(
                self.store.read_metadata()["current_writer_epoch"],
                lease.epoch,
            )
            warnings = [
                event
                for event in observer.events
                if event.kind is EventKind.BACKUP_CHANGED
            ]
            self.assertEqual(len(warnings), 1)
            self.assertEqual(
                warnings[0].attributes["status"],
                "checkpoint_source_snapshot_skipped_writer_preserved",
            )
            self.assertEqual(
                warnings[0].attributes["reason_code"],
                "checkpoint_source_measurement_unavailable",
            )
            self.assertNotIn("byte_count", warnings[0].attributes)
        finally:
            self.release_writer(lease)

    def test_checkpoint_source_release_failure_requires_exact_writer_readback(
        self,
    ) -> None:
        observer = CaptureWorkspaceObserver()
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        self.store = WorkspaceStore.open(
            self.paths,
            expected_project_id=self.project_id,
            observer=observer,
        )
        with patch.object(
            self.store,
            "release_writer",
            side_effect=RuntimeError("forced pre-release failure"),
        ):
            skipped = self.publish_checkpoint_source(lease, checkpoint)
        try:
            self.assertEqual(skipped.status, "snapshot_skipped_writer_preserved")
            self.assertEqual(
                skipped.capture_failure_code,
                "checkpoint_source_writer_release_failed",
            )
            self.assertEqual(skipped.preserved_lease, lease)
            self.assertEqual(
                self.store.read_metadata()["current_writer_epoch"],
                lease.epoch,
            )
            warnings = [
                event
                for event in observer.events
                if event.kind is EventKind.BACKUP_CHANGED
            ]
            self.assertEqual(len(warnings), 1)
            self.assertEqual(
                warnings[0].attributes["status"],
                "checkpoint_source_snapshot_skipped_writer_preserved",
            )
            self.assertEqual(
                warnings[0].attributes["reason_code"],
                "checkpoint_source_writer_release_failed",
            )
            self.assertGreater(int(warnings[0].attributes["byte_count"]), 0)
        finally:
            self.release_writer(lease)

    def test_checkpoint_source_committed_release_failure_is_not_preserved(self) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        release_writer = self.store.release_writer

        def release_then_fail(*args, **kwargs):
            release_writer(*args, **kwargs)
            raise RuntimeError("forced ambiguous release response")

        with patch.object(
            self.store,
            "release_writer",
            side_effect=release_then_fail,
        ):
            with self.assertRaises(CheckpointSourceError) as captured:
                self.publish_checkpoint_source(lease, checkpoint)
        self.assertEqual(
            captured.exception.code,
            "checkpoint_source_writer_state_unverified",
        )
        metadata = self.store.read_metadata()
        self.assertEqual(metadata["lifecycle"], "quiesced")
        self.assertIsNone(metadata["current_writer_epoch"])

    def test_checkpoint_source_published_validation_failure_retains_publication(
        self,
    ) -> None:
        principal = attest_current_principal()
        lease, checkpoint = self.complete_checkpoint_and_keep_writer(
            writer_owner=stable_principal_owner_binding(principal)
        )
        with patch.object(
            self.store,
            "_require_checkpoint_source_safe_in_live_store",
            side_effect=CheckpointSourceError(
                "checkpoint_source_invalid",
                "forced published-source validation failure",
            ),
        ):
            with self.assertRaises(
                CheckpointSourceFailureWithSafeWriterError
            ) as captured:
                self.publish_checkpoint_source(lease, checkpoint)

        self.assertEqual(captured.exception.code, "checkpoint_source_invalid")
        self.assertIsNotNone(captured.exception.published_source)
        self.assertEqual(
            captured.exception.published_source.checkpoint_id,
            checkpoint.document["checkpoint_id"],
        )
        successor = self.store.reissue_writer_lease(principal)
        self.assertGreater(successor.epoch, lease.epoch)
        self.release_writer(successor)

    def test_checkpoint_source_ambiguous_successor_claim_reissues_exact_writer(
        self,
    ) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        claim_writer = self.store._claim_writer_without_checkpoint_source_lock

        def claim_then_fail(*args, **kwargs):
            claim_writer(*args, **kwargs)
            raise RuntimeError("forced successor acknowledgement failure")

        with patch.object(
            self.store,
            "_claim_writer_without_checkpoint_source_lock",
            side_effect=claim_then_fail,
        ):
            handoff = self.publish_checkpoint_source(lease, checkpoint)

        self.assertEqual(handoff.status, "published")
        self.assertIsNotNone(handoff.source)
        self.assertIsNotNone(handoff.successor_lease)
        assert handoff.successor_lease is not None
        self.assertEqual(handoff.successor_lease.epoch, lease.epoch + 1)
        self.assertEqual(
            self.store.read_metadata()["current_writer_epoch"],
            handoff.successor_lease.epoch,
        )
        self.release_writer(handoff.successor_lease)

    def test_checkpoint_source_capture_failure_survives_ambiguous_successor_claim(
        self,
    ) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        claim_writer = self.store._claim_writer_without_checkpoint_source_lock

        def claim_then_fail(*args, **kwargs):
            claim_writer(*args, **kwargs)
            raise RuntimeError("forced successor acknowledgement failure")

        def fail_capture(step, _path, _payload):
            if step == "after_checkpoint_source_quiesce":
                raise OSError(errno.EIO, "forced checkpoint-source capture failure")

        with patch.object(
            self.store,
            "_claim_writer_without_checkpoint_source_lock",
            side_effect=claim_then_fail,
        ):
            handoff = self.publish_checkpoint_source(
                lease,
                checkpoint,
                fault_hook=fail_capture,
            )

        self.assertEqual(handoff.status, "capture_failed_writer_reacquired")
        self.assertIsNone(handoff.source)
        self.assertEqual(
            handoff.capture_failure_code,
            "checkpoint_source_capture_failed",
        )
        self.assertIsNotNone(handoff.successor_lease)
        assert handoff.successor_lease is not None
        self.assertEqual(handoff.successor_lease.epoch, lease.epoch + 1)
        self.assertEqual(
            self.store.read_metadata()["current_writer_epoch"],
            handoff.successor_lease.epoch,
        )
        self.release_writer(handoff.successor_lease)

    def test_checkpoint_source_operation_teardown_failure_revalidates_writer(
        self,
    ) -> None:
        class FailingExitLease:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                raise OSError("forced checkpoint-source lease teardown failure")

        observer = CaptureWorkspaceObserver()
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        self.store = WorkspaceStore.open(
            self.paths,
            expected_project_id=self.project_id,
            observer=observer,
        )
        with patch(
            "research_core.workspace_store.acquire_kernel_lease",
            return_value=FailingExitLease(),
        ):
            handoff = self.publish_checkpoint_source(lease, checkpoint)
        try:
            self.assertEqual(handoff.status, "published")
            self.assertTrue(handoff.checkpoint_completed)
            self.assertIsNotNone(handoff.source)
            self.assertIsNotNone(handoff.successor_lease)
            self.assertEqual(
                handoff.operation_failure_code,
                "checkpoint_source_lock_unsafe",
            )
            self.assertIsNotNone(handoff.operation_failure_message)
            warning = [
                event
                for event in observer.events
                if event.kind is EventKind.BACKUP_CHANGED
            ]
            self.assertEqual(len(warning), 1)
            self.assertEqual(
                warning[0].attributes["status"],
                "checkpoint_source_operation_teardown_failed_writer_revalidated",
            )
            self.assertEqual(
                warning[0].attributes["reason_code"],
                "checkpoint_source_published",
            )
            self.assertEqual(
                self.store.read_metadata()["current_writer_epoch"],
                handoff.successor_lease.epoch,
            )
        finally:
            assert handoff.successor_lease is not None
            self.release_writer(handoff.successor_lease)

    def test_checkpoint_source_operation_teardown_requires_writer_reproof(
        self,
    ) -> None:
        class FailingExitLease:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                raise OSError("forced checkpoint-source lease teardown failure")

        principal = attest_current_principal()
        lease, checkpoint = self.complete_checkpoint_and_keep_writer(
            writer_owner=stable_principal_owner_binding(principal)
        )
        with (
            patch(
                "research_core.workspace_store.acquire_kernel_lease",
                return_value=FailingExitLease(),
            ),
            patch.object(
                self.store,
                "_checkpoint_source_active_writer_scope",
                side_effect=StaleWriterError("forced writer reproof failure"),
            ),
        ):
            with self.assertRaises(
                CheckpointSourceOperationTeardownError
            ) as captured:
                self.publish_checkpoint_source(lease, checkpoint)
        self.assertEqual(captured.exception.code, "checkpoint_source_lock_unsafe")
        self.assertIsNotNone(captured.exception.published_source)
        current = self.store.reissue_writer_lease(principal)
        self.release_writer(current)

    def test_checkpoint_source_capture_and_teardown_emit_one_exact_warning(
        self,
    ) -> None:
        class FailingExitLease:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                raise OSError("forced checkpoint-source lease teardown failure")

        observer = CaptureWorkspaceObserver()
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        self.store = WorkspaceStore.open(
            self.paths,
            expected_project_id=self.project_id,
            observer=observer,
        )

        def fail_capture(step, _path, _payload):
            if step == "after_checkpoint_source_quiesce":
                raise OSError(errno.EIO, "forced checkpoint-source capture failure")

        with patch(
            "research_core.workspace_store.acquire_kernel_lease",
            return_value=FailingExitLease(),
        ):
            handoff = self.publish_checkpoint_source(
                lease,
                checkpoint,
                fault_hook=fail_capture,
            )
        try:
            self.assertEqual(handoff.status, "capture_failed_writer_reacquired")
            self.assertIsNone(handoff.source)
            self.assertIsNotNone(handoff.successor_lease)
            self.assertEqual(
                handoff.capture_failure_code,
                "checkpoint_source_capture_failed",
            )
            self.assertEqual(
                handoff.operation_failure_code,
                "checkpoint_source_lock_unsafe",
            )
            warning = [
                event
                for event in observer.events
                if event.kind is EventKind.BACKUP_CHANGED
            ]
            self.assertEqual(len(warning), 1)
            self.assertEqual(
                warning[0].attributes["status"],
                "checkpoint_source_operation_teardown_failed_writer_revalidated",
            )
            self.assertEqual(
                warning[0].attributes["reason_code"],
                "checkpoint_source_capture_failed",
            )
        finally:
            assert handoff.successor_lease is not None
            self.release_writer(handoff.successor_lease)

    def test_checkpoint_source_integrity_and_teardown_preserve_both_failures(
        self,
    ) -> None:
        class FailingExitLease:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                raise OSError("forced checkpoint-source lease teardown failure")

        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        with (
            patch(
                "research_core.workspace_store.acquire_kernel_lease",
                return_value=FailingExitLease(),
            ),
            patch.object(
                self.store,
                "_require_checkpoint_source_safe_in_live_store",
                side_effect=CheckpointSourceError(
                    "checkpoint_source_invalid",
                    "forced published-source integrity failure",
                ),
            ),
        ):
            with self.assertRaises(
                CheckpointSourceFailureWithSafeWriterError
            ) as captured:
                self.publish_checkpoint_source(lease, checkpoint)

        failure = captured.exception
        self.assertEqual(failure.code, "checkpoint_source_invalid")
        self.assertIsNotNone(failure.published_source)
        self.assertTrue(failure.writer_reacquired)
        self.assertEqual(
            failure.operation_failure_code,
            "checkpoint_source_lock_unsafe",
        )
        self.assertIn("teardown", failure.operation_failure_message)
        self.assertEqual(
            self.store.read_metadata()["current_writer_epoch"],
            failure.successor_lease.epoch,
        )
        self.release_writer(failure.successor_lease)

    def test_checkpoint_source_writer_corruption_is_never_a_safe_failure(
        self,
    ) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()

        def corrupt_writer_then_reject(_source):
            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE writer_epoch SET row_digest = ? "
                    "WHERE epoch = (SELECT current_writer_epoch FROM "
                    "workspace_metadata WHERE singleton = 1)",
                    ("0" * 64,),
                )
                connection.execute("COMMIT")
            raise CheckpointSourceError(
                "checkpoint_source_invalid",
                "forced source rejection after writer corruption",
            )

        with patch.object(
            self.store,
            "_require_checkpoint_source_safe_in_live_store",
            side_effect=corrupt_writer_then_reject,
        ):
            with self.assertRaises(CheckpointSourceReacquisitionError) as captured:
                self.publish_checkpoint_source(lease, checkpoint)

        self.assertEqual(
            captured.exception.capture_failure_code,
            "checkpoint_source_invalid",
        )
        self.assertIsNotNone(captured.exception.published_source)

    def test_checkpoint_source_reacquisition_failure_is_fatal_after_checkpoint(
        self,
    ) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        with patch.object(
            self.store,
            "_claim_writer_without_checkpoint_source_lock",
            side_effect=StaleWriterError("forced reacquisition fault"),
        ):
            with self.assertRaises(CheckpointSourceReacquisitionError) as captured:
                self.publish_checkpoint_source(lease, checkpoint)
        self.assertTrue(captured.exception.published_source is not None)
        metadata = self.store.read_metadata()
        self.assertIsNone(metadata["current_writer_epoch"])
        self.assertEqual(metadata["lifecycle"], "quiesced")
        self.assertIsNotNone(
            self.store.read_continuation_checkpoint(executive_epoch_id=self.epoch_id)
        )

    @unittest.skipUnless(os.name == "posix", "requires POSIX replacement semantics")
    def test_checkpoint_source_stage_replacement_cannot_redirect_sqlite_backup(
        self,
    ) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        external = self.root / "external-stage-target.sqlite3"
        connection = sqlite3.connect(external, isolation_level=None)
        try:
            connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel(value) VALUES ('preserve')")
        finally:
            connection.close()
        original_digest = hashlib.sha256(external.read_bytes()).hexdigest()
        stage = self.paths.recovery / ".checkpoint-source-stage.sqlite3"
        detached = self.root / "detached-checkpoint-source-stage.sqlite3"
        original_connect = sqlite3.connect
        redirected_stage_opened = False

        def replace_stage_before_sqlite_open(step, path, _payload):
            if step == "after_checkpoint_source_stage_create_before_sqlite_open":
                path.replace(detached)
                os.link(external, path)

        def restore_stage_after_redirected_sqlite_open(database, *args, **kwargs):
            nonlocal redirected_stage_opened
            raw_database = os.fspath(database)
            redirected = (
                raw_database == os.fspath(stage)
                or stage.absolute().as_uri() in raw_database
            )
            connection = original_connect(database, *args, **kwargs)
            if redirected:
                redirected_stage_opened = True
                stage.unlink()
                detached.replace(stage)
            return connection

        failure = None
        try:
            with patch.object(
                sqlite3,
                "connect",
                side_effect=restore_stage_after_redirected_sqlite_open,
            ):
                with self.assertRaises(
                    CheckpointSourceFailureWithSafeWriterError
                ) as captured:
                    self.publish_checkpoint_source(
                        lease,
                        checkpoint,
                        fault_hook=replace_stage_before_sqlite_open,
                    )
            failure = captured.exception
            self.assertEqual(failure.code, "checkpoint_source_path_unsafe")
            self.assertIsNone(failure.published_source)
            self.assertTrue(failure.writer_reacquired)
            self.assertFalse(redirected_stage_opened)
            self.assertEqual(
                hashlib.sha256(external.read_bytes()).hexdigest(),
                original_digest,
            )
            verification = sqlite3.connect(external, isolation_level=None)
            try:
                self.assertEqual(
                    verification.execute("SELECT value FROM sentinel").fetchone()[0],
                    "preserve",
                )
            finally:
                verification.close()
        finally:
            if stage.exists() or stage.is_symlink():
                stage.unlink()
            if detached.exists():
                detached.unlink()
            if failure is not None:
                self.release_writer(failure.successor_lease)

    def test_checkpoint_source_rejects_hardlinks_before_claim_finalize_or_publish(
        self,
    ) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        first = self.publish_checkpoint_source(lease, checkpoint)
        assert first.source is not None
        current_lease = first.successor_lease
        latest_link = self.root / "hostile-latest-link.sqlite3"
        os.link(first.source.path, latest_link)
        try:
            with self.assertRaises(CheckpointSourceError) as linked_latest:
                self.store.claim_checkpoint_source_for_pending(
                    mission_id=self.mission_id,
                    claim_id=self.checkpoint_source_claim_id(first.source),
                )
            self.assertEqual(linked_latest.exception.code, "checkpoint_source_path_unsafe")
        finally:
            latest_link.unlink()
        claimed = self.store.claim_checkpoint_source_for_pending(
            mission_id=self.mission_id,
            claim_id=self.checkpoint_source_claim_id(first.source),
        )
        claimed_link = self.root / "hostile-claimed-link.sqlite3"
        claimed_mode = stat.S_IMODE(os.lstat(claimed.path).st_mode)
        os.link(claimed.path, claimed_link)
        try:
            with self.assertRaises(CheckpointSourceError) as linked_claimed:
                self.store.finalize_claimed_checkpoint_source(
                    mission_id=self.mission_id,
                    expected_source_incarnation=claimed.source_incarnation,
                    claim_id=str(claimed.claim_id),
                )
            self.assertEqual(linked_claimed.exception.code, "checkpoint_source_path_unsafe")
        finally:
            if os.name == "nt":
                os.chmod(claimed_link, claimed_mode | stat.S_IWUSR)
            claimed_link.unlink()
            if claimed.path.exists():
                os.chmod(claimed.path, claimed_mode)
        def interrupt_finalize(phase, _path, _source):
            if phase == "before_checkpoint_source_finalize_unlink":
                raise RuntimeError("forced finalize interruption")

        with self.assertRaisesRegex(RuntimeError, "forced finalize interruption"):
            self.store.finalize_claimed_checkpoint_source(
                mission_id=self.mission_id,
                expected_source_incarnation=claimed.source_incarnation,
                claim_id=str(claimed.claim_id),
                fault_hook=interrupt_finalize,
            )
        self.assertTrue(claimed.path.is_file())
        self.assertEqual(
            self.store.finalize_claimed_checkpoint_source(
                mission_id=self.mission_id,
                expected_source_incarnation=claimed.source_incarnation,
                claim_id=str(claimed.claim_id),
            ),
            "finalized",
        )
        stage_link = self.root / "hostile-stage-link.sqlite3"

        def link_stage(phase, path, _value):
            if phase == "after_checkpoint_source_copy_before_publish":
                os.link(path, stage_link)

        with self.assertRaises(
            CheckpointSourceFailureWithSafeWriterError
        ) as unsafe_source:
            self.publish_checkpoint_source(
                current_lease,
                checkpoint,
                fault_hook=link_stage,
            )
        try:
            self.assertEqual(
                unsafe_source.exception.code,
                "checkpoint_source_path_unsafe",
            )
            self.assertIsNotNone(unsafe_source.exception.successor_lease)
        finally:
            if stage_link.exists():
                stage_link.unlink()
            self.release_writer(unsafe_source.exception.successor_lease)

    def test_portable_restore_revalidates_without_original_workspace_paths(self) -> None:
        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup()
        )
        portable = self.portable_copy(backup, "portable-current-backup")
        restore_parent = self.root / "portable-restore-owner"
        restore_parent.mkdir()
        restored = prepare_portable_side_by_side_restore(
            backup_directory=portable,
            restore_parent=restore_parent.resolve(),
            authority_repo_root=REPO_ROOT,
        )
        self.addCleanup(
            _remove_tree,
            restored.root,
            owned_parent=restore_parent,
            ignore_errors=True,
        )
        self.release_writer(successor)
        _remove_tree(self.paths.root, owned_parent=self.root)

        verify_restored_workspace(restored)
        self.assertTrue(restored.source_materialization_portable)
        self.assertTrue((restored.root / "workspace.sqlite3").is_file())
        self.assertTrue((portable / "manifest.json").is_file())

    def test_portable_fixed_target_restore_uses_authenticated_installed_release(
        self,
    ) -> None:
        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup()
        )
        portable = self.portable_copy(backup, "portable-fixed-target-backup")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / "fixed-target-owner"
        target_parent.mkdir()
        target = target_parent / "successor-workspace"
        restored = prepare_portable_fixed_target_restore(
            backup_directory=portable,
            target_root=target.resolve(strict=False),
            installed_release_root=release_root,
            executing_release_sha=release_sha,
        )
        self.addCleanup(
            _remove_tree,
            restored.root,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        try:
            self.assertEqual(restored.root, target.resolve())
            self.assertTrue(restored.caller_selected_target)
            self.assertTrue(restored.source_materialization_portable)
            verify_restored_workspace(restored)
            with self.assertRaises(RecoveryError) as collision:
                prepare_portable_fixed_target_restore(
                    backup_directory=portable,
                    target_root=target.resolve(),
                    installed_release_root=release_root,
                    executing_release_sha=release_sha,
                )
            self.assertEqual(collision.exception.code, "restore_path_collision")
        finally:
            self.release_writer(successor)

    def test_staging_import_rebinds_only_physical_and_writer_state(self) -> None:
        import research_core.workspace_recovery as recovery_module

        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup()
        )
        portable = self.portable_copy(backup, "portable-staging-backup")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / "rh-staging" / "workspaces"
        target_parent.mkdir(parents=True)
        target = (target_parent / self.mission_id).resolve(strict=False)
        self.addCleanup(
            _remove_tree,
            target,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        source_files_before = {
            item.relative_to(portable).as_posix(): digest(item.read_bytes())
            for item in portable.rglob("*")
            if item.is_file()
        }
        source_mission = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, self.mission_id)
        )
        assert source_mission is not None
        try:
            with patch.object(
                recovery_module, "_verify_recovery_snapshot_semantics",
                wraps=recovery_module._verify_recovery_snapshot_semantics,
            ) as source_audit, patch.object(
                WorkspaceStore, "verify_integrity", autospec=True,
                side_effect=WorkspaceStore.verify_integrity,
            ) as store_audit, patch.object(
                recovery_module, "_sqlite_integrity",
                side_effect=AssertionError("pinned staging copy repeated SQLite integrity"),
            ) as sqlite_audit, patch.object(
                recovery_module, "_load_persisted_backup_evidence",
                side_effect=AssertionError("pinned staging copy repeated persisted Evidence audit"),
            ) as evidence_audit:
                imported = import_portable_backup_to_staging(
                    backup_directory=portable,
                    target_root=target,
                    installed_release_root=release_root,
                    installed_release_sha=release_sha,
                    mission_id=self.mission_id,
                    expected_backup_id=backup.manifest.backup_id,
                    expected_manifest_sha256=backup.manifest.manifest_sha256,
                )
            # The independent request pin retains the earned source audit;
            # copying and the bounded writer/root rebind do not change science.
            source_audit.assert_not_called()
            store_audit.assert_not_called()
            sqlite_audit.assert_not_called()
            evidence_audit.assert_not_called()
            self.assertIsInstance(imported, StagingPortableImportReport)
            self.assertEqual(imported.status, "staging_imported_quiesced")
            self.assertEqual(imported.mission_id, self.mission_id)
            self.assertEqual(imported.project_id, self.project_id)
            self.assertEqual(imported.backup_id, backup.manifest.backup_id)
            self.assertEqual(
                imported.source_manifest_sha256,
                backup.manifest.manifest_sha256,
            )
            self.assertEqual(
                imported.source_database_sha256,
                backup.manifest.sqlite_sha256,
            )
            self.assertEqual(
                imported.source_root_identity,
                backup.manifest.store.root_identity,
            )
            self.assertNotEqual(
                imported.target_root_identity,
                imported.source_root_identity,
            )
            self.assertEqual(
                imported.project_commit,
                backup.manifest.store.project_commit_id,
            )
            self.assertEqual(
                imported.project_root_digest,
                backup.manifest.store.project_root_digest,
            )
            self.assertEqual(
                imported.transition_head,
                backup.manifest.store.transition_head,
            )
            self.assertEqual(
                imported.canonical_authority_digest,
                backup.manifest.store.canonical_authority_digest,
            )
            self.assertEqual(imported.writer_lifecycle, "quiesced")
            self.assertEqual(imported.mission_mutations, 0)
            self.assertFalse(imported.provider_effect)
            self.assertEqual(imported.canonical_effect, "none")
            self.assertTrue(imported.source_material_unchanged)
            self.assertTrue(imported.cas_verification_passed)
            self.assertTrue(imported.structural_verification_passed)
            self.assertTrue(imported.semantic_verification_passed)
            json.dumps(imported.to_payload())

            target_paths = WorkspacePaths.from_root(target)
            staging_store = WorkspaceStore.open(
                target_paths,
                expected_project_id=self.project_id,
            )
            integrity = staging_store.verify_integrity()
            metadata = staging_store.read_metadata()
            target_mission = staging_store.get_head(
                TypedWorkspaceId(IdentityKind.MISSION, self.mission_id)
            )
            assert target_mission is not None
            self.assertEqual(target_mission.payload, source_mission.payload)
            self.assertEqual(
                target_mission.payload_digest,
                source_mission.payload_digest,
            )
            self.assertEqual(metadata["root_identity"], target_paths.root_identity)
            self.assertEqual(metadata["current_writer_epoch"], None)
            self.assertEqual(metadata["lifecycle"], "quiesced")
            self.assertEqual(
                integrity.current_project_commit,
                backup.manifest.store.project_commit_id,
            )
            self.assertEqual(
                integrity.current_root_digest,
                backup.manifest.store.project_root_digest,
            )
            self.assertFalse((target / ".takeover-unseal.json").exists())
            self.assertFalse((target / "read_only_seal.json").exists())
            self.assertTrue(target_paths.staging.is_dir())
            self.assertTrue(target_paths.backups.is_dir())
            self.assertTrue(target_paths.recovery.is_dir())

            with patch.object(
                recovery_module, "_verify_recovery_snapshot_semantics",
                wraps=recovery_module._verify_recovery_snapshot_semantics,
            ) as replay_source_audit, patch.object(
                WorkspaceStore, "verify_integrity", autospec=True,
                side_effect=WorkspaceStore.verify_integrity,
            ) as replay_store_audit, patch.object(
                recovery_module, "_sqlite_integrity",
                side_effect=AssertionError("pinned staging replay repeated SQLite integrity"),
            ) as replay_sqlite_audit, patch.object(
                recovery_module, "_load_persisted_backup_evidence",
                side_effect=AssertionError("pinned staging replay repeated persisted Evidence audit"),
            ) as replay_evidence_audit:
                replay = import_portable_backup_to_staging(
                    backup_directory=portable,
                    target_root=target,
                    installed_release_root=release_root,
                    installed_release_sha=release_sha,
                    mission_id=self.mission_id,
                    expected_backup_id=backup.manifest.backup_id,
                    expected_manifest_sha256=backup.manifest.manifest_sha256,
                )
            # Replay rechecks the exact pin, bytes, custody and writer binding,
            # not the unchanged mathematical history.
            replay_source_audit.assert_not_called()
            replay_store_audit.assert_not_called()
            replay_sqlite_audit.assert_not_called()
            replay_evidence_audit.assert_not_called()
            self.assertEqual(replay.to_payload(), imported.to_payload())
            self.assertEqual(
                {
                    item.relative_to(portable).as_posix(): digest(item.read_bytes())
                    for item in portable.rglob("*")
                    if item.is_file()
                },
                source_files_before,
            )

            metadata = staging_store.read_metadata()
            lease = staging_store.claim_writer(
                owner=imported.writer_owner,
                creation_basis="staging runtime fixture",
                expected_project_commit=int(metadata["current_project_commit"]),
                expected_root_digest=str(metadata["current_root_digest"]),
                expected_canonical_authority_digest=str(
                    metadata["canonical_authority_digest"]
                ),
            )
            after_claim = staging_store.read_metadata()
            staging_store.release_writer(
                lease,
                expected_project_commit=int(after_claim["current_project_commit"]),
                expected_root_digest=str(after_claim["current_root_digest"]),
                expected_canonical_authority_digest=str(
                    after_claim["canonical_authority_digest"]
                ),
            )
        finally:
            self.release_writer(successor)

    def test_staging_import_rejects_legacy_unprefixed_project_identity(
        self,
    ) -> None:
        legacy = WorkspaceRecoveryTests(methodName="runTest")
        legacy.fixture_project_id = "riemann_hypothesis"
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        backup = legacy.create_backup("legacy-project-id-staging-source")
        portable = legacy.portable_copy(
            backup,
            "portable-legacy-project-id-staging-source",
        )
        release_root, release_sha = legacy.installed_release_fixture()
        target_parent = legacy.root / "rh-staging" / "workspaces"
        target_parent.mkdir(parents=True)

        with self.assertRaises(RecoveryError) as captured:
            import_portable_backup_to_staging(
                backup_directory=portable,
                target_root=target_parent / legacy.mission_id,
                installed_release_root=release_root,
                installed_release_sha=release_sha,
                mission_id=legacy.mission_id,
                expected_backup_id=backup.manifest.backup_id,
                expected_manifest_sha256=backup.manifest.manifest_sha256,
            )

        self.assertEqual(captured.exception.code, "staging_project_mismatch")

    def test_staging_import_rejects_production_path_and_provider_environment(
        self,
    ) -> None:
        production_parent = (
            self.root / "production" / "rh-staging" / "workspaces"
        )
        production_parent.mkdir(parents=True)
        with self.assertRaises(RecoveryError) as production:
            import_portable_backup_to_staging(
                backup_directory=self.root / "must-not-be-read",
                target_root=production_parent / self.mission_id,
                installed_release_root=self.root / "must-not-be-read-release",
                installed_release_sha="f" * 40,
                mission_id=self.mission_id,
                expected_backup_id="backup-1",
                expected_manifest_sha256="0" * 64,
            )
        self.assertEqual(production.exception.code, "staging_path_invalid")

        staging_parent = self.root / "rh-staging" / "workspaces"
        staging_parent.mkdir(parents=True)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-never-read"}):
            with self.assertRaises(RecoveryError) as environment:
                import_portable_backup_to_staging(
                    backup_directory=self.root / "must-not-be-read",
                    target_root=staging_parent / self.mission_id,
                    installed_release_root=self.root / "must-not-be-read-release",
                    installed_release_sha="f" * 40,
                    mission_id=self.mission_id,
                    expected_backup_id="backup-1",
                    expected_manifest_sha256="0" * 64,
                )
        self.assertEqual(
            environment.exception.code,
            "staging_credential_environment_forbidden",
        )
        with patch.dict(os.environ, {"RH_ENVIRONMENT": "production"}):
            with self.assertRaises(RecoveryError) as production_environment:
                import_portable_backup_to_staging(
                    backup_directory=self.root / "must-not-be-read",
                    target_root=staging_parent / self.mission_id,
                    installed_release_root=self.root / "must-not-be-read-release",
                    installed_release_sha="f" * 40,
                    mission_id=self.mission_id,
                    expected_backup_id="backup-1",
                    expected_manifest_sha256="0" * 64,
                )
        self.assertEqual(
            production_environment.exception.code,
            "staging_environment_forbidden",
        )
        with self.assertRaises(RecoveryError) as observation_environment:
            import_portable_backup_to_staging(
                backup_directory=self.root / "must-not-be-read",
                target_root=staging_parent / self.mission_id,
                installed_release_root=self.root / "must-not-be-read-release",
                installed_release_sha="f" * 40,
                mission_id=self.mission_id,
                expected_backup_id="backup-1",
                expected_manifest_sha256="0" * 64,
                observation_environment=ObservationEnvironment.PROD,
            )
        self.assertEqual(
            observation_environment.exception.code,
            "staging_environment_forbidden",
        )
        self.assertFalse((staging_parent / self.mission_id).exists())

    def test_staging_import_replays_after_committed_completion_failure(self) -> None:
        import research_core.workspace_recovery as recovery_module

        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup()
        )
        portable = self.portable_copy(backup, "portable-staging-replay")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / "rh-staging" / "workspaces"
        target_parent.mkdir(parents=True)
        target = (target_parent / self.mission_id).resolve(strict=False)
        self.addCleanup(
            _remove_tree,
            target,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        arguments = {
            "backup_directory": portable,
            "target_root": target,
            "installed_release_root": release_root,
            "installed_release_sha": release_sha,
            "mission_id": self.mission_id,
            "expected_backup_id": backup.manifest.backup_id,
            "expected_manifest_sha256": backup.manifest.manifest_sha256,
        }
        try:
            with patch(
                "research_core.workspace_recovery."
                "_normalize_staging_database_profile",
                side_effect=RuntimeError("forced staging completion failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "forced staging completion failure",
                ):
                    import_portable_backup_to_staging(**arguments)
            marker = target / ".takeover-unseal.json"
            self.assertTrue(marker.is_file())
            with self.assertRaises(WorkspaceIntegrityError):
                WorkspaceStore.open(
                    WorkspacePaths.from_root(target),
                    expected_project_id=self.project_id,
                )

            original_files = {
                name: (portable / name).read_bytes()
                for name in ("workspace.sqlite3", "manifest.json")
            }
            normalize = recovery_module._normalize_staging_database_profile
            for tamper, expected_codes in (
                ("database", {"backup_sqlite_corrupt"}),
                ("protection", {"backup_evidence_mutable", "recovery_tree_mutable", "restore_not_physically_read_only"}),
                ("rebound_manifest", {"staging_source_changed"}),
            ):
                with self.subTest(late_import_source_change=tamper):
                    def change_after_source_audit(paths):
                        for suffix in ("-wal", "-shm"):
                            sidecar = paths.database.with_name(paths.database.name + suffix)
                            if sidecar.exists():
                                self.assertTrue(stat.S_IMODE(sidecar.stat().st_mode) & stat.S_IWUSR,
                                    "exact replay must unseal its existing SQLite sidecars before checkpointing")
                        normalize(paths)
                        self.unlock_fixture_tree(portable)
                        database = portable / "workspace.sqlite3"
                        os.chmod(database, stat.S_IMODE(database.stat().st_mode) | stat.S_IWUSR)
                        if tamper == "database":
                            database.write_bytes(original_files["workspace.sqlite3"] + b"changed")
                        elif tamper == "rebound_manifest":
                            self.rewrite_manifest(
                                portable, lambda payload: payload.update(created_at="2026-01-01T00:00:00Z"),
                            )
                        if tamper != "protection":
                            _protect_closed_tree(portable, deny_delete=False)

                    try:
                        with patch.object(
                            recovery_module, "_normalize_staging_database_profile",
                            side_effect=change_after_source_audit,
                        ), patch.object(
                            recovery_module, "_verify_recovery_snapshot_semantics",
                            wraps=recovery_module._verify_recovery_snapshot_semantics,
                        ) as source_audit:
                            with self.assertRaises(RecoveryError) as rejected:
                                import_portable_backup_to_staging(**arguments)
                        source_audit.assert_not_called()
                        self.assertIn(rejected.exception.code, expected_codes)
                        self.assertTrue(marker.is_file())
                        for suffix in ("-wal", "-shm"):
                            sidecar = target / ("workspace.sqlite3" + suffix)
                            if sidecar.exists():
                                self.assertFalse(stat.S_IMODE(sidecar.stat().st_mode) & stat.S_IWUSR,
                                    "failed completion must contain existing SQLite sidecars")
                    finally:
                        self.unlock_fixture_tree(portable)
                        for name, raw in original_files.items():
                            path = portable / name
                            os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
                            path.write_bytes(raw)
                        _protect_closed_tree(portable, deny_delete=False)

            replay = import_portable_backup_to_staging(**arguments)
            self.assertEqual(replay.status, "staging_imported_quiesced")
            self.assertEqual(
                replay.writer_epoch,
                backup.manifest.store.writer_epoch_high_watermark + 1,
            )
            self.assertFalse(marker.exists())
            WorkspaceStore.open(
                WorkspacePaths.from_root(target),
                expected_project_id=self.project_id,
            ).verify_integrity()
        finally:
            self.release_writer(successor)

    def test_staging_import_holds_store_gate_through_final_publication(self) -> None:
        import research_core.workspace_recovery as recovery_module

        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup()
        )
        portable = self.portable_copy(backup, "portable-staging-gate")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / "rh-staging" / "workspaces"
        target_parent.mkdir(parents=True)
        target = (target_parent / self.mission_id).resolve(strict=False)
        self.addCleanup(
            _remove_tree,
            target,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        original_fsync = recovery_module._fsync_directory
        gate_observations: list[str] = []

        def observe_final_publication(path: Path) -> None:
            original_fsync(path)
            marker = target / ".takeover-unseal.json"
            if (
                Path(path) == target
                and (target / "workspace.sqlite3").is_file()
                and not marker.exists()
                and not (target / "read_only_seal.json").exists()
            ):
                competing = WorkspaceStore.open(
                    WorkspacePaths.from_root(target),
                    expected_project_id=self.project_id,
                )
                metadata = competing.read_metadata()
                with self.assertRaises(CheckpointSourceError) as blocked:
                    competing.claim_writer(
                        owner=stable_principal_owner_binding(attest_current_principal()),
                        creation_basis="must remain excluded during publication",
                        expected_project_commit=int(metadata["current_project_commit"]),
                        expected_root_digest=str(metadata["current_root_digest"]),
                        expected_canonical_authority_digest=str(
                            metadata["canonical_authority_digest"]
                        ),
                    )
                gate_observations.append(blocked.exception.code)

        try:
            with patch(
                "research_core.workspace_recovery._fsync_directory",
                side_effect=observe_final_publication,
            ):
                imported = import_portable_backup_to_staging(
                    backup_directory=portable,
                    target_root=target,
                    installed_release_root=release_root,
                    installed_release_sha=release_sha,
                    mission_id=self.mission_id,
                    expected_backup_id=backup.manifest.backup_id,
                    expected_manifest_sha256=backup.manifest.manifest_sha256,
                )
            self.assertEqual(imported.status, "staging_imported_quiesced")
            self.assertEqual(gate_observations, ["checkpoint_source_busy"])
        finally:
            self.release_writer(successor)

    def test_staging_import_recovers_exact_abandoned_rollback_journal(self) -> None:
        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup()
        )
        portable = self.portable_copy(backup, "portable-staging-journal")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / "rh-staging" / "workspaces"
        target_parent.mkdir(parents=True)
        target = (target_parent / self.mission_id).resolve(strict=False)
        self.addCleanup(
            _remove_tree,
            target,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(PACKAGE_ROOT),
                str(WORKSTATION_PACKAGES_ROOT),
                environment.get("PYTHONPATH", ""),
            )
        )
        crash_child = r'''
import os
import sys
from pathlib import Path

import research_core.workspace_recovery as recovery

real_connect = recovery.sqlite3.connect

class CrashConnection:
    def __init__(self, connection):
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def execute(self, statement, *args):
        if statement.lstrip().startswith("INSERT INTO writer_epoch"):
            self._connection.execute("PRAGMA cache_size = 1")
        result = self._connection.execute(statement, *args)
        if statement.lstrip().startswith("INSERT INTO writer_epoch"):
            os._exit(86)
        return result

def crash_connect(*args, **kwargs):
    return CrashConnection(real_connect(*args, **kwargs))

original_commit = recovery._commit_staging_root_rebind

def crash_commit(**kwargs):
    recovery.sqlite3.connect = crash_connect
    return original_commit(**kwargs)

recovery._commit_staging_root_rebind = crash_commit
recovery.import_portable_backup_to_staging(
    backup_directory=Path(sys.argv[1]),
    target_root=Path(sys.argv[2]),
    installed_release_root=Path(sys.argv[3]),
    installed_release_sha=sys.argv[4],
    mission_id=sys.argv[5],
    expected_backup_id=sys.argv[6],
    expected_manifest_sha256=sys.argv[7],
)
'''
        try:
            interrupted = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    crash_child,
                    str(portable),
                    str(target),
                    str(release_root),
                    release_sha,
                    self.mission_id,
                    backup.manifest.backup_id,
                    backup.manifest.manifest_sha256,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
            self.assertTrue((target / ".takeover-unseal.json").is_file())
            self.assertTrue((target / "workspace.sqlite3-journal").is_file())

            replay = import_portable_backup_to_staging(
                backup_directory=portable,
                target_root=target,
                installed_release_root=release_root,
                installed_release_sha=release_sha,
                mission_id=self.mission_id,
                expected_backup_id=backup.manifest.backup_id,
                expected_manifest_sha256=backup.manifest.manifest_sha256,
            )
            self.assertEqual(replay.status, "staging_imported_quiesced")
            self.assertFalse((target / ".takeover-unseal.json").exists())
            self.assertFalse((target / "workspace.sqlite3-journal").exists())
            WorkspaceStore.open(
                WorkspacePaths.from_root(target),
                expected_project_id=self.project_id,
            ).verify_integrity()
        finally:
            self.release_writer(successor)

    def _crash_fixed_target_restore(
        self,
        *,
        portable: Path,
        target: Path,
        release_root: Path,
        release_sha: str,
        stage: Path,
        phase: str,
    ) -> subprocess.CompletedProcess[str]:
        script = r'''
import os
import sys
from pathlib import Path

import research_core.workspace_recovery as recovery

phase = sys.argv[1]
stage = Path(sys.argv[6])
if phase == "copy":
    real_copy = recovery._copy_verified
    def crash_copy(*args, **kwargs):
        result = real_copy(*args, **kwargs)
        os._exit(86)
    recovery._copy_verified = crash_copy
elif phase == "seal":
    real_protect = recovery._protect_closed_tree
    def crash_protect(root, *args, **kwargs):
        root = Path(root)
        if root == stage:
            marker = root / recovery.TAKEOVER_UNSEAL_MARKER
            if not marker.is_file():
                os._exit(91)
            protected = next(
                item for item in root.rglob("*")
                if item.is_file() and item != marker
            )
            recovery._make_owner_read_only(protected)
            os._exit(87)
        return real_protect(root, *args, **kwargs)
    recovery._protect_closed_tree = crash_protect
else:
    raise SystemExit(92)

recovery.prepare_portable_fixed_target_restore(
    backup_directory=Path(sys.argv[2]),
    target_root=Path(sys.argv[3]),
    installed_release_root=Path(sys.argv[4]),
    executing_release_sha=sys.argv[5],
)
'''
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(PACKAGE_ROOT),
                str(WORKSTATION_PACKAGES_ROOT),
                environment.get("PYTHONPATH", ""),
            )
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                phase,
                str(portable),
                str(target),
                str(release_root),
                release_sha,
                str(stage),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def _assert_fixed_restore_hard_crash_replays(self, phase: str, exit_code: int) -> None:
        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup()
        )
        portable = self.portable_copy(backup, f"portable-fixed-{phase}-crash")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / f"fixed-{phase}-crash-owner"
        target_parent.mkdir()
        target = (target_parent / "successor-workspace").resolve(strict=False)
        stage, lock = _fixed_target_restore_artifacts(target)
        self.addCleanup(
            _remove_tree,
            target,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        try:
            interrupted = self._crash_fixed_target_restore(
                portable=portable,
                target=target,
                release_root=release_root,
                release_sha=release_sha,
                stage=stage,
                phase=phase,
            )
            self.assertEqual(interrupted.returncode, exit_code, interrupted.stderr)
            self.assertFalse(target.exists())
            self.assertTrue(stage.is_dir())
            state = json.loads(lock.read_text(encoding="utf-8"))
            self.assertEqual(
                state["format"],
                "research-portable-fixed-target-restore-lock-v1",
            )
            self.assertEqual(state["lifecycle"], "held")
            self.assertEqual(state["backup_manifest_sha256"], backup.manifest.manifest_sha256)
            self.assertEqual(
                state["target_root"],
                os.path.normcase(str(target.absolute())),
            )

            restored = prepare_portable_fixed_target_restore(
                backup_directory=portable,
                target_root=target,
                installed_release_root=release_root,
                executing_release_sha=release_sha,
            )
            self.assertEqual(restored.root, target)
            self.assertFalse(stage.exists())
            self.assertTrue((target / ".takeover-unseal.json").is_file())
            verify_restored_workspace(restored)
        finally:
            self.release_writer(successor)

    def test_fixed_target_restore_replays_after_hard_copy_crash(self) -> None:
        self._assert_fixed_restore_hard_crash_replays("copy", 86)

    def test_fixed_target_restore_replays_after_hard_seal_crash(self) -> None:
        self._assert_fixed_restore_hard_crash_replays("seal", 87)

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlink semantics")
    def test_fixed_target_owner_rejects_posix_target_and_parent_aliases(self) -> None:
        external = self.root / "fixed-target-external"
        external.mkdir()
        sentinel = external / "preserved.txt"
        sentinel.write_bytes(b"preserve-external-target")
        aliases = (
            (self.root / "fixed-target-leaf-link", True),
            (self.root / "fixed-target-parent-link", False),
        )
        for alias, leaf in aliases:
            with self.subTest(alias_kind="leaf" if leaf else "parent"):
                try:
                    alias.symlink_to(external, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f"symlink creation unavailable: {exc}")
                try:
                    target = alias if leaf else alias / "successor-workspace"
                    with self.assertRaises(RecoveryError) as captured:
                        activate_portable_checkpoint_source_takeover_from_owner(
                            source_unit="research-fixture.service",
                            backup_directory=self.root / "must-not-be-read",
                            target_root=target,
                            installed_release_root=self.root / "must-not-be-read-release",
                            executing_release_sha="f" * 40,
                            expected_manifest_sha256="0" * 64,
                            mission_id=self.mission_id,
                            command_id="reject.posix.target-alias",
                        )
                    self.assertEqual(captured.exception.code, "restore_path_invalid")
                    self.assertEqual(
                        sentinel.read_bytes(), b"preserve-external-target"
                    )
                    self.assertFalse((external / "successor-workspace").exists())
                finally:
                    alias.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == "nt", "requires a native Windows junction")
    def test_fixed_target_owner_rejects_windows_target_and_parent_reparse(self) -> None:
        external = self.root / "fixed-target-external"
        external.mkdir()
        sentinel = external / "preserved.txt"
        sentinel.write_bytes(b"preserve-external-target")
        aliases = (
            (self.root / "fixed-target-leaf-junction", True),
            (self.root / "fixed-target-parent-junction", False),
        )
        for alias, leaf in aliases:
            with self.subTest(alias_kind="leaf" if leaf else "parent"):
                created = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "New-Item -ItemType Junction -Path '"
                        + str(alias).replace("'", "''")
                        + "' -Target '"
                        + str(external).replace("'", "''")
                        + "' | Out-Null",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                try:
                    self.assertTrue(
                        alias.lstat().st_file_attributes
                        & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    )
                    target = alias if leaf else alias / "successor-workspace"
                    with self.assertRaises(RecoveryError) as captured:
                        activate_portable_checkpoint_source_takeover_from_owner(
                            source_unit="research-fixture.service",
                            backup_directory=self.root / "must-not-be-read",
                            target_root=target,
                            installed_release_root=self.root / "must-not-be-read-release",
                            executing_release_sha="f" * 40,
                            expected_manifest_sha256="0" * 64,
                            mission_id=self.mission_id,
                            command_id="reject.windows.target-alias",
                        )
                    self.assertEqual(captured.exception.code, "restore_path_invalid")
                    self.assertEqual(
                        sentinel.read_bytes(), b"preserve-external-target"
                    )
                    self.assertFalse((external / "successor-workspace").exists())
                finally:
                    # Remove only the junction entry; never recurse into its target.
                    alias.rmdir()

    def test_trusted_source_control_verification_cannot_be_caller_issued(self) -> None:
        with self.assertRaises(ValueError):
            TrustedSourceControlVerification(
                mission_id=self.mission_id,
                source_incarnation="a" * 64,
                source_root_identity="b" * 64,
                backup_manifest_sha256="c" * 64,
                prior_writer_epoch=1,
                prior_writer_owner="os-principal-owner:sha256:" + "d" * 64,
                source_unit="research-fixture.service",
                source_unit_user="research-fixture",
                source_unit_enablement="disabled",
                source_unit_activity="inactive",
                source_unit_control_group=(
                    "/system.slice/research-fixture.service"
                ),
                source_unit_tasks_current=0,
                write_exclusion_source_incarnation="a" * 64,
                physical_write_exclusion_method=(
                    "posix_service_principal_tree_write_denied"
                ),
                physical_write_exclusion_target=str(self.paths.root),
                physical_write_exclusion_result=(
                    "old_incarnation_cannot_start_or_write"
                ),
                verification_reference="e" * 64,
                verified_at="2026-09-10T00:00:00Z",
            )

    def test_source_control_readback_requires_an_explicit_exact_service(self) -> None:
        # A missing or malformed selector must fail before any host command.
        with patch("research_core.workspace_recovery.subprocess.run") as run:
            with self.assertRaises(TypeError):
                _closed_source_unit_readback()
            for value in (
                None,
                "",
                "research-fixture",
                "../research-fixture.service",
                "*.service",
                "research-fixture.service --all",
            ):
                with self.subTest(source_unit=value), self.assertRaisesRegex(
                    ValueError, "one explicit exact systemd service name"
                ):
                    _closed_source_unit_readback(source_unit=value)
            run.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "requires POSIX systemd semantics")
    def test_source_control_readback_accepts_only_both_real_empty_cgroup_forms(
        self,
    ) -> None:
        prefix = (
            "LoadState=loaded\n"
            "UnitFileState=disabled\n"
            "ActiveState=inactive\n"
            "SubState=dead\n"
            "MainPID=0\n"
            "User=research-fixture\n"
        )
        outputs = (
            prefix
            + "ControlGroup=/system.slice/research-fixture.service\n"
            + "TasksCurrent=0\n",
            prefix + "ControlGroup=\nTasksCurrent=[not set]\n",
        )
        for stdout in outputs:
            with self.subTest(stdout=stdout), patch(
                "research_core.workspace_recovery.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=stdout, stderr=""
                ),
            ):
                observed = _closed_source_unit_readback(source_unit="research-fixture.service")
                self.assertEqual(observed["User"], "research-fixture")
        for stdout in (
            prefix + "ControlGroup=\nTasksCurrent=0\n",
            prefix
            + "ControlGroup=/system.slice/research-fixture.service\n"
            + "TasksCurrent=[not set]\n",
        ):
            with self.subTest(crossed=stdout), patch(
                "research_core.workspace_recovery.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=stdout, stderr=""
                ),
            ), self.assertRaises(RecoveryError):
                _closed_source_unit_readback(source_unit="research-fixture.service")

    def test_legacy_takeover_preserves_distinct_fenced_predecessor_owner(self) -> None:
        backup = self.create_backup("legacy-distinct-predecessor")
        restored = prepare_side_by_side_restore(
            backup=backup,
            authority_repo_root=REPO_ROOT,
        )
        successor_owner = stable_principal_owner_binding(
            attest_current_principal()
        )
        # Persisted writer-fence validation has its own negative integration test;
        # this case isolates the legacy actor/prior-owner compatibility boundary.
        with patch(
            "research_core.workspace_recovery._verify_persisted_writer_fence",
            return_value=(1, "source-writer"),
        ) as verify_fence:
            plan = prepare_takeover(
                restored,
                fence_proof=self.fence_reference,
            )
            self.assertNotEqual(plan.prior_owner, successor_owner)
            permit = confirm_explicit_takeover(
                plan,
                restored=restored,
                command_id="takeover.legacy.distinct-predecessor",
                actor=successor_owner,
                writer_creation_basis="legacy fenced recovery",
                expected_manifest_sha256=backup.manifest.manifest_sha256,
                authority_repo_root=REPO_ROOT,
            )
        self.assertEqual(verify_fence.call_count, 2)
        self.assertEqual(permit.actor, successor_owner)
        self.assertEqual(permit.plan.prior_owner, "source-writer")

    def test_takeover_cleanup_faults_fail_closed_release_scope_and_replay(
        self,
    ) -> None:
        import research_core.workspace_recovery as recovery_module

        successor_owner = stable_principal_owner_binding(
            attest_current_principal()
        )

        def prepare_case(label: str):
            backup = self.create_backup(f"takeover-cleanup-{label}")
            restored = prepare_side_by_side_restore(
                backup=backup,
                authority_repo_root=REPO_ROOT,
            )
            with patch(
                "research_core.workspace_recovery._verify_persisted_writer_fence",
                return_value=(1, "source-writer"),
            ):
                plan = prepare_takeover(
                    restored,
                    fence_proof=self.fence_reference,
                )
                permit = confirm_explicit_takeover(
                    plan,
                    restored=restored,
                    command_id=f"takeover.cleanup.{label}",
                    actor=successor_owner,
                    writer_creation_basis="legacy fenced recovery",
                    expected_manifest_sha256=backup.manifest.manifest_sha256,
                    authority_repo_root=REPO_ROOT,
                )
            paths = WorkspacePaths.from_root(restored.root)
            store = WorkspaceStore.open_takeover_target(
                paths,
                permit,
                expected_project_id=self.project_id,
            )
            return backup, paths, store, permit

        def activate(store, permit, cut):
            return store.activate_takeover(
                permit,
                creation_basis="legacy fenced recovery",
                expected_project_commit=cut.project_commit_id,
                expected_root_digest=cut.project_root_digest,
                expected_canonical_authority_digest=(
                    cut.canonical_authority_digest
                ),
            )

        def assert_failure_fenced(paths, store, *, marker_expected: bool) -> None:
            marker = paths.root / ".takeover-unseal.json"
            seal = paths.root / "read_only_seal.json"
            self.assertEqual(marker.is_file(), marker_expected)
            self.assertEqual(seal.is_file(), not marker_expected)
            self.assertFalse(
                _write_bit_present(
                    (
                        os.lstat(paths.root).st_mode,
                        os.lstat(paths.database).st_mode,
                        os.lstat(marker if marker_expected else seal).st_mode,
                    )
                )
            )
            self.assertFalse(store._takeover_activation.get())
            if marker_expected:
                with self.assertRaises(CheckpointSourceError) as blocked:
                    with store._checkpoint_source_operation():
                        pass
                self.assertEqual(blocked.exception.code, "checkpoint_source_busy")
                with recovery_module._takeover_target_operation(
                    paths,
                    project_id=self.project_id,
                ):
                    pass
            else:
                with store._checkpoint_source_operation():
                    pass

        def release(store, lease, cut) -> None:
            store.release_writer(
                lease,
                expected_project_commit=cut.project_commit_id,
                expected_root_digest=cut.project_root_digest,
                expected_canonical_authority_digest=(
                    cut.canonical_authority_digest
                ),
            )

        backup, paths, store, permit = prepare_case("mkdir")
        cut = backup.manifest.store
        real_mkdir = Path.mkdir
        mkdir_error = RuntimeError("simulated recovery-directory failure")

        def fail_target_recovery_mkdir(path: Path, *args, **kwargs):
            if path == paths.recovery:
                raise mkdir_error
            return real_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", new=fail_target_recovery_mkdir):
            with self.assertRaises(RuntimeError) as mkdir_failure:
                activate(store, permit, cut)
        self.assertIs(mkdir_failure.exception, mkdir_error)
        assert_failure_fenced(paths, store, marker_expected=False)
        release(store, activate(store, permit, cut), cut)

        backup, paths, store, permit = prepare_case("unseal-root-write")
        cut = backup.manifest.store
        root_write_error = RuntimeError("simulated post-ACL root-write failure")
        real_make_owner_writable = recovery_module._make_owner_writable

        def fail_after_root_write(path: Path) -> None:
            if path == paths.root:
                real_make_owner_writable(path)
                raise root_write_error
            real_make_owner_writable(path)

        with patch(
            "research_core.workspace_recovery._remove_windows_write_deny"
        ) as remove_windows_deny, patch(
            "research_core.workspace_recovery._make_owner_writable",
            side_effect=fail_after_root_write,
        ):
            with self.assertRaises(RuntimeError) as root_write_failure:
                activate(store, permit, cut)
        self.assertIs(root_write_failure.exception, root_write_error)
        remove_windows_deny.assert_called_once_with(
            paths.root,
            recursive=True,
        )
        assert_failure_fenced(paths, store, marker_expected=False)
        release(store, activate(store, permit, cut), cut)

        backup, paths, store, permit = prepare_case("unseal-protection")
        cut = backup.manifest.store
        unseal_error = RuntimeError("simulated unseal failure before marker")
        unseal_protection_error = RuntimeError(
            "simulated strict unseal protection failure"
        )
        real_make_owner_writable = recovery_module._make_owner_writable
        real_force_read_only = recovery_module._force_owner_read_only_tree

        def fail_unseal_after_root_write(path: Path) -> None:
            if path == paths.root:
                real_make_owner_writable(path)
                raise unseal_error
            real_make_owner_writable(path)

        with patch(
            "research_core.workspace_recovery._remove_windows_write_deny"
        ), patch(
            "research_core.workspace_recovery._make_owner_writable",
            side_effect=fail_unseal_after_root_write,
        ), patch(
            "research_core.workspace_recovery._protect_closed_tree",
            side_effect=unseal_protection_error,
        ), patch(
            "research_core.workspace_recovery._force_owner_read_only_tree",
            wraps=real_force_read_only,
        ) as unseal_fallback:
            with self.assertRaises(RuntimeError) as unseal_protection_failure:
                activate(store, permit, cut)
        self.assertIs(
            unseal_protection_failure.exception,
            unseal_protection_error,
        )
        self.assertIs(unseal_protection_error.__cause__, unseal_error)
        unseal_fallback.assert_called_once_with(paths.root)
        assert_failure_fenced(paths, store, marker_expected=False)
        release(store, activate(store, permit, cut), cut)

        for mode in ("rollback", "first-close"):
            backup, paths, store, permit = prepare_case(mode)
            cut = backup.manifest.store
            real_connect = sqlite3.connect
            body_error = RuntimeError(f"simulated {mode} transaction failure")
            rollback_failure = RuntimeError("simulated rollback cleanup failure")
            close_failure = RuntimeError("simulated first close failure")
            wrapped_connections = []

            class FaultConnection:
                def __init__(self, connection):
                    object.__setattr__(self, "connection", connection)
                    object.__setattr__(self, "close_attempts", 0)

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def __setattr__(self, name, value):
                    setattr(self.connection, name, value)

                def execute(self, statement, *args):
                    normalized = statement.strip().upper()
                    if normalized.startswith("INSERT INTO WRITER_EPOCH"):
                        raise body_error
                    result = self.connection.execute(statement, *args)
                    if mode == "rollback" and normalized == "ROLLBACK":
                        raise rollback_failure
                    return result

                def close(self):
                    object.__setattr__(
                        self,
                        "close_attempts",
                        self.close_attempts + 1,
                    )
                    if mode == "first-close" and self.close_attempts == 1:
                        raise close_failure
                    self.connection.close()

            def fault_connect(database, *args, **kwargs):
                connection = real_connect(database, *args, **kwargs)
                if (
                    isinstance(database, os.PathLike)
                    and Path(database) == paths.database
                ):
                    wrapped = FaultConnection(connection)
                    wrapped_connections.append(wrapped)
                    return wrapped
                return connection

            with patch(
                "research_core.workspace_store.sqlite3.connect",
                side_effect=fault_connect,
            ):
                with self.assertRaises(RuntimeError) as transaction_failure:
                    activate(store, permit, cut)
            self.assertIs(transaction_failure.exception, body_error)
            self.assertEqual(
                wrapped_connections[0].close_attempts,
                1 if mode == "rollback" else 2,
            )
            expected_note = (
                "transaction rollback also raised RuntimeError"
                if mode == "rollback"
                else "the first connection close also raised RuntimeError"
            )
            self.assertIn(expected_note, getattr(body_error, "__notes__", ()))
            assert_failure_fenced(paths, store, marker_expected=False)
            release(store, activate(store, permit, cut), cut)

        backup, paths, store, permit = prepare_case("postcommit-close")
        cut = backup.manifest.store
        real_connect = sqlite3.connect
        first_close_failure = RuntimeError(
            "simulated first committed close failure"
        )
        second_close_failure = RuntimeError(
            "simulated persistent committed close failure"
        )
        committed_connections = []

        class PersistentCloseConnection:
            def __init__(self, connection):
                object.__setattr__(self, "connection", connection)
                object.__setattr__(self, "close_attempts", 0)

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def __setattr__(self, name, value):
                setattr(self.connection, name, value)

            def close(self):
                object.__setattr__(
                    self,
                    "close_attempts",
                    self.close_attempts + 1,
                )
                if self.close_attempts == 1:
                    self.connection.close()
                    raise first_close_failure
                raise second_close_failure

        def persistent_close_connect(database, *args, **kwargs):
            connection = real_connect(database, *args, **kwargs)
            if (
                isinstance(database, os.PathLike)
                and Path(database) == paths.database
            ):
                wrapped = PersistentCloseConnection(connection)
                committed_connections.append(wrapped)
                return wrapped
            return connection

        with patch(
            "research_core.workspace_store.sqlite3.connect",
            side_effect=persistent_close_connect,
        ):
            with self.assertRaises(RecoveryError) as close_failure:
                activate(store, permit, cut)
        self.assertEqual(close_failure.exception.code, "takeover_completion_failed")
        self.assertIs(close_failure.exception.__cause__, second_close_failure)
        self.assertEqual(committed_connections[0].close_attempts, 2)
        assert_failure_fenced(paths, store, marker_expected=True)

        committed_setup_error = RuntimeError(
            "simulated committed replay setup failure"
        )

        def fail_committed_recovery_mkdir(path: Path, *args, **kwargs):
            if path == paths.recovery:
                raise committed_setup_error
            return real_mkdir(path, *args, **kwargs)

        with patch(
            "research_core.workspace_recovery._remove_windows_write_deny"
        ) as remove_replay_deny, patch.object(
            Path,
            "mkdir",
            new=fail_committed_recovery_mkdir,
        ):
            with self.assertRaises(RuntimeError) as committed_setup_failure:
                activate(store, permit, cut)
        self.assertIs(committed_setup_failure.exception, committed_setup_error)
        self.assertGreaterEqual(remove_replay_deny.call_count, 1)
        self.assertTrue(
            all(
                call.args == (paths.root,)
                and call.kwargs == {"recursive": True}
                for call in remove_replay_deny.call_args_list
            )
        )
        assert_failure_fenced(paths, store, marker_expected=True)

        replay_completion_error = RuntimeError("simulated committed replay failure")
        with patch(
            "research_core.workspace_recovery._complete_takeover_unseal",
            side_effect=replay_completion_error,
        ):
            with self.assertRaises(RecoveryError) as replay_failure:
                activate(store, permit, cut)
        self.assertEqual(replay_failure.exception.code, "takeover_completion_failed")
        self.assertIs(replay_failure.exception.__cause__, replay_completion_error)
        assert_failure_fenced(paths, store, marker_expected=True)
        release(store, activate(store, permit, cut), cut)

        backup, paths, store, permit = prepare_case("final-verification")
        cut = backup.manifest.store
        final_verification_error = RuntimeError("simulated final verification failure")
        protection_error = RuntimeError("simulated strict protection failure")
        protection_scope_error = RuntimeError(
            "simulated protection operation-scope failure"
        )
        real_scope_factory = store._checkpoint_source_operation

        real_force_read_only = recovery_module._force_owner_read_only_tree

        class ProtectionExitFailureScope:
            def __init__(self):
                self.inner = real_scope_factory()

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, exc_type, exc, traceback):
                self.inner.__exit__(exc_type, exc, traceback)
                raise protection_scope_error

        def strict_protection_under_held_scope_then_fail(
            root: Path,
            **kwargs,
        ) -> None:
            self.assertEqual(kwargs, {"explicit_windows_acl": True})
            with self.assertRaises(CheckpointSourceError):
                with store._checkpoint_source_operation():
                    pass
            raise protection_error

        with patch(
            "research_core.workspace_recovery._complete_takeover_unseal",
            side_effect=final_verification_error,
        ), patch(
            "research_core.workspace_recovery._protect_closed_tree",
            side_effect=strict_protection_under_held_scope_then_fail,
        ), patch(
            "research_core.workspace_recovery._force_owner_read_only_tree",
            wraps=real_force_read_only,
        ) as force_read_only, patch.object(
            store,
            "_checkpoint_source_operation",
            side_effect=ProtectionExitFailureScope,
        ):
            with self.assertRaises(RuntimeError) as final_failure:
                activate(store, permit, cut)
        self.assertIs(final_failure.exception, protection_error)
        self.assertIs(final_failure.exception.__cause__, final_verification_error)
        self.assertIn(
            "operation-scope cleanup also raised RuntimeError",
            getattr(protection_error, "__notes__", ()),
        )
        force_read_only.assert_called_once_with(paths.root)
        assert_failure_fenced(paths, store, marker_expected=True)
        release(store, activate(store, permit, cut), cut)

        backup, paths, store, permit = prepare_case("scope-exit")
        cut = backup.manifest.store
        real_scope_factory = store._checkpoint_source_operation
        scope_exit_error = RuntimeError("simulated operation-scope exit failure")

        class ExitFailureScope:
            def __init__(self):
                self.inner = real_scope_factory()

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, exc_type, exc, traceback):
                self.inner.__exit__(exc_type, exc, traceback)
                raise scope_exit_error

        with patch.object(
            store,
            "_checkpoint_source_operation",
            side_effect=ExitFailureScope,
        ):
            with self.assertRaises(RecoveryError) as scope_failure:
                activate(store, permit, cut)
        self.assertEqual(scope_failure.exception.code, "takeover_completion_failed")
        self.assertIs(scope_failure.exception.__cause__, scope_exit_error)
        self.assertFalse(store._takeover_activation.get())
        with store._checkpoint_source_operation():
            pass
        reissued = store.reissue_writer_lease(attest_current_principal())
        release(store, reissued, cut)

    @unittest.skipUnless(
        os.name == "posix"
        and hasattr(os, "geteuid")
        and os.geteuid() != 0
        and Path("/usr/bin/test").is_file(),
        "requires the non-root Linux source-unit principal",
    )
    def test_operational_fence_takeover_contends_recovers_commit_and_activates(
        self,
    ) -> None:
        principal = attest_current_principal()
        writer_owner = stable_principal_owner_binding(principal)
        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup(writer_owner=writer_owner)
        )
        portable = self.portable_copy(backup, "takeover-portable-source")
        restore_parent = self.root / "takeover-restore-owner"
        restore_parent.mkdir()
        restored = prepare_portable_side_by_side_restore(
            backup_directory=portable,
            restore_parent=restore_parent.resolve(),
            authority_repo_root=REPO_ROOT,
        )
        self.addCleanup(
            _remove_tree,
            restored.root,
            owned_parent=restore_parent,
            ignore_errors=True,
        )
        self.release_writer(successor)

        controller = self.root / "fixed-test-systemctl"
        expected_argv = [
            "show",
            "research-fixture.service",
            "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID,"
            "User,ControlGroup,TasksCurrent",
            "--no-pager",
        ]
        controller.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"expected = {expected_argv!r}\n"
            "if sys.argv[1:] != expected:\n"
            "    raise SystemExit(19)\n"
            "print('LoadState=loaded')\n"
            "print('UnitFileState=disabled')\n"
            "print('ActiveState=inactive')\n"
            "print('SubState=dead')\n"
            "print('MainPID=0')\n"
            "print('ControlGroup=/system.slice/research-fixture.service')\n"
            "print('TasksCurrent=0')\n"
            f"print('User=' + {principal.principal_name!r})\n",
            encoding="utf-8",
        )
        os.chmod(controller, 0o755)
        _protect_closed_tree(self.paths.root, deny_delete=False)
        parent_mode = stat.S_IMODE(os.lstat(self.root).st_mode)
        self.addCleanup(os.chmod, self.root, parent_mode | stat.S_IWUSR)
        os.chmod(
            self.root,
            parent_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
        )

        with patch(
            "research_core.workspace_recovery._SYSTEMCTL",
            controller,
        ):
            with patch(
                "research_core.workspace_recovery.stable_principal_owner_binding",
                return_value="os-principal-owner:sha256:" + "0" * 64,
            ):
                with self.assertRaises(RecoveryError) as principal_mismatch:
                    _verify_trusted_source_control(
                        backup,
                        source_unit="research-fixture.service",
                        source_root=self.paths.root,
                    )
            self.assertEqual(
                principal_mismatch.exception.code,
                "source_incarnation_fence_binding_mismatch",
            )
            verification = _verify_trusted_source_control(
                backup,
                source_unit="research-fixture.service",
                source_root=self.paths.root,
            )
            with self.assertRaises(ValueError):
                replace(verification, mission_id="mission.other")
            fence = _issue_source_incarnation_fence(
                backup,
                trusted_control_verification=verification,
            )
            with self.assertRaises(RecoveryError) as legacy_fence:
                prepare_takeover(restored, fence_proof=self.fence_reference)
            self.assertEqual(
                legacy_fence.exception.code,
                "source_incarnation_fence_required",
            )
            plan = prepare_takeover(restored, source_fence=fence)
            permit = confirm_explicit_takeover(
                plan,
                restored=restored,
                command_id="takeover.current.checkpoint-source",
                actor=writer_owner,
                writer_creation_basis="qualified current-source takeover",
                expected_manifest_sha256=backup.manifest.manifest_sha256,
                authority_repo_root=REPO_ROOT,
                source_fence=fence,
            )

        target_paths = WorkspacePaths.from_root(restored.root)
        target_store = WorkspaceStore.open_takeover_target(
            target_paths,
            permit,
            expected_project_id=self.project_id,
        )
        cut = backup.manifest.store
        with target_store._checkpoint_source_operation():
            with self.assertRaises(CheckpointSourceError) as contended:
                target_store.activate_takeover(
                    permit,
                    creation_basis="qualified current-source takeover",
                    expected_project_commit=cut.project_commit_id,
                    expected_root_digest=cut.project_root_digest,
                    expected_canonical_authority_digest=(
                        cut.canonical_authority_digest
                    ),
                )
        self.assertEqual(contended.exception.code, "checkpoint_source_busy")
        self.assertTrue((restored.root / "read_only_seal.json").is_file())
        self.assertFalse((restored.root / ".takeover-unseal.json").exists())

        import research_core.workspace_recovery as recovery_module

        complete_takeover = recovery_module._complete_takeover_unseal

        def lose_reply_after_marker_unlink(paths, exact_permit):
            complete_takeover(paths, exact_permit)
            raise RuntimeError("forced post-marker-unlink process edge")

        with patch(
            "research_core.workspace_recovery._complete_takeover_unseal",
            side_effect=lose_reply_after_marker_unlink,
        ):
            with self.assertRaises(RecoveryError) as incomplete:
                target_store.activate_takeover(
                    permit,
                    creation_basis="qualified current-source takeover",
                    expected_project_commit=cut.project_commit_id,
                    expected_root_digest=cut.project_root_digest,
                    expected_canonical_authority_digest=(
                        cut.canonical_authority_digest
                    ),
                )
        self.assertEqual(incomplete.exception.code, "takeover_completion_failed")
        self.assertFalse((restored.root / ".takeover-unseal.json").exists())
        self.assertTrue(stat.S_IMODE(restored.root.stat().st_mode) & stat.S_IWUSR)

        replay_store = WorkspaceStore.open_takeover_target(
            target_paths,
            permit,
            expected_project_id=self.project_id,
        )
        activated = replay_store.activate_takeover(
            permit,
            creation_basis="qualified current-source takeover",
            expected_project_commit=cut.project_commit_id,
            expected_root_digest=cut.project_root_digest,
            expected_canonical_authority_digest=cut.canonical_authority_digest,
        )
        self.assertEqual(activated.epoch, plan.proposed_epoch)
        self.assertEqual(activated.owner, writer_owner)
        self.assertFalse((restored.root / ".takeover-unseal.json").exists())
        self.assertFalse((restored.root / "read_only_seal.json").exists())
        active = replay_store.read_metadata()
        self.assertEqual(active["root_identity"], plan.target_root_identity)
        self.assertEqual(active["current_writer_epoch"], plan.proposed_epoch)
        replay_store.release_writer(
            activated,
            expected_project_commit=cut.project_commit_id,
            expected_root_digest=cut.project_root_digest,
            expected_canonical_authority_digest=cut.canonical_authority_digest,
        )
        checkpoint_lock = (
            restored.root.parent
            / f".{restored.root.name}.checkpoint-source.lock"
        )
        self.assertTrue(checkpoint_lock.is_file())

    @unittest.skipUnless(
        os.name == "posix"
        and hasattr(os, "geteuid")
        and os.geteuid() != 0
        and Path("/usr/bin/test").is_file(),
        "requires the non-root Linux source-unit principal",
    )
    def test_fixed_target_owner_replays_after_hard_pre_marker_unseal_crash(
        self,
    ) -> None:
        principal = attest_current_principal()
        writer_owner = stable_principal_owner_binding(principal)
        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup(writer_owner=writer_owner)
        )
        portable = self.portable_copy(backup, "owner-pre-marker-portable")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / "owner-pre-marker-successor-parent"
        target_parent.mkdir()
        target_root = target_parent / "successor-workspace"
        controller = self.root / "fixed-pre-marker-systemctl"
        expected_argv = [
            "show",
            "research-fixture.service",
            "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID,"
            "User,ControlGroup,TasksCurrent",
            "--no-pager",
        ]
        controller.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"expected = {expected_argv!r}\n"
            "if sys.argv[1:] != expected:\n"
            "    raise SystemExit(19)\n"
            "print('LoadState=loaded')\n"
            "print('UnitFileState=disabled')\n"
            "print('ActiveState=inactive')\n"
            "print('SubState=dead')\n"
            "print('MainPID=0')\n"
            "print('ControlGroup=/system.slice/research-fixture.service')\n"
            "print('TasksCurrent=0')\n"
            f"print('User=' + {principal.principal_name!r})\n",
            encoding="utf-8",
        )
        os.chmod(controller, 0o755)
        self.release_writer(successor)
        _protect_closed_tree(self.paths.root, deny_delete=False)
        parent_mode = stat.S_IMODE(os.lstat(self.root).st_mode)
        self.addCleanup(os.chmod, self.root, parent_mode | stat.S_IWUSR)
        os.chmod(
            self.root,
            parent_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
        )
        self.addCleanup(
            _remove_tree,
            target_root,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(PACKAGE_ROOT),
                str(WORKSTATION_PACKAGES_ROOT),
                environment.get("PYTHONPATH", ""),
            )
        )
        publish_crash_child = r'''
import os
import sys
from pathlib import Path

import research_core.workspace_recovery as recovery

recovery._SYSTEMCTL = Path(sys.argv[1])
target = Path(sys.argv[4]).resolve(strict=False)
real_protect = recovery._protect_closed_tree
def crash_after_publish(root, *args, **kwargs):
    root = Path(root).resolve(strict=False)
    if root == target and (root / recovery.TAKEOVER_UNSEAL_MARKER).is_file():
        os._exit(89)
    return real_protect(root, *args, **kwargs)
recovery._protect_closed_tree = crash_after_publish
recovery.activate_portable_checkpoint_source_takeover_from_owner(
    source_unit="research-fixture.service",
    backup_directory=Path(sys.argv[2]),
    source_root=Path(sys.argv[3]),
    target_root=Path(sys.argv[4]),
    installed_release_root=Path(sys.argv[5]),
    executing_release_sha=sys.argv[6],
    expected_manifest_sha256=sys.argv[7],
    mission_id=sys.argv[8],
    command_id=sys.argv[9],
)
'''
        arguments = [
            str(controller),
            str(portable),
            str(self.paths.root),
            str(target_root),
            str(release_root),
            release_sha,
            backup.manifest.manifest_sha256,
            self.mission_id,
            "takeover.owner.pre-marker.v1",
        ]
        publish_interrupted = subprocess.run(
            [sys.executable, "-c", publish_crash_child, *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(publish_interrupted.returncode, 89, publish_interrupted.stderr)
        self.assertTrue((target_root / ".takeover-unseal.json").is_file())
        _stage, restore_lock = _fixed_target_restore_artifacts(
            target_root.resolve(strict=False)
        )
        self.assertEqual(
            json.loads(restore_lock.read_text(encoding="utf-8"))["lifecycle"],
            "held",
        )

        crash_child = r'''
import os
import sys
from pathlib import Path

import research_core.workspace_recovery as recovery

recovery._SYSTEMCTL = Path(sys.argv[1])
def crash_resume(paths, permit):
    marker = paths.root / recovery.TAKEOVER_UNSEAL_MARKER
    if not marker.is_file():
        os._exit(91)
    recovery._make_owner_writable(paths.root)
    os._exit(88)
recovery._resume_takeover_unseal_permissions = crash_resume
recovery.activate_portable_checkpoint_source_takeover_from_owner(
    source_unit="research-fixture.service",
    backup_directory=Path(sys.argv[2]),
    source_root=Path(sys.argv[3]),
    target_root=Path(sys.argv[4]),
    installed_release_root=Path(sys.argv[5]),
    executing_release_sha=sys.argv[6],
    expected_manifest_sha256=sys.argv[7],
    mission_id=sys.argv[8],
    command_id=sys.argv[9],
)
'''
        interrupted = subprocess.run(
            [sys.executable, "-c", crash_child, *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(interrupted.returncode, 88, interrupted.stderr)
        self.assertTrue((target_root / ".takeover-unseal.json").is_file())
        self.assertFalse((target_root / "read_only_seal.json").exists())
        self.assertEqual(
            json.loads(restore_lock.read_text(encoding="utf-8"))["lifecycle"],
            "released",
        )

        replay_child = r'''
import json
import sys
from pathlib import Path

import research_core.workspace_recovery as recovery

recovery._SYSTEMCTL = Path(sys.argv[1])
result = recovery.activate_portable_checkpoint_source_takeover_from_owner(
    source_unit="research-fixture.service",
    backup_directory=Path(sys.argv[2]),
    source_root=Path(sys.argv[3]),
    target_root=Path(sys.argv[4]),
    installed_release_root=Path(sys.argv[5]),
    executing_release_sha=sys.argv[6],
    expected_manifest_sha256=sys.argv[7],
    mission_id=sys.argv[8],
    command_id=sys.argv[9],
)
print(json.dumps(dict(result), sort_keys=True))
'''
        completed = subprocess.run(
            [sys.executable, "-c", replay_child, *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["disposition"], "incomplete_activation_replay")
        self.assertFalse((target_root / ".takeover-unseal.json").exists())

        active_store = WorkspaceStore.open(
            WorkspacePaths.from_root(target_root),
            expected_project_id=self.project_id,
        )
        active_lease = active_store.reissue_writer_lease(attest_current_principal())
        cut = backup.manifest.store
        active_store.release_writer(
            active_lease,
            expected_project_commit=cut.project_commit_id,
            expected_root_digest=cut.project_root_digest,
            expected_canonical_authority_digest=cut.canonical_authority_digest,
        )

    @unittest.skipUnless(
        os.name == "posix"
        and hasattr(os, "geteuid")
        and os.geteuid() != 0
        and Path("/usr/bin/test").is_file(),
        "requires the non-root Linux source-unit principal",
    )
    def test_fixed_target_owner_takeover_reissues_after_hard_restart(self) -> None:
        principal = attest_current_principal()
        writer_owner = stable_principal_owner_binding(principal)
        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup(writer_owner=writer_owner)
        )
        portable = self.portable_copy(backup, "owner-takeover-portable")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / "owner-selected-successor-parent"
        target_parent.mkdir()
        delete_target_root = target_parent / "successor-workspace-delete-commit"
        target_root = target_parent / "successor-workspace-wal-normalization"
        controller = self.root / "fixed-owner-systemctl"
        expected_argv = [
            "show",
            "research-fixture.service",
            "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID,"
            "User,ControlGroup,TasksCurrent",
            "--no-pager",
        ]
        controller.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"expected = {expected_argv!r}\n"
            "if sys.argv[1:] != expected:\n"
            "    raise SystemExit(19)\n"
            "print('LoadState=loaded')\n"
            "print('UnitFileState=disabled')\n"
            "print('ActiveState=inactive')\n"
            "print('SubState=dead')\n"
            "print('MainPID=0')\n"
            "print('ControlGroup=/system.slice/research-fixture.service')\n"
            "print('TasksCurrent=0')\n"
            f"print('User=' + {principal.principal_name!r})\n",
            encoding="utf-8",
        )
        os.chmod(controller, 0o755)
        self.release_writer(successor)
        _protect_closed_tree(self.paths.root, deny_delete=False)
        parent_mode = stat.S_IMODE(os.lstat(self.root).st_mode)
        self.addCleanup(os.chmod, self.root, parent_mode | stat.S_IWUSR)
        os.chmod(
            self.root,
            parent_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
        )

        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(PACKAGE_ROOT),
                str(WORKSTATION_PACKAGES_ROOT),
                environment.get("PYTHONPATH", ""),
            )
        )
        crash_child = r'''
import os
import sys
from pathlib import Path

import research_core.workspace_recovery as recovery
import research_core.workspace_store as store_module

recovery._SYSTEMCTL = Path(sys.argv[1])
real_connect = store_module.sqlite3.connect
real_connect_workspace = store_module._connect_workspace
crash_mode = sys.argv[10]

class CrashConnection:
    def __init__(self, connection):
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def execute(self, statement, *args):
        result = self._connection.execute(statement, *args)
        if statement.strip().upper() == "COMMIT":
            os._exit(86)
        return result

def crash_connect(*args, **kwargs):
    return CrashConnection(real_connect(*args, **kwargs))

def crash_wal_normalization(database, *args, **kwargs):
    connection = real_connect_workspace(database, *args, **kwargs)
    connection.execute("BEGIN IMMEDIATE")
    database = Path(database)
    wal = database.with_name(database.name + "-wal")
    shared_memory = database.with_name(database.name + "-shm")
    if wal.is_file() and shared_memory.is_file():
        os._exit(87)
    connection.close()
    raise SystemExit(18)

original_activate = store_module.WorkspaceStore.activate_takeover

def crash_activate(self, permit, **kwargs):
    if crash_mode == "delete-commit":
        store_module.sqlite3.connect = crash_connect
    elif crash_mode == "wal-normalization":
        store_module._connect_workspace = crash_wal_normalization
    else:
        raise SystemExit(17)
    return original_activate(self, permit, **kwargs)

store_module.WorkspaceStore.activate_takeover = crash_activate
recovery.activate_portable_checkpoint_source_takeover_from_owner(
    source_unit="research-fixture.service",
    backup_directory=Path(sys.argv[2]),
    source_root=Path(sys.argv[3]),
    target_root=Path(sys.argv[4]),
    installed_release_root=Path(sys.argv[5]),
    executing_release_sha=sys.argv[6],
    expected_manifest_sha256=sys.argv[7],
    mission_id=sys.argv[8],
    command_id=sys.argv[9],
)
'''
        def run_crash(target: Path, mode: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    crash_child,
                    str(controller),
                    str(portable),
                    str(self.paths.root),
                    str(target),
                    str(release_root),
                    release_sha,
                    backup.manifest.manifest_sha256,
                    self.mission_id,
                    "takeover.owner.fixed-target.v1",
                    mode,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        interrupted_delete = run_crash(delete_target_root, "delete-commit")
        self.assertEqual(interrupted_delete.returncode, 86, interrupted_delete.stderr)
        self.assertTrue((delete_target_root / ".takeover-unseal.json").is_file())
        self.assertFalse((delete_target_root / "workspace.sqlite3-wal").exists())
        self.assertFalse((delete_target_root / "workspace.sqlite3-shm").exists())
        delete_replay = activate_portable_checkpoint_source_takeover_from_owner(
            source_unit="research-fixture.service",
            backup_directory=portable,
            target_root=delete_target_root,
            installed_release_root=release_root,
            executing_release_sha=release_sha,
            expected_manifest_sha256=backup.manifest.manifest_sha256,
            mission_id=self.mission_id,
            command_id="takeover.owner.fixed-target.v1",
        )
        self.assertEqual(delete_replay["disposition"], "incomplete_activation_replay")
        self.assertEqual(delete_replay["writer_owner"], writer_owner)
        self.assertFalse((delete_target_root / ".takeover-unseal.json").exists())
        cut = backup.manifest.store
        delete_store = WorkspaceStore.open(
            WorkspacePaths.from_root(delete_target_root),
            expected_project_id=self.project_id,
        )
        delete_lease = delete_store.reissue_writer_lease(attest_current_principal())
        delete_store.release_writer(
            delete_lease,
            expected_project_commit=cut.project_commit_id,
            expected_root_digest=cut.project_root_digest,
            expected_canonical_authority_digest=cut.canonical_authority_digest,
        )
        _remove_tree(
            delete_target_root,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        self.assertFalse(delete_target_root.exists())

        interrupted = run_crash(target_root, "wal-normalization")
        self.assertEqual(interrupted.returncode, 87, interrupted.stderr)
        self.assertTrue((target_root / ".takeover-unseal.json").is_file())
        self.assertTrue((target_root / "workspace.sqlite3-wal").is_file())
        shared_memory = target_root / "workspace.sqlite3-shm"
        self.assertTrue(shared_memory.is_file())
        orphan_shared_memory = shared_memory.read_bytes()
        shared_memory.unlink()
        self.assertFalse(shared_memory.exists())
        import research_core.workspace_recovery as recovery_module

        real_snapshot_open = recovery_module.open_snapshot_connection

        class CloseOnce:
            def __init__(self, connection):
                self.connection = connection
                self.close_attempts = 0

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def close(self):
                self.close_attempts += 1
                if self.close_attempts == 1:
                    raise RuntimeError("simulated snapshot close failure")
                self.connection.close()

        def close_once_snapshot(database: Path):
            return CloseOnce(real_snapshot_open(database))

        target_paths = WorkspacePaths.from_root(target_root)
        real_remove_windows_deny = recovery_module._remove_windows_write_deny
        real_protect_closed_tree = recovery_module._protect_closed_tree
        with patch(
            "research_core.workspace_recovery.open_snapshot_connection",
            side_effect=close_once_snapshot,
        ), patch(
            "research_core.workspace_recovery._remove_windows_write_deny",
            wraps=real_remove_windows_deny,
        ) as remove_snapshot_deny, patch(
            "research_core.workspace_recovery._protect_closed_tree",
            wraps=real_protect_closed_tree,
        ) as protect_snapshot_tree, recovery_module._takeover_target_operation(
            target_paths,
            project_id=self.project_id,
        ), self.assertRaisesRegex(RuntimeError, "snapshot close failure"):
            with recovery_module._takeover_state_snapshot(
                target_paths,
                manifest=backup.manifest,
            ) as snapshot:
                snapshot.execute("SELECT 1").fetchone()
        remove_snapshot_deny.assert_called_once_with(
            target_paths.root,
            recursive=True,
        )
        protect_snapshot_tree.assert_called_once_with(
            target_paths.root,
            explicit_windows_acl=True,
        )
        writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        protected_paths = [
            target_root,
            target_root / "workspace.sqlite3",
            target_root / "workspace.sqlite3-wal",
        ]
        if shared_memory.exists():
            protected_paths.append(shared_memory)
        for protected in protected_paths:
            self.assertFalse(stat.S_IMODE(protected.stat().st_mode) & writable)

        class BodyAndCloseFailure:
            def __init__(self, connection):
                self.connection = connection
                self.close_attempts = 0

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, *args, **kwargs):
                raise RecoveryError(
                    "takeover_unseal_state_invalid",
                    "simulated primary snapshot failure",
                )

            def close(self):
                self.close_attempts += 1
                if self.close_attempts == 1:
                    raise RuntimeError("simulated secondary snapshot close failure")
                self.connection.close()

        body_close_failures: list[BodyAndCloseFailure] = []

        def body_and_close_failure_snapshot(database: Path):
            wrapped = BodyAndCloseFailure(real_snapshot_open(database))
            body_close_failures.append(wrapped)
            return wrapped

        with patch(
            "research_core.workspace_recovery.open_snapshot_connection",
            side_effect=body_and_close_failure_snapshot,
        ), recovery_module._takeover_target_operation(
            target_paths,
            project_id=self.project_id,
        ), self.assertRaises(RecoveryError) as primary_failure:
            with recovery_module._takeover_state_snapshot(
                target_paths,
                manifest=backup.manifest,
            ) as snapshot:
                snapshot.execute("SELECT 1").fetchone()
        self.assertEqual(
            primary_failure.exception.code,
            "takeover_unseal_state_invalid",
        )
        self.assertEqual(body_close_failures[0].close_attempts, 2)

        self.assertTrue((target_root / "workspace.sqlite3-wal").is_file())
        recovery_module._make_owner_writable(target_root)
        shared_memory.unlink(missing_ok=True)
        _protect_closed_tree(target_root)
        self.assertFalse(shared_memory.exists())

        force_read_only = recovery_module._force_owner_read_only_tree
        forced_closed: list[Path] = []

        def record_force_closed(root: Path, **kwargs) -> None:
            forced_closed.append(root)
            force_read_only(root, **kwargs)

        strict_reseal_error = RecoveryError(
            "recovery_tree_mutable",
            "simulated strict reseal failure",
        )

        def fail_strict_reseal(root: Path, **kwargs) -> None:
            self.assertEqual(kwargs, {"explicit_windows_acl": True})
            raise strict_reseal_error

        with patch(
            "research_core.workspace_recovery._protect_closed_tree",
            side_effect=fail_strict_reseal,
        ), patch(
            "research_core.workspace_recovery._force_owner_read_only_tree",
            side_effect=record_force_closed,
        ), self.assertRaises(RecoveryError) as reseal_failure:
            activate_portable_checkpoint_source_takeover_from_owner(
                source_unit="research-fixture.service",
                backup_directory=portable,
                target_root=target_root,
                installed_release_root=release_root,
                executing_release_sha=release_sha,
                expected_manifest_sha256=backup.manifest.manifest_sha256,
                mission_id=self.mission_id,
                command_id="takeover.owner.fixed-target.v1",
            )
        self.assertEqual(reseal_failure.exception.code, "recovery_tree_mutable")
        self.assertEqual(forced_closed, [target_root.resolve(strict=True)])
        self.assertTrue((target_root / "workspace.sqlite3-wal").is_file())
        recovery_module._make_owner_writable(target_root)
        shared_memory.unlink(missing_ok=True)
        _protect_closed_tree(target_root)
        self.assertFalse(shared_memory.exists())
        self.addCleanup(
            _remove_tree,
            target_root,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        os.chmod(self.root, parent_mode | stat.S_IWUSR)
        _remove_tree(self.paths.root, owned_parent=self.root)
        self.assertFalse(self.paths.root.exists())

        # A new interpreter has a new issuer token/secret and cannot reuse any
        # Fence, Plan, Permit, or WriterLease object from the interrupted call.
        # The exact committed writer evidence makes the retired source unnecessary.
        child = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "import research_core.workspace_recovery as recovery\n"
            "result = recovery.activate_portable_checkpoint_source_takeover_from_owner(\n"
            "    source_unit='research-fixture.service',\n"
            "    backup_directory=Path(sys.argv[1]),\n"
            "    target_root=Path(sys.argv[2]),\n"
            "    installed_release_root=Path(sys.argv[3]),\n"
            "    executing_release_sha=sys.argv[4],\n"
            "    expected_manifest_sha256=sys.argv[5],\n"
            "    mission_id=sys.argv[6],\n"
            "    command_id=sys.argv[7],\n"
            ")\n"
            "print(json.dumps(dict(result), sort_keys=True))\n"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(portable),
                str(target_root),
                str(release_root),
                release_sha,
                backup.manifest.manifest_sha256,
                self.mission_id,
                "takeover.owner.fixed-target.v1",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            set(result),
            {
                "schema_version",
                "status",
                "disposition",
                "project_id",
                "mission_id",
                "backup_id",
                "backup_manifest_sha256",
                "source_incarnation",
                "source_root_identity",
                "target_root_identity",
                "project_commit",
                "root_digest",
                "transition_head",
                "writer_epoch",
                "writer_owner",
                "command_id",
            },
        )
        self.assertEqual(
            result["schema_version"],
            "mathematical_research.portable_checkpoint_source_takeover_owner.v1",
        )
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["disposition"], "incomplete_activation_replay")
        self.assertEqual(result["writer_owner"], writer_owner)
        self.assertFalse((target_root / ".takeover-unseal.json").exists())

        # WAL sidecar deletion on last close is not a SQLite contract.  Build the
        # following orphan-SHM fixture only after an exact, non-busy checkpoint
        # and removal of a proven-empty test-owned WAL with no live connection.
        checkpoint_connection = sqlite3.connect(
            target_root / "workspace.sqlite3",
            timeout=0,
            isolation_level=None,
        )
        try:
            checkpoint_connection.execute("PRAGMA busy_timeout = 0")
            checkpoint_result = checkpoint_connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        finally:
            checkpoint_connection.close()
        self.assertEqual(checkpoint_result, (0, 0, 0))
        residual_wal = target_root / "workspace.sqlite3-wal"
        if residual_wal.exists():
            self.assertEqual(residual_wal.stat().st_size, 0)
            residual_wal.unlink()
        shared_memory.unlink(missing_ok=True)
        self.assertFalse(residual_wal.exists())
        shared_memory.write_bytes(orphan_shared_memory)

        active_replay = activate_portable_checkpoint_source_takeover_from_owner(
            source_unit="research-fixture.service",
            backup_directory=portable,
            target_root=target_root,
            installed_release_root=release_root,
            executing_release_sha=release_sha,
            expected_manifest_sha256=backup.manifest.manifest_sha256,
            mission_id=self.mission_id,
            command_id="takeover.owner.fixed-target.v1",
        )
        self.assertEqual(active_replay["disposition"], "active_replay")
        self.assertEqual(active_replay["writer_owner"], writer_owner)

        active_store = WorkspaceStore.open(
            WorkspacePaths.from_root(target_root),
            expected_project_id=self.project_id,
        )
        active_lease = active_store.reissue_writer_lease(
            attest_current_principal()
        )
        active_store.release_writer(
            active_lease,
            expected_project_commit=cut.project_commit_id,
            expected_root_digest=cut.project_root_digest,
            expected_canonical_authority_digest=cut.canonical_authority_digest,
        )

    @unittest.skipUnless(
        os.name == "posix"
        and hasattr(os, "geteuid")
        and os.geteuid() != 0
        and Path("/usr/bin/test").is_file(),
        "requires the non-root Linux source-unit principal",
    )
    def test_fixed_target_owner_takeover_recovers_hard_precommit_journal(
        self,
    ) -> None:
        principal = attest_current_principal()
        writer_owner = stable_principal_owner_binding(principal)
        successor, _checkpoint, _claimed, backup, _report = (
            self.create_claimed_current_backup(writer_owner=writer_owner)
        )
        portable = self.portable_copy(backup, "owner-precommit-portable")
        release_root, release_sha = self.installed_release_fixture()
        target_parent = self.root / "owner-precommit-successor-parent"
        target_parent.mkdir()
        target_root = target_parent / "successor-workspace"
        controller = self.root / "fixed-precommit-systemctl"
        expected_argv = [
            "show",
            "research-fixture.service",
            "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID,"
            "User,ControlGroup,TasksCurrent",
            "--no-pager",
        ]
        controller.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"expected = {expected_argv!r}\n"
            "if sys.argv[1:] != expected:\n"
            "    raise SystemExit(19)\n"
            "print('LoadState=loaded')\n"
            "print('UnitFileState=disabled')\n"
            "print('ActiveState=inactive')\n"
            "print('SubState=dead')\n"
            "print('MainPID=0')\n"
            "print('ControlGroup=/system.slice/research-fixture.service')\n"
            "print('TasksCurrent=0')\n"
            f"print('User=' + {principal.principal_name!r})\n",
            encoding="utf-8",
        )
        os.chmod(controller, 0o755)
        self.release_writer(successor)
        _protect_closed_tree(self.paths.root, deny_delete=False)
        parent_mode = stat.S_IMODE(os.lstat(self.root).st_mode)
        self.addCleanup(os.chmod, self.root, parent_mode | stat.S_IWUSR)
        os.chmod(
            self.root,
            parent_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
        )
        self.addCleanup(
            _remove_tree,
            target_root,
            owned_parent=target_parent,
            ignore_errors=True,
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(PACKAGE_ROOT),
                str(WORKSTATION_PACKAGES_ROOT),
                environment.get("PYTHONPATH", ""),
            )
        )
        crash_child = r'''
import os
import sys
from pathlib import Path

import research_core.workspace_recovery as recovery
import research_core.workspace_store as store_module

recovery._SYSTEMCTL = Path(sys.argv[1])
real_connect = store_module.sqlite3.connect

class CrashConnection:
    def __init__(self, connection):
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def execute(self, statement, *args):
        if statement.lstrip().startswith("INSERT INTO writer_epoch"):
            self._connection.execute("PRAGMA cache_size = 1")
        result = self._connection.execute(statement, *args)
        if statement.lstrip().startswith("INSERT INTO writer_epoch"):
            os._exit(86)
        return result

def crash_connect(*args, **kwargs):
    return CrashConnection(real_connect(*args, **kwargs))

original_activate = store_module.WorkspaceStore.activate_takeover

def crash_activate(self, permit, **kwargs):
    store_module.sqlite3.connect = crash_connect
    return original_activate(self, permit, **kwargs)

store_module.WorkspaceStore.activate_takeover = crash_activate
recovery.activate_portable_checkpoint_source_takeover_from_owner(
    source_unit="research-fixture.service",
    backup_directory=Path(sys.argv[2]),
    source_root=Path(sys.argv[3]),
    target_root=Path(sys.argv[4]),
    installed_release_root=Path(sys.argv[5]),
    executing_release_sha=sys.argv[6],
    expected_manifest_sha256=sys.argv[7],
    mission_id=sys.argv[8],
    command_id=sys.argv[9],
)
'''
        interrupted = subprocess.run(
            [
                sys.executable,
                "-c",
                crash_child,
                str(controller),
                str(portable),
                str(self.paths.root),
                str(target_root),
                str(release_root),
                release_sha,
                backup.manifest.manifest_sha256,
                self.mission_id,
                "takeover.owner.precommit.v1",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
        self.assertTrue((target_root / ".takeover-unseal.json").is_file())
        self.assertTrue((target_root / "workspace.sqlite3-journal").is_file())

        replay_child = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "import research_core.workspace_recovery as recovery\n"
            "recovery._SYSTEMCTL = Path(sys.argv[1])\n"
            "result = recovery.activate_portable_checkpoint_source_takeover_from_owner(\n"
            "    source_unit='research-fixture.service',\n"
            "    backup_directory=Path(sys.argv[2]),\n"
            "    source_root=Path(sys.argv[3]),\n"
            "    target_root=Path(sys.argv[4]),\n"
            "    installed_release_root=Path(sys.argv[5]),\n"
            "    executing_release_sha=sys.argv[6],\n"
            "    expected_manifest_sha256=sys.argv[7],\n"
            "    mission_id=sys.argv[8],\n"
            "    command_id=sys.argv[9],\n"
            ")\n"
            "print(json.dumps(dict(result), sort_keys=True))\n"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                replay_child,
                str(controller),
                str(portable),
                str(self.paths.root),
                str(target_root),
                str(release_root),
                release_sha,
                backup.manifest.manifest_sha256,
                self.mission_id,
                "takeover.owner.precommit.v1",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["disposition"], "incomplete_activation_replay")
        self.assertEqual(result["writer_owner"], writer_owner)
        self.assertFalse((target_root / ".takeover-unseal.json").exists())
        self.assertFalse((target_root / "workspace.sqlite3-journal").exists())

        active_store = WorkspaceStore.open(
            WorkspacePaths.from_root(target_root),
            expected_project_id=self.project_id,
        )
        active_lease = active_store.reissue_writer_lease(
            attest_current_principal()
        )
        cut = backup.manifest.store
        active_store.release_writer(
            active_lease,
            expected_project_commit=cut.project_commit_id,
            expected_root_digest=cut.project_root_digest,
            expected_canonical_authority_digest=cut.canonical_authority_digest,
        )

    def test_portable_verifier_moves_only_location_and_returns_closed_facts(self) -> None:
        from research_core import workspace_recovery as recovery_module

        backup = self.create_backup("portable-moved")
        moved = self.portable_copy(backup, "operator-root/portable-moved")

        def identities(root: Path) -> tuple[tuple[str, str, int], ...]:
            return tuple(
                sorted(
                    (
                        item.relative_to(root).as_posix(),
                        digest(item.read_bytes()),
                        item.stat().st_size,
                    )
                    for item in root.rglob("*")
                    if item.is_file()
                )
            )

        before = identities(moved)
        with self.assertRaises(RecoveryError) as strict:
            verify_backup_set(moved)
        self.assertIn(
            strict.exception.code,
            {"backup_path_invalid", "backup_store_binding_invalid"},
        )

        with patch.object(
            recovery_module, "_verify_recovery_snapshot_semantics",
            wraps=recovery_module._verify_recovery_snapshot_semantics,
        ) as semantics, patch.object(
            recovery_module, "_portable_backup_report_from_verified",
            wraps=recovery_module._portable_backup_report_from_verified,
        ) as materialize:
            report = verify_portable_backup_set(moved)
        semantics.assert_called_once()
        materialize.assert_called_once()
        verified = materialize.call_args.args[0]
        with patch.object(
            recovery_module, "_verify_recovery_snapshot_semantics",
            side_effect=AssertionError("descriptive closeout repeated Store semantics"),
        ), patch.object(
            recovery_module, "_sqlite_integrity",
            side_effect=AssertionError("unchanged snapshot repeated SQLite integrity"),
        ), patch.object(
            recovery_module, "_load_persisted_backup_evidence",
            side_effect=AssertionError("unchanged snapshot repeated persisted Evidence audit"),
        ):
            closeout_report = recovery_module._portable_backup_report_from_unchanged_verified(verified)
            inspected, pinned_report = recovery_module.inspect_portable_backup_set(
                moved, expected_manifest_sha256=backup.manifest.manifest_sha256,
            )
            self.assertEqual(inspected, verified)
            self.assertEqual(pinned_report, report)
            with self.assertRaises(RecoveryError) as wrong_pin:
                recovery_module.inspect_portable_backup_set(moved, expected_manifest_sha256="0" * 64)
            self.assertEqual(wrong_pin.exception.code, "stale_backup")
        self.assertEqual(closeout_report, report)

        self.assertIs(type(report), PortableBackupVerificationReport)
        self.assertEqual(identities(moved), before)
        self.assertEqual(report.manifest_sha256, backup.manifest.manifest_sha256)
        self.assertEqual(report.source_root_identity, self.paths.root_identity)
        self.assertTrue(report.structural_verification_passed)
        self.assertTrue(report.semantic_verification_passed)
        self.assertTrue(report.read_only_tree_verified)
        self.assertFalse(report.capability_bearing_values_returned)
        for name in (
            "claim_writer",
            "prepare_takeover",
            "transition_recovery",
            "verify_integrity",
        ):
            self.assertFalse(hasattr(report, name))
        self.assertTrue(
            all(
                isinstance(getattr(report, item.name), (str, int, bool, type(None)))
                for item in fields(report)
            )
        )
        with self.assertRaises(FrozenInstanceError):
            report.backup_id = "changed"  # type: ignore[misc]

        # A retained pin does not waive fresh byte/custody checks or accept a
        # self-rebound replacement manifest, even though no semantic scan runs.
        original = {name: (moved / name).read_bytes() for name in (
            "manifest.json", "workspace.sqlite3", backup.manifest.evidence_inventory[0].logical_path,
        )}
        for changed, expected_codes in (
            ("workspace.sqlite3", {"backup_sqlite_corrupt"}),
            (backup.manifest.evidence_inventory[0].logical_path, {"backup_cas_invalid"}),
            ("manifest.json", {"stale_backup"}),
            ("protection", {"recovery_tree_mutable", "restore_not_physically_read_only", "backup_evidence_mutable"}),
        ):
            with self.subTest(pinned_inspection_change=changed):
                self.unlock_fixture_tree(moved)
                try:
                    if changed == "manifest.json":
                        self.rewrite_manifest(moved, lambda value: value.update(created_at="2026-01-01T00:00:00Z"))
                    elif changed != "protection":
                        path = moved / changed
                        os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
                        path.write_bytes(original[changed] + b"changed")
                    if changed != "protection":
                        _protect_closed_tree(moved, deny_delete=False)
                    with patch.object(WorkspaceStore, "verify_integrity", side_effect=AssertionError("pinned inspection must not audit")):
                        with self.assertRaises(RecoveryError) as rejected:
                            recovery_module.inspect_portable_backup_set(moved, expected_manifest_sha256=backup.manifest.manifest_sha256)
                    self.assertIn(rejected.exception.code, expected_codes)
                finally:
                    self.unlock_fixture_tree(moved)
                    for name, raw in original.items():
                        path = moved / name
                        os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
                        path.write_bytes(raw)
                    _protect_closed_tree(moved, deny_delete=False)

    def test_portable_verifier_rejects_source_provenance_substitution(self) -> None:
        backup = self.create_backup("portable-provenance")
        moved = self.portable_copy(backup, "portable-provenance-copy")
        self.rewrite_manifest(
            moved,
            lambda payload: payload["store"].update(root_identity="a" * 64),
        )
        _protect_closed_tree(moved, deny_delete=False)

        with self.assertRaises(RecoveryError) as captured:
            verify_portable_backup_set(moved)

        self.assertEqual(captured.exception.code, "backup_store_binding_invalid")

    def test_portable_verifier_rejects_external_hardlinks_for_every_file_class(
        self,
    ) -> None:
        backup = self.create_backup("portable-hardlinks")
        cases = {
            "manifest": "manifest.json",
            "database": "workspace.sqlite3",
            "cas": backup.manifest.evidence_inventory[0].logical_path,
        }
        for label, relative in cases.items():
            with self.subTest(file_class=label):
                target = backup.path / relative
                external = self.root / f"external-{label}"
                self.unlock_fixture_tree(backup.path)
                _remove_windows_write_deny(backup.path, recursive=True)
                os.chmod(target, stat.S_IMODE(target.stat().st_mode) | stat.S_IWUSR)
                os.link(target, external)
                try:
                    _protect_closed_tree(backup.path, deny_delete=False)
                    self.assertEqual(target.stat().st_nlink, 2)
                    # Ordinary location-bound verification retains its existing
                    # contract; independent physical files are portable-only.
                    self.assertEqual(
                        verify_backup_set(backup.path).manifest.manifest_sha256,
                        backup.manifest.manifest_sha256,
                    )
                    with patch.object(
                        Path,
                        "open",
                        side_effect=AssertionError("unsafe portable tree was opened"),
                    ) as opened:
                        with self.assertRaises(RecoveryError) as captured:
                            verify_portable_backup_set(backup.path)
                    opened.assert_not_called()
                    self.assertEqual(captured.exception.code, "backup_manifest_invalid")
                finally:
                    self.unlock_fixture_tree(backup.path)
                    _remove_windows_write_deny(backup.path, recursive=True)
                    os.chmod(target, stat.S_IMODE(target.stat().st_mode) | stat.S_IWUSR)
                    external.unlink()
                    _protect_closed_tree(backup.path, deny_delete=False)

    @unittest.skipUnless(os.name == "nt", "requires a native Windows junction")
    def test_portable_verifier_rejects_native_junction_root(self) -> None:
        backup = self.create_backup("portable-junction")
        moved = self.portable_copy(backup, "portable-junction-target")
        junction = self.root / "portable-junction-root"
        created = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Junction -Path '"
                + str(junction).replace("'", "''")
                + "' -Target '"
                + str(moved).replace("'", "''")
                + "' | Out-Null",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        try:
            self.assertTrue(
                junction.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            self.assertEqual(junction.resolve(), moved.resolve())
            with patch.object(
                Path,
                "open",
                side_effect=AssertionError("junction target was opened"),
            ) as opened:
                with self.assertRaises(RecoveryError) as captured:
                    verify_portable_backup_set(junction)
            opened.assert_not_called()
            self.assertEqual(captured.exception.code, "backup_manifest_invalid")
        finally:
            # Remove only the junction entry, never recursively its target.
            junction.rmdir()
        self.assertTrue((moved / "manifest.json").is_file())

    def test_portable_verifier_rejects_special_entries_before_any_payload_read(
        self,
    ) -> None:
        backup = self.create_backup("portable-special-adapter")
        moved = self.portable_copy(backup, "portable-special-adapter-copy")
        target = moved / "manifest.json"
        original_lstat = Path.lstat
        target_identity = os.path.normcase(os.path.abspath(target))
        for special_type in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(file_type=special_type):
                def adapted_lstat(path, *args, **kwargs):
                    metadata = original_lstat(path, *args, **kwargs)
                    if os.path.normcase(os.path.abspath(path)) == target_identity:
                        values = list(metadata)
                        values[0] = special_type | 0o400
                        return os.stat_result(
                            values,
                            {
                                "st_file_attributes": getattr(
                                    metadata, "st_file_attributes", 0
                                ),
                                "st_reparse_tag": getattr(metadata, "st_reparse_tag", 0),
                            },
                        )
                    return metadata

                with patch.object(Path, "lstat", adapted_lstat), patch.object(
                    Path,
                    "open",
                    side_effect=AssertionError("special-file payload was opened"),
                ) as opened:
                    with self.assertRaises(RecoveryError) as captured:
                        verify_portable_backup_set(moved)
                opened.assert_not_called()
                self.assertEqual(captured.exception.code, "backup_manifest_invalid")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires native POSIX FIFO support")
    def test_portable_verifier_rejects_native_fifo_before_open(self) -> None:
        backup = self.create_backup("portable-fifo")
        moved = self.portable_copy(backup, "portable-fifo-copy")
        self.unlock_fixture_tree(moved)
        fifo = moved / "unbound-fifo"
        os.mkfifo(fifo, 0o400)
        try:
            _protect_closed_tree(moved, deny_delete=False)
            with patch.object(
                Path,
                "open",
                side_effect=AssertionError("FIFO tree payload was opened"),
            ) as opened:
                with self.assertRaises(RecoveryError) as captured:
                    verify_portable_backup_set(moved)
            opened.assert_not_called()
            self.assertEqual(captured.exception.code, "backup_manifest_invalid")
        finally:
            self.unlock_fixture_tree(moved)
            fifo.unlink()

    def test_portable_verifier_rejects_unbound_and_casefold_colliding_directories(
        self,
    ) -> None:
        backup = self.create_backup("portable-directory-closure")
        cases = {
            "unbound": (("unbound-empty",), "backup_closure_unexpected"),
            "casefold": (("Straße", "STRASSE"), "backup_path_collision"),
        }
        for label, (names, expected_code) in cases.items():
            with self.subTest(directory_class=label):
                moved = self.portable_copy(backup, f"portable-directories-{label}")
                self.unlock_fixture_tree(moved)
                for name in names:
                    (moved / name).mkdir()
                _protect_closed_tree(moved, deny_delete=False)
                with self.assertRaises(RecoveryError) as captured:
                    verify_portable_backup_set(moved)
                self.assertEqual(captured.exception.code, expected_code)

    def test_portable_verifier_rejects_every_writable_tree_class(self) -> None:
        backup = self.create_backup("portable-protection")
        cas_relative = backup.manifest.evidence_inventory[0].logical_path
        cases = {
            "manifest": "manifest.json",
            "database": "workspace.sqlite3",
            "cas": cas_relative,
            "directory": None,
        }
        for label, relative in cases.items():
            with self.subTest(protected_class=label):
                moved = self.portable_copy(
                    backup,
                    f"portable-writable-{label}",
                )
                self.unlock_fixture_tree(moved)
                target = moved if relative is None else moved / relative
                os.chmod(
                    target,
                    stat.S_IMODE(target.stat().st_mode) | stat.S_IWUSR,
                )
                with self.assertRaises(RecoveryError) as captured:
                    verify_portable_backup_set(moved)
                self.assertEqual(
                    captured.exception.code,
                    "backup_tree_protection_invalid",
                )

    def test_portable_verifier_rejects_manifest_database_and_cas_corruption(self) -> None:
        from research_core import workspace_recovery as recovery_module

        backup = self.create_backup("portable-corruption-source")

        late = self.portable_copy(backup, "portable-late-closeout-corruption")
        verified = recovery_module._verify_portable_backup_for_trust_transition(
            _verify_backup_tree_contents(late, enforce_source_location=False),
        )
        original_files = {
            name: (late / name).read_bytes()
            for name in ("workspace.sqlite3", "manifest.json")
        }
        for tamper, expected_code in (
            ("database", "backup_sqlite_corrupt"),
            ("protection", "backup_tree_protection_invalid"),
            ("rebound_manifest", "stale_backup"),
        ):
            with self.subTest(late_closeout=tamper):
                try:
                    self.unlock_fixture_tree(late)
                    database = late / "workspace.sqlite3"
                    os.chmod(database, stat.S_IMODE(database.stat().st_mode) | stat.S_IWUSR)
                    if tamper == "database":
                        database.write_bytes(original_files["workspace.sqlite3"] + b"changed")
                    elif tamper == "rebound_manifest":
                        self.rewrite_manifest(
                            late, lambda payload: payload.update(created_at="2026-01-01T00:00:00Z"),
                        )
                    if tamper != "protection":
                        _protect_closed_tree(late, deny_delete=False)
                    if tamper == "rebound_manifest":
                        # It is a valid, protected tree, but not the identity
                        # whose semantics were earned before the other audit.
                        self.assertNotEqual(
                            _verify_backup_tree_contents(late, enforce_source_location=False), verified,
                        )
                    with patch.object(
                        recovery_module, "_verify_recovery_snapshot_semantics",
                        side_effect=AssertionError("late guard repeated semantic audit"),
                    ), patch.object(
                        recovery_module, "_portable_backup_report_from_verified",
                        side_effect=AssertionError("changed backup produced a success report"),
                    ), self.assertRaises(RecoveryError) as rejected:
                        recovery_module._portable_backup_report_from_unchanged_verified(verified)
                    self.assertEqual(rejected.exception.code, expected_code)
                    if tamper == "protection":
                        self.assertIsInstance(rejected.exception.__cause__, RecoveryError)
                        self.assertIn(rejected.exception.__cause__.code, {
                            "backup_evidence_mutable", "recovery_tree_mutable",
                            "restore_not_physically_read_only",
                        })
                finally:
                    self.unlock_fixture_tree(late)
                    for name, raw in original_files.items():
                        path = late / name
                        os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
                        path.write_bytes(raw)
                    _protect_closed_tree(late, deny_delete=False)
                self.assertEqual(
                    _verify_backup_tree_contents(late, enforce_source_location=False), verified,
                )

        wrong_hash = self.portable_copy(backup, "portable-wrong-database-hash")
        self.unlock_fixture_tree(wrong_hash)
        database = wrong_hash / "workspace.sqlite3"
        os.chmod(database, stat.S_IMODE(database.stat().st_mode) | stat.S_IWUSR)
        database.write_bytes(database.read_bytes() + b"changed")
        _protect_closed_tree(wrong_hash, deny_delete=False)
        with self.assertRaises(RecoveryError) as captured:
            verify_portable_backup_set(wrong_hash)
        self.assertEqual(captured.exception.code, "backup_sqlite_corrupt")

        malformed = self.portable_copy(backup, "portable-malformed-database")
        self.unlock_fixture_tree(malformed)
        database = malformed / "workspace.sqlite3"
        os.chmod(database, stat.S_IMODE(database.stat().st_mode) | stat.S_IWUSR)
        database.write_bytes(b"not sqlite")
        self.rebind_manifest_to_database(malformed)
        with self.assertRaises(RecoveryError) as captured:
            verify_portable_backup_set(malformed)
        self.assertEqual(captured.exception.code, "backup_sqlite_invalid")

        missing = self.portable_copy(backup, "portable-missing-cas")
        self.unlock_fixture_tree(missing)
        blob = missing / backup.manifest.evidence_inventory[0].logical_path
        os.chmod(blob, stat.S_IMODE(blob.stat().st_mode) | stat.S_IWUSR)
        blob.unlink()
        _protect_closed_tree(missing, deny_delete=False)
        with self.assertRaises(RecoveryError) as captured:
            verify_portable_backup_set(missing)
        self.assertIn(
            captured.exception.code,
            {"backup_cas_invalid", "backup_closure_unexpected"},
        )

        extra = self.portable_copy(backup, "portable-extra-cas")
        self.unlock_fixture_tree(extra)
        extra_path = extra / "cas" / "sha256" / "ff" / ("0" * 62)
        extra_path.parent.mkdir(parents=True, exist_ok=True)
        extra_path.write_bytes(b"extra")
        _protect_closed_tree(extra, deny_delete=False)
        with self.assertRaises(RecoveryError) as captured:
            verify_portable_backup_set(extra)
        self.assertEqual(captured.exception.code, "backup_closure_unexpected")

        manifest = self.portable_copy(backup, "portable-manifest-mismatch")
        self.unlock_fixture_tree(manifest)
        manifest_path = manifest / "manifest.json"
        os.chmod(
            manifest_path,
            stat.S_IMODE(manifest_path.stat().st_mode) | stat.S_IWUSR,
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["created_at"] = "changed-without-rebinding"
        manifest_path.write_bytes(canonical_json_bytes(payload))
        _protect_closed_tree(manifest, deny_delete=False)
        with self.assertRaises(RecoveryError) as captured:
            verify_portable_backup_set(manifest)
        self.assertEqual(captured.exception.code, "backup_manifest_corrupt")

    def test_portable_verifier_runs_full_semantic_integrity_for_all_row_classes(
        self,
    ) -> None:
        lease, _checkpoint = self.complete_checkpoint_and_keep_writer()
        try:
            source = self.create_backup("portable-semantic-source")
        finally:
            self.release_writer(lease)

        def journal(connection) -> None:
            connection.execute(
                "UPDATE transition_journal SET predecessor_digest = ? "
                "WHERE sequence_no = (SELECT MAX(sequence_no) FROM transition_journal)",
                ("f" * 64,),
            )

        def revision(connection) -> None:
            connection.execute(
                "UPDATE branch_revision SET payload_digest = ? "
                "WHERE object_id = 'branch.recovery' AND revision = 1",
                ("f" * 64,),
            )

        def head(connection) -> None:
            connection.execute(
                "UPDATE branch_head SET payload_digest = ? "
                "WHERE object_id = 'branch.recovery'",
                ("f" * 64,),
            )

        def command_result(connection) -> None:
            connection.execute(
                "UPDATE command_result SET result_digest = ? WHERE command_id = "
                "(SELECT command_id FROM command_result ORDER BY command_id LIMIT 1)",
                ("f" * 64,),
            )

        def checkpoint(connection) -> None:
            connection.execute(
                "UPDATE continuation_checkpoint SET payload_digest = ?",
                ("f" * 64,),
            )

        def canonical(connection) -> None:
            connection.execute(
                "UPDATE project_commit SET canonical_authority_digest = ? WHERE commit_no = "
                "(SELECT MIN(commit_no) FROM project_commit)",
                ("f" * 64,),
            )

        def closure(connection) -> None:
            connection.execute(
                "INSERT INTO closure_manifest VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "closure.invalid-portable",
                    "candidate",
                    "f" * 64,
                    "[]",
                    "{}",
                    "2026-09-04T00:00:00Z",
                ),
            )

        def deletion_directive(connection) -> None:
            connection.execute(
                "INSERT INTO deletion_directive VALUES (?, ?, ?, ?, ?)",
                (
                    "directive.invalid-portable",
                    "{}",
                    "{}",
                    "active",
                    "2026-09-04T00:00:00Z",
                ),
            )

        def tombstone(connection) -> None:
            evidence = connection.execute(
                "SELECT evidence_id, revision FROM evidence_item_revision "
                "ORDER BY evidence_id, revision LIMIT 1"
            ).fetchone()
            assert evidence is not None
            directive_id = "directive.for-invalid-tombstone"
            connection.execute(
                "INSERT INTO deletion_directive VALUES (?, ?, ?, ?, ?)",
                (
                    directive_id,
                    json.dumps({"authorization_sha256": "a" * 64}),
                    json.dumps(
                        {
                            "reason": "fixture",
                            "blob_sha256s": [],
                            "evidence_references": [],
                        }
                    ),
                    "active",
                    "2026-09-04T00:00:00Z",
                ),
            )
            connection.execute(
                "INSERT INTO evidence_tombstone VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "tombstone.invalid-portable",
                    str(evidence[0]),
                    int(evidence[1]),
                    directive_id,
                    json.dumps({"reason_sha256": "b" * 64}),
                    "2026-09-04T00:00:00Z",
                ),
            )

        corruptions = {
            "journal": journal,
            "revision": revision,
            "head": head,
            "command_result": command_result,
            "checkpoint": checkpoint,
            "canonical": canonical,
            "closure": closure,
            "deletion_directive": deletion_directive,
            "tombstone": tombstone,
        }
        for label, corrupt in corruptions.items():
            with self.subTest(row_class=label):
                moved = self.portable_copy(source, f"portable-semantic-{label}")
                self.unlock_fixture_tree(moved)
                database = moved / "workspace.sqlite3"
                os.chmod(
                    database,
                    stat.S_IMODE(database.stat().st_mode) | stat.S_IWUSR,
                )
                connection = sqlite3.connect(database)
                try:
                    corrupt(connection)
                    connection.commit()
                finally:
                    connection.close()
                self.rebind_manifest_to_database(moved)
                with self.assertRaises(RecoveryError) as captured:
                    verify_portable_backup_set(moved)
                self.assertEqual(
                    captured.exception.code,
                    "backup_semantic_integrity_failed",
                )

    def test_portable_mode_and_path_adapters_cover_cross_platform_ambiguity(self) -> None:
        self.assertFalse(_write_bit_present((0o400, 0o500, 0o440)))
        self.assertTrue(_write_bit_present((0o400, 0o700)))
        self.assertTrue(_write_bit_present((0o402, 0o500)))
        backup = self.create_backup("portable-posix-adapter")
        moved = self.portable_copy(backup, "portable-posix-adapter-copy")
        self.assertFalse(
            _write_bit_present(
                item.stat().st_mode for item in moved.rglob("*") if item.is_file()
            )
        )
        self.assertFalse(
            _write_bit_present(
                path.stat().st_mode
                for path in (moved, *(item for item in moved.rglob("*") if item.is_dir()))
            )
        )
        from research_core.workspace_recovery import _validate_backup_relative_paths

        _validate_backup_relative_paths(("manifest.json", "cas/sha256/aa/value"))
        with self.assertRaises(RecoveryError) as captured:
            _validate_backup_relative_paths(("CAS/item", "cas/item"))
        self.assertEqual(captured.exception.code, "backup_path_collision")
        with self.assertRaises(RecoveryError) as captured:
            _validate_backup_relative_paths(("cas/Straße", "cas/STRASSE"))
        self.assertEqual(captured.exception.code, "backup_path_collision")

    def test_portable_scope_is_sealed_and_unscoped_live_rules_stay_strict(self) -> None:
        backup = self.create_backup("portable-scope")
        moved = self.portable_copy(backup, "portable-scope-copy")
        portable = _verify_backup_tree_contents(
            moved,
            enforce_source_location=False,
        )
        authority = _issue_verified_backup_integrity_authority(
            portable,
            portable_materialization=True,
        )
        with self.assertRaises(ValueError):
            replace(authority, source_root_identity="b" * 64)
        with self.assertRaises(ValueError):
            replace(authority, portable_materialization=False)
        other = self.portable_copy(backup, "portable-scope-foreign-copy")
        with self.assertRaises(ValueError):
            replace(
                authority,
                database=other / "workspace.sqlite3",
                cas_root=other,
            )

        verifier = WorkspaceStore(
            WorkspacePaths.from_root(moved),
            self.project_id,
        )
        connection = open_snapshot_connection(moved / "workspace.sqlite3")
        try:
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "workspace root identity changed",
            ):
                verifier.verify_integrity(
                    _connection_override=connection,
                    _allow_sealed_delete=True,
                )
        finally:
            connection.close()

    def test_closure_selector_rejects_absence_and_authoritative_ties(self) -> None:
        with self.assertRaises(RecoveryError) as absent:
            _select_newest_journaled_closure(())
        self.assertEqual(absent.exception.code, "qualifying_closure_absent")
        tied = (
            _SelectedJournaledClosure(self.closure, 12),
            _SelectedJournaledClosure(self.closure, 12),
        )
        with self.assertRaises(RecoveryError) as ambiguous:
            _select_newest_journaled_closure(tied)
        self.assertEqual(ambiguous.exception.code, "closure_order_ambiguous")

    def test_closure_selector_skips_stale_newest_then_fails_when_none_qualify(
        self,
    ) -> None:
        metadata = self.store.read_metadata()
        lease = self.store.claim_writer(
            owner="closure-selection-writer",
            creation_basis="create a later independently rooted closure",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=self.authority_digest,
        )
        try:
            candidate = deep_thaw(self.candidate_payload)
            candidate["candidate_id"] = "candidate.recovery-newest"
            candidate["standing"]["basis"] = "later closure selection fixture"
            requirement = candidate_a1_requirement(candidate)
            self.assertIsNotNone(requirement)
            self.store.commit_candidate_revision(
                executive_epoch_id=self.epoch_id,
                mission_id=self.mission_id,
                candidate_id=candidate["candidate_id"],
                payload=candidate,
                expected_head_revision=None,
                expected_head_payload_digest=None,
                a1_requirement=requirement,
                lease=lease,
                command_id="asr0a.closure.newest",
                actor="test.fixture",
                expected_canonical_authority_digest=self.authority_digest,
            )
            database = self.root / "closure-selection.sqlite3"
            self.store.export_verified_backup(database)
        finally:
            self.release_writer(lease)

        newest = _newest_journaled_closure_from_snapshot(database)
        self.assertNotEqual(newest.closure.closure_id, self.closure.closure_id)

        def invalidate_root(closure) -> None:
            binding = {
                "mission": ("mission_head", "object_id"),
                "context": ("context_head", "object_id"),
                "session": ("session_head", "object_id"),
                "candidate": ("candidate_head", "object_id"),
                "evidence": ("evidence_item_head", "evidence_id"),
            }[closure.root.target_kind]
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    f"UPDATE {binding[0]} SET payload_digest = ? "
                    f"WHERE {binding[1]} = ?",
                    ("0" * 64, closure.root.target_id),
                )
                connection.commit()
            finally:
                connection.close()

        invalidate_root(newest.closure)
        selected = _newest_journaled_closure_from_snapshot(database)
        self.assertEqual(selected.closure.closure_id, self.closure.closure_id)

        invalidate_root(selected.closure)
        with self.assertRaises(RecoveryError) as absent:
            _newest_journaled_closure_from_snapshot(database)
        self.assertEqual(absent.exception.code, "qualifying_closure_absent")

    def test_installed_current_backup_rejects_mismatched_canonical_bytes(self) -> None:
        lease, _checkpoint = self.complete_checkpoint_and_keep_writer()
        release_root, release_sha = self.installed_release_fixture()
        canonical = release_root / self.snapshot.authority_vector.canonical_state_path
        canonical.write_bytes(self.canonical_bytes + b"changed")
        try:
            with self.assertRaises(RecoveryError) as captured:
                create_installed_current_backup(
                    store=self.store,
                    cas=self.cas,
                    backup_id="asr0a.canonical-mismatch",
                    mission_id=self.mission_id,
                    project_id=self.project_id,
                    installed_release_root=release_root,
                    executing_release_sha=release_sha,
                )
            self.assertEqual(captured.exception.code, "canonical_binding_stale")
            self.assertFalse(
                (self.paths.backups / "asr0a.canonical-mismatch").exists()
            )
        finally:
            self.release_writer(lease)

    def test_installed_current_backup_uses_one_copied_cut_while_live_mission_advances(
        self,
    ) -> None:
        lease, checkpoint = self.complete_checkpoint_and_keep_writer()
        epoch_id = self.start_successor_epoch(lease, checkpoint)
        release_root, release_sha = self.installed_release_fixture()
        self.assertFalse((release_root / ".git").exists())
        active_goal_marker = self.root / "active-goal.marker"
        active_goal_marker.write_text("must remain irrelevant", encoding="utf-8")
        cut_commit = int(self.store.read_metadata()["current_project_commit"])
        live_commit: list[int] = []

        def advance_after_cut(event, _path, _value) -> None:
            if event == "after_sqlite_snapshot" and not live_commit:
                live_commit.append(self.advance_live_branch(lease, epoch_id))

        try:
            with patch(
                "research_core.workspace_recovery._git_head",
                side_effect=AssertionError("installed backup must not read Git HEAD"),
            ):
                backup, report = create_installed_current_backup(
                    store=self.store,
                    cas=self.cas,
                    backup_id="asr0a.generation-current-cut",
                    mission_id=self.mission_id,
                    project_id=self.project_id,
                    installed_release_root=release_root,
                    executing_release_sha=release_sha,
                    fault_hook=advance_after_cut,
                )
            self.assertEqual(report.database_project_commit, cut_commit)
            before_read = self.store.read_metadata()
            with patch.object(WorkspaceStore, "verify_integrity", side_effect=AssertionError("pinned existing backup must not be audited again")):
                existing, recovered = inspect_installed_current_backup(
                    store=self.store, backup_id=backup.manifest.backup_id,
                    mission_id=self.mission_id, project_id=self.project_id,
                    installed_release_root=release_root, executing_release_sha=release_sha,
                    expected_manifest_sha256=backup.manifest.manifest_sha256,
                )
            self.assertEqual(recovered, report)
            self.assertEqual(existing, backup)
            self.assertEqual(self.store.read_metadata(), before_read)
            disposable_parent = self.root / "operational-disposable"
            disposable_parent.mkdir()
            materialized = disposable_parent / backup.path.name
            shutil.copytree(backup.path, materialized)
            protect_portable_backup_materialization(materialized, owned_parent=disposable_parent)
            self.assertEqual(verify_portable_backup_set(materialized).manifest_sha256, report.manifest_sha256)
            dispose_portable_backup_materialization(materialized, owned_parent=disposable_parent)
            self.assertFalse(materialized.exists())
            with self.assertRaises(RecoveryError):
                inspect_installed_current_backup(
                    store=self.store, backup_id=backup.manifest.backup_id,
                    mission_id="mission.wrong", project_id=self.project_id,
                    installed_release_root=release_root, executing_release_sha=release_sha,
                )
            self.assertEqual(backup.manifest.store.project_commit_id, cut_commit)
            self.assertEqual(len(live_commit), 1)
            self.assertGreater(live_commit[0], report.database_project_commit)
            self.assertEqual(
                self.store.read_metadata()["current_writer_epoch"],
                lease.epoch,
            )
            self.assertEqual(report.checkpoint_id, checkpoint.document["checkpoint_id"])
            self.assertEqual(report.checkpoint_sha256, checkpoint.digest_sha256)
            self.assertEqual(report.closure_id, self.closure.closure_id)
            self.assertNotEqual(report.canonical_source_commit, release_sha)
            self.assertEqual(report.cas_inventory_contract, _COMMITTED_BLOB_CAS_INVENTORY)
            self.assertEqual(
                (
                    report.writer_leases_created,
                    report.mission_mutations,
                    report.lifecycle_mutations,
                    report.host_effects,
                    report.provider_effects,
                ),
                (0, 0, 0, 0, 0),
            )
            self.assertEqual(
                active_goal_marker.read_text(encoding="utf-8"),
                "must remain irrelevant",
            )
            self.assertEqual(
                verify_backup_set(backup.path).manifest.manifest_sha256,
                report.manifest_sha256,
            )
        finally:
            self.release_writer(lease)

    def test_backup_binds_direct_mission_mode_and_root_digest_version(self) -> None:
        backup = self.create_backup("mode-binding")

        self.assertEqual(backup.manifest.store.operating_mode, "mission_runtime")
        self.assertEqual(backup.manifest.store.root_digest_version, 6)
        verified = verify_backup_set(backup.path)
        self.assertEqual(verified.manifest.store.operating_mode, "mission_runtime")
        self.assertEqual(verified.manifest.store.root_digest_version, 6)

    def test_manifest_written_tree_is_not_publishable_until_protected(self) -> None:
        backup = self.create_backup("physical-seal")
        expected_manifest = backup.manifest.manifest_sha256
        self.unlock_fixture_tree(backup.path)

        with self.assertRaises(RecoveryError) as unsealed:
            verify_backup_set(backup.path)
        self.assertEqual(
            unsealed.exception.code,
            (
                "restore_not_physically_read_only"
                if os.name == "nt"
                else "recovery_tree_mutable"
            ),
        )

        _remove_windows_write_deny(backup.path, recursive=True)
        _protect_closed_tree(backup.path, deny_delete=False)
        self.assertEqual(
            verify_backup_set(backup.path).manifest.manifest_sha256,
            expected_manifest,
        )

    def test_historical_mode_binding_is_version_gated_and_never_infers_card11(self) -> None:
        self.assertEqual(
            _store_mode_binding_from_metadata({}, schema_version=2),
            ("inactive_foundation", 1),
        )
        with self.assertRaises(RecoveryError) as historical_columns:
            _store_mode_binding_from_metadata(
                {
                    "operating_mode": "pre_cutover_observer",
                    "root_digest_version": 2,
                },
                schema_version=2,
            )
        self.assertEqual(
            historical_columns.exception.code,
            "backup_store_binding_invalid",
        )
        with self.assertRaises(RecoveryError) as missing_current_binding:
            _store_mode_binding_from_metadata({}, schema_version=4)
        self.assertEqual(
            missing_current_binding.exception.code,
            "backup_store_binding_invalid",
        )

    def test_backup_rejects_an_unauthorized_mode_version_pair(self) -> None:
        backup = self.create_backup("mode-version-mismatch")
        self.rewrite_manifest(
            backup.path,
            lambda value: value["store"].__setitem__(
                "operating_mode", "pre_cutover_observer"
            ),
        )

        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(backup.path)
        self.assertEqual(captured.exception.code, "backup_manifest_invalid")

    def test_historical_observer_backup_is_inspection_only(self) -> None:
        backup = self.create_backup("historical-observer-classification")
        restored = prepare_side_by_side_restore(
            backup=backup,
            authority_repo_root=REPO_ROOT,
        )
        card11_store = replace(
            backup.manifest.store,
            operating_mode="pre_cutover_observer",
            root_digest_version=2,
        )
        card11_manifest = replace(backup.manifest, store=card11_store)
        card11_restored = replace(
            restored,
            backup=replace(backup, manifest=card11_manifest),
        )

        self.assertEqual(
            card11_manifest.takeover_classification,
            "inspection_only_historical_workspace",
        )
        with patch(
            "research_core.workspace_recovery.verify_restored_workspace",
            return_value=None,
        ):
            with self.assertRaises(RecoveryError) as captured:
                prepare_takeover(card11_restored, fence_proof=self.fence_reference)
        self.assertEqual(captured.exception.code, "backup_inspection_only")

    def rewrite_manifest(self, backup_path: Path, mutate) -> dict:
        self.unlock_fixture_tree(backup_path)
        path = backup_path / "manifest.json"
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600)
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        body = {key: value for key, value in payload.items() if key != "manifest_sha256"}
        payload["manifest_sha256"] = digest(canonical_json_bytes(body))
        path.write_bytes(canonical_json_bytes(payload))
        return payload

    def unlock_fixture_tree(self, root: Path) -> None:
        _remove_windows_write_deny(root)
        for directory in (root, *(item for item in root.rglob("*") if item.is_dir())):
            os.chmod(directory, stat.S_IMODE(directory.stat().st_mode) | stat.S_IWUSR)

    def test_remove_tree_deletes_a_sealed_plain_child_without_changing_parent(self) -> None:
        owner = self.root / "cleanup-owner"
        target = owner / "sealed-child"
        nested = target / "nested"
        nested.mkdir(parents=True)
        (nested / "read_only_seal.json").write_text("sealed", encoding="utf-8")
        parent_mode = stat.S_IMODE(owner.stat().st_mode)
        _protect_closed_tree(target)

        _remove_tree(target, owned_parent=owner)

        self.assertFalse(target.exists())
        self.assertEqual(stat.S_IMODE(owner.stat().st_mode), parent_mode)

    def test_remove_tree_rejects_root_and_nested_links_without_touching_target(self) -> None:
        owner = self.root / "cleanup-link-owner"
        target = owner / "plain-child"
        external = self.root / "cleanup-external"
        target.mkdir(parents=True)
        external.mkdir()
        external_file = external / "preserved.txt"
        external_file.write_text("preserve me", encoding="utf-8")
        nested_link = target / "external-link"
        root_link = owner / "root-link"
        try:
            nested_link.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        self.addCleanup(nested_link.unlink, missing_ok=True)
        try:
            root_link.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"second symlink creation unavailable: {exc}")
        self.addCleanup(root_link.unlink, missing_ok=True)

        with self.assertRaises(RecoveryError) as nested_error:
            _remove_tree(target, owned_parent=owner)
        self.assertEqual(nested_error.exception.code, "recovery_cleanup_unsafe")
        with self.assertRaises(RecoveryError) as root_error:
            _remove_tree(root_link, owned_parent=owner, ignore_errors=True)
        self.assertEqual(root_error.exception.code, "recovery_cleanup_unsafe")
        self.assertEqual(external_file.read_text(encoding="utf-8"), "preserve me")
        self.assertTrue(target.exists())

        nested_link.unlink()
        root_link.unlink()
        _remove_tree(target, owned_parent=owner)

    def test_remove_tree_reprotects_plain_residue_after_failed_disposal(self) -> None:
        owner = self.root / "cleanup-failure-owner"
        target = owner / "sealed-child"
        target.mkdir(parents=True)
        payload = target / "payload.txt"
        payload.write_text("sealed", encoding="utf-8")
        _protect_closed_tree(target)

        failure = PermissionError("forced cleanup failure")
        with patch(
            "research_core.workspace_recovery.shutil.rmtree",
            side_effect=failure,
        ):
            with self.assertRaises(PermissionError):
                _remove_tree(target, owned_parent=owner)

        self.assertTrue(target.exists())
        self.assertFalse(stat.S_IMODE(target.stat().st_mode) & stat.S_IWUSR)
        self.assertFalse(stat.S_IMODE(payload.stat().st_mode) & stat.S_IWUSR)
        with patch(
            "research_core.workspace_recovery.shutil.rmtree",
            side_effect=failure,
        ):
            _remove_tree(target, owned_parent=owner, ignore_errors=True)
        self.assertFalse(stat.S_IMODE(target.stat().st_mode) & stat.S_IWUSR)
        self.assertFalse(stat.S_IMODE(payload.stat().st_mode) & stat.S_IWUSR)
        _remove_tree(target, owned_parent=owner)

    def test_remove_tree_requires_an_exact_direct_child_boundary(self) -> None:
        owner = self.root / "cleanup-boundary-owner"
        middle = owner / "middle"
        target = middle / "nested-child"
        target.mkdir(parents=True)

        with self.assertRaises(RecoveryError) as captured:
            _remove_tree(target, owned_parent=owner, ignore_errors=True)

        self.assertEqual(captured.exception.code, "recovery_cleanup_unsafe")
        self.assertTrue(target.exists())
        _remove_tree(target, owned_parent=middle)

    def test_backup_is_bound_to_real_store_live_authority_exact_closure_and_actual_restore(self) -> None:
        from research_core import workspace_recovery as recovery_module

        before = self.canonical_path.read_bytes()
        with patch.object(WorkspaceStore, "verify_integrity", autospec=True, side_effect=WorkspaceStore.verify_integrity) as audit:
            backup = self.create_backup()
        audit.assert_called_once()
        manifest = backup.manifest
        # Finalizing the accepted checkpoint-source claim consumes only this
        # protected identity. It must not reopen or rehash the database/CAS.
        with patch.object(recovery_module, "_sha256_file", side_effect=AssertionError("manifest-only read hashed a data file")), \
             patch.object(recovery_module, "open_snapshot_connection", side_effect=AssertionError("manifest-only read opened SQLite")), \
             patch.object(WorkspaceStore, "verify_integrity", side_effect=AssertionError("manifest-only read repeated semantics")):
            retained = recovery_module._read_pinned_completed_backup_manifest(
                backup.path, expected_manifest_sha256=manifest.manifest_sha256,
            )
            self.assertEqual(retained, manifest)
            with self.assertRaises(RecoveryError) as wrong_pin:
                recovery_module._read_pinned_completed_backup_manifest(
                    backup.path, expected_manifest_sha256="0" * 64,
                )
            self.assertEqual(wrong_pin.exception.code, "stale_backup")
        metadata = self.store.read_metadata()
        self.assertEqual(manifest.store.project_id, self.store.project_id)
        self.assertEqual(manifest.store.root_identity, self.paths.root_identity)
        self.assertEqual(manifest.store.project_root_digest, metadata["current_root_digest"])
        self.assertEqual(manifest.canonical.sha256, self.snapshot.raw_sha256)
        self.assertEqual(manifest.closure.manifest_sha256, self.closure.manifest_sha256)
        self.assertRegex(manifest.evidence_metadata_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            tuple(item.version for item in manifest.store.migration_history),
            tuple(range(1, manifest.store.schema_version + 1)),
        )
        self.assertEqual(
            tuple(item.sha256 for item in manifest.evidence_inventory),
            tuple(sorted(self.closure.blob_sha256s)),
        )
        self.assertEqual(
            manifest.cas_inventory_contract,
            _COMMITTED_BLOB_CAS_INVENTORY,
        )
        self.assertEqual(manifest.closure.blob_sha256s, (self.blob.sha256,))
        self.assertTrue((backup.path / self.blob.logical_path).is_file())
        self.assertEqual(manifest.restore_test.status, "passed")
        self.assertEqual(manifest.restore_test.sqlite_integrity, "passed")
        self.assertRegex(manifest.restore_test.receipt_sha256, r"^[0-9a-f]{64}$")
        self.assertFalse(
            stat.S_IMODE((backup.path / "workspace.sqlite3").stat().st_mode)
            & stat.S_IWUSR
        )
        if os.name == "nt":
            with self.assertRaises(PermissionError):
                (backup.path / "forbidden-new-file").write_bytes(b"forbidden")
        self.assertFalse(any(self.paths.backups.glob(".restore-test-*")))
        self.assertEqual(self.canonical_path.read_bytes(), before)
        with self.assertRaises(RecoveryError) as wrong_store:
            create_bound_backup(
                store="toy sqlite",  # type: ignore[arg-type]
                cas=self.cas,
                backup_id="toy",
                authority_repo_root=REPO_ROOT,
                closure_id=self.closure.closure_id,
            )
        self.assertEqual(wrong_store.exception.code, "workspace_store_required")
        with self.assertRaises(RecoveryError) as absent:
            create_bound_backup(
                store=self.store,
                cas=self.cas,
                backup_id="absent-closure",
                authority_repo_root=REPO_ROOT,
                closure_id="closure.caller-assertion-does-not-exist",
            )
        self.assertEqual(absent.exception.code, "persisted_closure_absent")

    def test_strong_backup_authority_binds_database_tree_and_rejects_tamper(self) -> None:
        backup = self.create_backup("strong-authority")
        authority = _issue_verified_backup_integrity_authority(backup)

        binding = _verify_recovery_snapshot_semantics(authority)
        self.assertEqual(binding, backup.manifest.store)
        for field_name, value in (
            ("database_sha256", "f" * 64),
            ("database_length", authority.database_length + 1),
            ("snapshot_tree_sha256", "e" * 64),
            ("source_backup_manifest_sha256", "d" * 64),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    replace(authority, **{field_name: value})

        self.unlock_fixture_tree(backup.path)
        database = backup.path / "workspace.sqlite3"
        os.chmod(
            database,
            stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
        )
        raw = database.read_bytes()
        database.write_bytes(raw + b"stale")
        with self.assertRaises(RecoveryError) as captured:
            _verify_recovery_snapshot_semantics(authority)
        self.assertEqual(
            captured.exception.code,
            "backup_integrity_authority_stale",
        )

    def test_existing_legacy_closure_inventory_remains_readable_only(self) -> None:
        backup = self.create_backup("legacy-readback")
        self.unlock_fixture_tree(backup.path)

        payload = json.loads(
            (backup.path / "manifest.json").read_text(encoding="utf-8")
        )
        payload.pop("cas_inventory_contract")
        payload["evidence_inventory"] = [
            item
            for item in payload["evidence_inventory"]
            if item["sha256"] in self.closure.blob_sha256s
        ]
        payload["evidence_root_sha256"] = digest(
            canonical_json_bytes(payload["evidence_inventory"])
        )
        legacy_persisted = _load_persisted_backup_evidence(
            backup.path / "workspace.sqlite3",
            closure_id=self.closure.closure_id,
            cas=EvidenceCAS(WorkspacePaths.from_root(backup.path)),
            cas_inventory_contract=_LEGACY_CLOSURE_CAS_INVENTORY,
        )
        payload["evidence_metadata_sha256"] = legacy_persisted.metadata_sha256
        restore_root = self.paths.recovery / ".legacy-readback-restore"
        verification = _execute_restore_copy(
            source_root=backup.path,
            target_root=restore_root,
            owned_parent=self.paths.recovery,
            sqlite_sha256=backup.manifest.sqlite_sha256,
            sqlite_length=backup.manifest.sqlite_length,
            inventory=(self.blob,),
            directives=backup.manifest.deletion_directives,
            tombstones=backup.manifest.tombstones,
        )
        payload["restore_test"] = _restore_test_record(verification).to_payload()
        _remove_tree(restore_root, owned_parent=self.paths.recovery)
        body = {
            key: value
            for key, value in payload.items()
            if key != "manifest_sha256"
        }
        payload["manifest_sha256"] = digest(canonical_json_bytes(body))
        manifest_path = backup.path / "manifest.json"
        os.chmod(
            manifest_path,
            stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
        )
        manifest_path.write_bytes(canonical_json_bytes(payload))
        _protect_closed_tree(backup.path, deny_delete=False)

        verified = verify_backup_set(backup.path)

        self.assertEqual(
            verified.manifest.cas_inventory_contract,
            _LEGACY_CLOSURE_CAS_INVENTORY,
        )
        self.assertEqual(
            tuple(item.sha256 for item in verified.manifest.evidence_inventory),
            self.closure.blob_sha256s,
        )
        self.assertNotIn("cas_inventory_contract", verified.manifest.body_payload())

    def test_canonical_binding_separates_backup_commit_from_live_authority_origin(self) -> None:
        source_commit, _archive_bound = resolve_verified_test_source_commit(REPO_ROOT)
        authority = self.snapshot.authority_vector.to_mapping()
        binding = CanonicalBinding(
            path=str(authority["canonical_state_path"]),
            sha256=str(authority["canonical_state_sha256"]),
            source_commit=source_commit,
            authority_vector=authority,
        )
        self.assertEqual(binding.authority_payload(), authority)
        self.assertNotIn("validation_contract_sha256", binding.to_payload())
        authority_raw = canonical_json_bytes(authority)
        from_metadata = _canonical_binding_from_metadata(
            {
                "canonical_authority_json": authority_raw.decode("utf-8"),
                "canonical_authority_digest": digest(authority_raw),
            },
            source_commit=source_commit,
        )
        self.assertEqual(from_metadata, binding)

        legacy_authority = {
            "source_class": "live_canonical",
            "canonical_state_path": authority["canonical_state_path"],
            "canonical_state_sha256": authority["canonical_state_sha256"],
            "canonical_schema_version": 2,
            "validation_contract_sha256": "1" * 64,
            "campaign_path": None,
            "campaign_sha256": None,
            "protocol_path": None,
            "protocol_sha256": None,
            "state_schema_path": "legacy/state.schema.json",
            "state_schema_sha256": "2" * 64,
            "workbench_schema_path": None,
            "workbench_schema_sha256": None,
            "architecture_sha256": "3" * 64,
            "contracts_sha256": "4" * 64,
            "status_sha256": "5" * 64,
            "testing_sha256": "6" * 64,
            "workbench_sha256": None,
            "workbench_record_revisions": [],
            "renderer_contract_version": None,
            "source_commit": None,
        }
        legacy = CanonicalBinding(
            path=str(legacy_authority["canonical_state_path"]),
            sha256=str(legacy_authority["canonical_state_sha256"]),
            source_commit=source_commit,
            authority_vector=legacy_authority,
            validation_contract_sha256="1" * 64,
        )
        legacy_authority["workbench_record_revisions"].append(
            {"ref": "forged.after.decode", "revision": 99}
        )
        self.assertEqual(
            deep_thaw(legacy.authority_vector)["workbench_record_revisions"],
            [],
        )
        self.assertIn("validation_contract_sha256", legacy.to_payload())

        installed_digest = _verify_installed_release_canonical_binding(
            legacy,
            REPO_ROOT,
            exact_source_commit=source_commit,
        )
        self.assertEqual(
            installed_digest,
            digest(canonical_json_bytes(legacy.to_payload())),
        )
        with self.assertRaises(RecoveryError) as live_contract_mismatch:
            verify_live_canonical_binding(legacy, REPO_ROOT)
        self.assertEqual(live_contract_mismatch.exception.code, "canonical_binding_stale")
        self.assertEqual(
            str(live_contract_mismatch.exception),
            "legacy validation-contract bytes changed",
        )
        with self.assertRaises(RecoveryError) as source_release_mismatch:
            _verify_installed_release_canonical_binding(
                legacy,
                REPO_ROOT,
                exact_source_commit="f" * 40,
            )
        self.assertEqual(source_release_mismatch.exception.code, "canonical_binding_stale")
        source_bound_authority = deep_thaw(legacy.authority_vector)
        source_bound_authority["source_commit"] = source_commit
        source_bound_legacy = CanonicalBinding(
            path=legacy.path,
            sha256=legacy.sha256,
            source_commit=source_commit,
            authority_vector=source_bound_authority,
            validation_contract_sha256=legacy.validation_contract_sha256,
        )
        with self.assertRaises(RecoveryError) as source_bound_contract_mismatch:
            _verify_installed_release_canonical_binding(
                source_bound_legacy,
                REPO_ROOT,
                exact_source_commit=source_commit,
            )
        self.assertEqual(
            str(source_bound_contract_mismatch.exception),
            "legacy validation-contract bytes changed",
        )
        with tempfile.TemporaryDirectory() as temporary:
            installed_root = Path(temporary).resolve()
            canonical_path = installed_root / legacy.path
            canonical_path.parent.mkdir(parents=True)
            canonical_path.write_bytes(b"different canonical state")
            with self.assertRaises(RecoveryError) as canonical_mismatch:
                _verify_installed_release_canonical_binding(
                    legacy,
                    installed_root,
                    exact_source_commit=source_commit,
                )
            self.assertEqual(canonical_mismatch.exception.code, "canonical_binding_stale")

        for malformed in (
            {**authority, "binding_version": 3},
            {**authority, "source_class": "synthetic_fixture"},
            {**authority, "source_commit": None},
            {**authority, "extra": "receipt"},
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    CanonicalBinding(
                        path=str(authority["canonical_state_path"]),
                        sha256=str(authority["canonical_state_sha256"]),
                        source_commit=source_commit,
                        authority_vector=malformed,
                    )

    def test_backup_rejects_non_live_copied_store_without_publish(self) -> None:
        source_commit, _archive_bound = resolve_verified_test_source_commit(REPO_ROOT)

        def forge_non_live_authority(stage_name, stage, _backup_report) -> None:
            if stage_name != "after_sqlite_snapshot":
                return
            authority = self.snapshot.authority_vector.to_mapping()
            authority.update(
                {
                    "source_class": "synthetic_fixture",
                    "source_commit": source_commit,
                }
            )
            authority_raw = canonical_json_bytes(authority)
            connection = sqlite3.connect(stage / "workspace.sqlite3")
            try:
                connection.execute(
                    "UPDATE workspace_metadata SET canonical_authority_json = ?, "
                    "canonical_authority_digest = ? WHERE singleton = 1",
                    (authority_raw.decode("utf-8"), digest(authority_raw)),
                )
                connection.commit()
            finally:
                connection.close()

        backup_id = "non-live-canonical-authority"
        with self.assertRaises(RecoveryError) as captured:
            create_bound_backup(
                store=self.store,
                cas=self.cas,
                backup_id=backup_id,
                authority_repo_root=REPO_ROOT,
                closure_id=self.closure.closure_id,
                fault_hook=forge_non_live_authority,
            )
        self.assertEqual(captured.exception.code, "workspace_canonical_binding_invalid")
        self.assertFalse((self.paths.backups / backup_id).exists())

    def test_backup_uses_copied_store_and_evidence_snapshot_under_concurrent_ingest(self) -> None:
        snapshot_commit = int(self.store.read_metadata()["current_project_commit"])
        claimed = []
        unreferenced = []

        def mutate_live_after_snapshot(stage, backup_report) -> None:
            del stage
            self.assertEqual(backup_report.project_commit, snapshot_commit)
            metadata = self.store.read_metadata()
            claimed.append(
                self.store.claim_writer(
                    owner="concurrent-writer",
                    creation_basis="online backup concurrency test",
                    expected_project_commit=int(metadata["current_project_commit"]),
                    expected_root_digest=str(metadata["current_root_digest"]),
                    expected_canonical_authority_digest=str(
                        metadata["canonical_authority_digest"]
                    ),
                )
            )
            unreferenced.append(
                self.cas.ingest_bytes(
                    b"concurrent unreferenced Evidence",
                    original_name="unreferenced.txt",
                ).record
            )

        def hook(stage_name, stage, value) -> None:
            if stage_name == "after_sqlite_snapshot":
                mutate_live_after_snapshot(stage, value)

        try:
            backup = create_bound_backup(
                store=self.store,
                cas=self.cas,
                backup_id="concurrent-snapshot",
                authority_repo_root=REPO_ROOT,
                closure_id=self.closure.closure_id,
                fault_hook=hook,
            )
            self.assertEqual(backup.manifest.store.project_commit_id, snapshot_commit)
            self.assertIsNone(backup.manifest.store.current_writer_epoch)
            self.assertEqual(
                tuple(item.sha256 for item in backup.manifest.evidence_inventory),
                tuple(sorted(self.closure.blob_sha256s)),
            )
            self.assertNotIn(
                unreferenced[0].sha256,
                {item.sha256 for item in backup.manifest.evidence_inventory},
            )
            self.assertFalse((backup.path / unreferenced[0].logical_path).exists())
            self.assertTrue(self.cas.path_for_digest(unreferenced[0].sha256).exists())
            self.assertEqual(
                self.store.read_metadata()["current_writer_epoch"],
                claimed[0].epoch,
            )
        finally:
            if claimed and self.store.read_metadata()["current_writer_epoch"] == claimed[0].epoch:
                metadata = self.store.read_metadata()
                self.store.release_writer(
                    claimed[0],
                    expected_project_commit=int(metadata["current_project_commit"]),
                    expected_root_digest=str(metadata["current_root_digest"]),
                    expected_canonical_authority_digest=str(
                        metadata["canonical_authority_digest"]
                    ),
                )

    def test_bound_backup_captures_active_writer_without_auto_resume(self) -> None:
        metadata = self.store.read_metadata()
        lease = self.store.claim_writer(
            owner="active-backup-writer",
            creation_basis="online bound-backup test",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=str(
                metadata["canonical_authority_digest"]
            ),
        )
        try:
            backup = create_bound_backup(
                store=self.store,
                cas=self.cas,
                backup_id="active-writer-snapshot",
                authority_repo_root=REPO_ROOT,
                closure_id=self.closure.closure_id,
            )
            self.assertEqual(backup.manifest.store.current_writer_epoch, lease.epoch)
            self.assertEqual(backup.manifest.store.writer_lifecycle, "active")
            self.assertEqual(
                backup.manifest.takeover_classification,
                "inspection_only_active_writer",
            )
            self.assertEqual(backup.manifest.restore_test.status, "passed")
            observer = CaptureWorkspaceObserver()
            restored = prepare_side_by_side_restore(
                backup=backup,
                authority_repo_root=REPO_ROOT,
                observer=observer,
                observation_environment=ObservationEnvironment.CI,
            )
            self.assertEqual(restored.lifecycle, RecoveryLifecycle.VERIFIED_READ_ONLY)
            self.assertFalse(restored.mission_auto_resume)
            self.assertFalse(restored.selected_live_root)
            self.assertIn(EventKind.RESTORE_CHANGED, tuple(item.kind for item in observer.events))
            with self.assertRaises(RecoveryError) as takeover:
                prepare_takeover(restored, fence_proof=self.fence_reference)
            self.assertEqual(takeover.exception.code, "backup_inspection_only")
        finally:
            metadata = self.store.read_metadata()
            if metadata["current_writer_epoch"] == lease.epoch:
                self.store.release_writer(
                    lease,
                    expected_project_commit=int(metadata["current_project_commit"]),
                    expected_root_digest=str(metadata["current_root_digest"]),
                    expected_canonical_authority_digest=str(
                        metadata["canonical_authority_digest"]
                    ),
                )


    def test_backup_fails_if_snapshot_referenced_blob_disappears_before_copy(self) -> None:
        source = self.cas.path_for_digest(self.blob.sha256)
        held = self.paths.staging / "temporarily-held-required-blob"

        def hook(stage_name, _stage, _value) -> None:
            if stage_name == "after_sqlite_snapshot":
                source.rename(held)

        try:
            with self.assertRaises(RecoveryError) as captured:
                create_bound_backup(
                    store=self.store,
                    cas=self.cas,
                    backup_id="missing-required-during-copy",
                    authority_repo_root=REPO_ROOT,
                    closure_id=self.closure.closure_id,
                    fault_hook=hook,
                )
            self.assertEqual(captured.exception.code, "backup_cas_invalid")
        finally:
            if held.exists():
                held.rename(source)
        self.assertFalse(
            (self.paths.backups / "missing-required-during-copy").exists()
        )

    def test_backup_rejects_copied_closure_with_tampered_authority_at_write(self) -> None:
        def corrupt_copied_store(stage_name, stage, _backup_report, *, corruption) -> None:
            if stage_name != "after_sqlite_snapshot":
                return
            database = stage / "workspace.sqlite3"
            connection = sqlite3.connect(database)
            try:
                journals = tuple(
                    connection.execute(
                        "SELECT sequence_no, project_commit_no, auxiliary_writes_json "
                        "FROM transition_journal ORDER BY sequence_no"
                    )
                )
                origin = next(
                    row
                    for row in journals
                    if any(
                        item.get("table") == "closure_manifest"
                        and item.get("primary_key")
                        == {"closure_id": self.closure.closure_id}
                        for item in json.loads(str(row[2]))
                    )
                )
                if corruption == "origin_authority":
                    connection.execute(
                        "UPDATE project_commit SET canonical_authority_digest = ? "
                        "WHERE commit_no = ?",
                        ("f" * 64, int(origin[1])),
                    )
                else:
                    tail = journals[-1]
                    connection.execute(
                        "UPDATE transition_journal SET predecessor_digest = ? "
                        "WHERE sequence_no = ?",
                        ("f" * 64, int(tail[0])),
                    )
                connection.commit()
            finally:
                connection.close()

        for corruption in ("origin_authority", "journal_tail"):
            with self.subTest(corruption=corruption):
                backup_id = f"tampered-closure-origin-{corruption}"
                with self.assertRaises(RecoveryError) as captured:
                    create_bound_backup(
                        store=self.store,
                        cas=self.cas,
                        backup_id=backup_id,
                        authority_repo_root=REPO_ROOT,
                        closure_id=self.closure.closure_id,
                        fault_hook=lambda *args, _corruption=corruption: corrupt_copied_store(
                            *args,
                            corruption=_corruption,
                        ),
                    )
                self.assertEqual(captured.exception.code, "backup_closure_invalid")
                self.assertFalse((self.paths.backups / backup_id).exists())

    def test_restore_seals_direct_evidence_without_automatic_deletion(self) -> None:
        backup = self.create_backup()
        self.assertTrue((backup.path / self.blob.logical_path).is_file())
        restored = prepare_side_by_side_restore(
            backup=backup,
            authority_repo_root=REPO_ROOT,
        )
        self.assertEqual(restored.lifecycle, RecoveryLifecycle.VERIFIED_READ_ONLY)
        self.assertFalse(restored.selected_live_root)
        self.assertFalse(restored.mission_auto_resume)
        self.assertEqual(restored.root.parent, self.paths.recovery)
        self.assertTrue((restored.root / "read_only_seal.json").is_file())
        self.assertFalse(
            stat.S_IMODE((restored.root / "workspace.sqlite3").stat().st_mode)
            & stat.S_IWUSR
        )
        with self.assertRaises(sqlite3.OperationalError):
            connection = sqlite3.connect(restored.root / "workspace.sqlite3")
            try:
                connection.execute("CREATE TABLE forbidden_mutation(id TEXT PRIMARY KEY)")
            finally:
                connection.close()
        if os.name == "nt":
            with self.assertRaises(PermissionError):
                (restored.root / "forbidden-new-file").write_bytes(b"forbidden")
        fence = json.loads((restored.root / "deletion_fence.json").read_text(encoding="utf-8"))
        self.assertEqual(fence["deletion_directives"], [])
        self.assertEqual(fence["tombstones"], [])
        self.assertFalse(fence["automatic_byte_deletion"])
        self.assertTrue((restored.root / self.blob.logical_path).exists())
        self.assertEqual(
            restored.verification.evidence_count,
            len(self.closure.blob_sha256s),
        )
        self.assertTrue(self.cas.path_for_digest(self.blob.sha256).exists())
        self.assertEqual(self.canonical_path.read_bytes(), self.canonical_bytes)

    def test_incomplete_corrupt_missing_unsafe_or_fabricated_backup_fails_closed(self) -> None:
        incomplete = self.create_backup("incomplete")
        self.rewrite_manifest(incomplete.path, lambda value: value.__setitem__("complete", False))
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(incomplete.path)
        self.assertEqual(captured.exception.code, "backup_incomplete")

        corrupt_db = self.create_backup("corrupt-db")
        self.unlock_fixture_tree(corrupt_db.path)
        db_path = corrupt_db.path / "workspace.sqlite3"
        os.chmod(db_path, stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600)
        db_path.write_bytes(b"not sqlite")
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(corrupt_db.path)
        self.assertEqual(captured.exception.code, "backup_sqlite_corrupt")

        missing_blob = self.create_backup("missing-blob")
        self.unlock_fixture_tree(missing_blob.path)
        blob_path = missing_blob.path / self.blob.logical_path
        os.chmod(blob_path, stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600)
        blob_path.unlink()
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(missing_blob.path)
        self.assertEqual(captured.exception.code, "backup_cas_invalid")

        unsafe = self.create_backup("unsafe-path")
        def mutate(value):
            value["evidence_inventory"][0]["logical_path"] = "../../escape"
            value["evidence_root_sha256"] = digest(
                canonical_json_bytes(value["evidence_inventory"])
            )
        self.rewrite_manifest(unsafe.path, mutate)
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(unsafe.path)
        self.assertEqual(captured.exception.code, "backup_manifest_invalid")

        fabricated_restore = self.create_backup("fabricated-restore")
        self.rewrite_manifest(
            fabricated_restore.path,
            lambda value: value["restore_test"].__setitem__("status", "passed-without-test"),
        )
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(fabricated_restore.path)
        self.assertEqual(captured.exception.code, "backup_not_restore_tested")

        forged_restore_binding = self.create_backup("forged-restore-binding")

        def forge_restore_receipt(value):
            receipt = value["restore_test"]
            receipt["evidence_count"] = 999
            receipt_body = {
                key: item for key, item in receipt.items() if key != "receipt_sha256"
            }
            receipt["receipt_sha256"] = digest(canonical_json_bytes(receipt_body))

        self.rewrite_manifest(forged_restore_binding.path, forge_restore_receipt)
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(forged_restore_binding.path)
        self.assertEqual(captured.exception.code, "backup_not_restore_tested")

        forged_store = self.create_backup("forged-store-binding")
        self.rewrite_manifest(
            forged_store.path,
            lambda value: value["store"].__setitem__(
                "root_identity", digest("fabricated store root")
            ),
        )
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(forged_store.path)
        self.assertEqual(captured.exception.code, "backup_store_binding_invalid")

        forged_metadata = self.create_backup("forged-evidence-metadata")
        self.rewrite_manifest(
            forged_metadata.path,
            lambda value: value.__setitem__("evidence_metadata_sha256", "0" * 64),
        )
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(forged_metadata.path)
        self.assertEqual(captured.exception.code, "backup_evidence_metadata_invalid")

        forged_migration = self.create_backup("forged-migration-history")

        def forge_migration_history(value):
            history = value["store"]["migration_history"]
            history[0]["name"] = "caller-asserted-migration"
            value["store"]["migration_history_sha256"] = digest(
                canonical_json_bytes(history)
            )

        self.rewrite_manifest(forged_migration.path, forge_migration_history)
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(forged_migration.path)
        self.assertEqual(captured.exception.code, "backup_store_binding_invalid")

        displaced = self.root / "outside-workspace-paths" / "displaced"
        shutil.copytree(self.create_backup("displaced").path, displaced)
        with self.assertRaises(RecoveryError) as captured:
            verify_backup_set(displaced)
        self.assertEqual(captured.exception.code, "backup_path_invalid")

    def test_unclean_restart_and_takeover_pending_have_no_generic_active_transition(self) -> None:
        state = begin_recovery(
            RecoveryLifecycle.ACTIVE,
            writer_epoch=7,
            clean_shutdown_observed=False,
        )
        self.assertEqual(state.lifecycle, RecoveryLifecycle.RECOVERING)
        self.assertFalse(state.mission_auto_resume)
        verified = transition_recovery(
            state,
            RecoveryLifecycle.VERIFIED_READ_ONLY,
            reason="closure checks passed",
        )
        pending = transition_recovery(
            verified,
            RecoveryLifecycle.TAKEOVER_PENDING,
            reason="explicit fencing review",
        )
        with self.assertRaises(RecoveryError) as captured:
            transition_recovery(pending, RecoveryLifecycle.ACTIVE, reason="shortcut")
        self.assertEqual(captured.exception.code, "invalid_recovery_transition")


    def test_takeover_rejects_direct_evidence_that_is_not_a_writer_fence(self) -> None:
        backup = self.create_backup()
        self.assertEqual(backup.manifest.store.writer_epoch_high_watermark, 1)
        restored = prepare_side_by_side_restore(
            backup=backup,
            authority_repo_root=REPO_ROOT,
        )
        with self.assertRaises(RecoveryError) as captured:
            prepare_takeover(restored, fence_proof=self.fence_reference)
        self.assertEqual(captured.exception.code, "writer_fence_evidence_invalid")
        self.assertTrue((restored.root / "read_only_seal.json").is_file())
        self.assertFalse((restored.root / ".takeover-unseal.json").exists())
        source_metadata = self.store.read_metadata()
        self.assertEqual(source_metadata["lifecycle"], "quiesced")
        self.assertIsNone(source_metadata["current_writer_epoch"])

    def test_offline_migration_requires_the_verified_backup_object(self) -> None:
        backup = self.create_backup()
        with self.assertRaises(RecoveryError) as captured:
            prepare_offline_migration(
                backup=backup.manifest.manifest_sha256,  # type: ignore[arg-type]
                target_schema_version=backup.manifest.store.schema_version + 1,
                lifecycle=RecoveryLifecycle.OFFLINE,
            )
        self.assertEqual(captured.exception.code, "verified_backup_required")
        with self.assertRaises(RecoveryError) as captured:
            prepare_offline_migration(
                backup=backup,
                target_schema_version=backup.manifest.store.schema_version - 1,
                lifecycle=RecoveryLifecycle.OFFLINE,
            )
        self.assertEqual(captured.exception.code, "automatic_downgrade_forbidden")
        with self.assertRaises(RecoveryError) as captured:
            prepare_offline_migration(
                backup=backup,
                target_schema_version=backup.manifest.store.schema_version,
                lifecycle=RecoveryLifecycle.OFFLINE,
            )
        self.assertEqual(captured.exception.code, "migration_not_required")
        with self.assertRaises(RecoveryError) as captured:
            prepare_offline_migration(
                backup=backup,
                target_schema_version=backup.manifest.store.schema_version + 1,
                lifecycle=RecoveryLifecycle.OFFLINE,
            )
        self.assertEqual(captured.exception.code, "migration_target_unregistered")


class StagingPortableIdentityTests(unittest.TestCase):
    def test_portable_staging_schema11_operation_identity_binds_the_closed_pair(
        self,
    ) -> None:
        arguments = {
            "source_generation_id": "prod-20260906-01",
            "mission_id": "mission.rh.public.1",
            "source_backup_id": "asr0a.prod-20260906-01",
            "source_manifest_sha256": (
                "9142661c46957764f39400703fd10b5eca12b2565ce5f8b10b59c295873fecdd"
            ),
            "source_release_sha": "14e2826f10a6f7b8c9acb4fa36baf732e2b5c430",
            "migration_executor_release_sha": "c84e8b9f38201916bd4beb0524f3a3680ba4ec1f",
        }
        self.assertEqual(
            derive_staging_portable_migration_operation_id(
                **arguments, source_schema_version=10,
            ),
            "migration-18ea17488025301add8c3bb04e7bab6f2ecfcf6a3a8a174e83d44fc2d8c0b8da",
        )
        self.assertEqual(
            derive_staging_portable_migration_operation_id(
                **arguments, source_schema_version=9,
            ),
            derive_staging_portable_migration_operation_id(**arguments),
        )
        for invalid in (8, 11, True, "10"):
            with self.subTest(source_schema_version=invalid):
                with self.assertRaises(RecoveryError) as rejected:
                    derive_staging_portable_migration_operation_id(
                        **arguments, source_schema_version=invalid,
                    )
                self.assertEqual(
                    rejected.exception.code, "portable_migration_identity_invalid",
                )

    def test_portable_staging_migration_operation_id_preserves_v1_wire_identity(
        self,
    ) -> None:
        self.assertEqual(
            derive_staging_portable_migration_operation_id(
                source_generation_id="prod-20260906-01",
                mission_id="mission.rh.public.1",
                source_backup_id="asr0a.prod-20260906-01",
                source_manifest_sha256=(
                    "9142661c46957764f39400703fd10b5eca12b2565ce5f8b10b59c295873fecdd"
                ),
                source_release_sha=(
                    "14e2826f10a6f7b8c9acb4fa36baf732e2b5c430"
                ),
                migration_executor_release_sha=(
                    "c84e8b9f38201916bd4beb0524f3a3680ba4ec1f"
                ),
            ),
            "migration-56cb2519a286de9b91ccfeaaad898e04f151b7d739b3d46618187f26bf3de559",
        )


class StagingSchema10MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from test_workspace_schema12_migration import initialize_schema10_fixture

        self.fixture = WorkspaceRecoveryTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        with patch.object(
            WorkspaceStore,
            "initialize_direct_mission_workspace",
            side_effect=initialize_schema10_fixture,
        ):
            self.fixture.setUp()
        self.backup = self.fixture.create_backup("schema10-portable-migration-source")
        self.release_root, self.release_sha = self.fixture.installed_release_fixture()

    @staticmethod
    def _files(root: Path) -> dict[str, str]:
        return {
            item.relative_to(root).as_posix(): digest(item.read_bytes())
            for item in root.rglob("*")
            if item.is_file()
        }

    def _arguments(
        self,
        *,
        source_schema_version: int = 10,
        expected_manifest_sha256: str | None = None,
    ) -> dict[str, object]:
        manifest_sha256 = (
            self.backup.manifest.manifest_sha256
            if expected_manifest_sha256 is None else expected_manifest_sha256
        )
        identity = {
            "source_generation_id": "checkpoint-schema10-fixture",
            "mission_id": self.fixture.mission_id,
            "source_backup_id": self.backup.manifest.backup_id,
            "source_manifest_sha256": manifest_sha256,
            "source_release_sha": self.release_sha,
            "migration_executor_release_sha": "a" * 40,
        }
        operation_id = derive_staging_portable_migration_operation_id(
            **identity, source_schema_version=source_schema_version,
        )
        portable = self.fixture.portable_copy(
            self.backup,
            f"rh-staging/imports/{operation_id}/backups/"
            f"{self.backup.manifest.backup_id}",
        )
        return {
            "backup_directory": portable,
            "source_generation_id": identity["source_generation_id"],
            "migration_operation_id": operation_id,
            "migration_executor_release_sha": identity["migration_executor_release_sha"],
            "mission_id": self.fixture.mission_id,
            "expected_backup_id": self.backup.manifest.backup_id,
            "expected_manifest_sha256": manifest_sha256,
            "installed_release_root": self.release_root,
            "installed_release_sha": self.release_sha,
        }

    def test_schema10_portable_migration_replays_and_imports_once_without_source_changes(
        self,
    ) -> None:
        from research_core import workspace_recovery as recovery_module

        fixture = self.fixture
        arguments = self._arguments()
        portable = arguments["backup_directory"]
        assert isinstance(portable, Path)
        original_before = self._files(self.backup.path)
        portable_before = self._files(portable)
        source_metadata = fixture.store.read_metadata()
        source_mission = fixture.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, fixture.mission_id)
        )
        assert source_mission is not None

        semantic_verify = recovery_module._verify_recovery_snapshot_semantics
        rebackup = recovery_module.rebackup_verified_offline_migration
        report_from_unchanged = recovery_module._portable_backup_report_from_unchanged_verified
        migrated_path = (portable.parent / f"{self.backup.manifest.backup_id}.schema11").resolve()

        def migrate_with_closeout_trace():
            in_closeout = False
            events = []
            exact_backups = {portable: replace(self.backup, path=portable)}

            def finish_rebackup(*args, **kwargs):
                nonlocal in_closeout
                backup = rebackup(*args, **kwargs)
                exact_backups[backup.path] = backup
                in_closeout = True
                return backup

            def verify_semantics(authority, *args, **kwargs):
                binding = semantic_verify(authority, *args, **kwargs)
                if in_closeout:
                    self.assertEqual(binding, exact_backups[authority.backup_path].manifest.store)
                    events.append(("semantics", authority.backup_path, binding))
                return binding

            def report_unchanged(backup):
                self.assertTrue(in_closeout)
                self.assertEqual(backup, exact_backups[backup.path])
                events.append(("guarded_report", backup.path, backup.manifest.store))
                return report_from_unchanged(backup)

            with patch.object(
                recovery_module, "rebackup_verified_offline_migration", side_effect=finish_rebackup,
            ), patch.object(
                recovery_module, "_verify_recovery_snapshot_semantics", side_effect=verify_semantics,
            ), patch.object(
                recovery_module, "_portable_backup_report_from_unchanged_verified", side_effect=report_unchanged,
            ):
                outcome = migrate_portable_backup_to_staging(**arguments)
            # Publication and closeout retain the executor's result, never
            # re-auditing the unchanged copied source or target.
            self.assertEqual(events, [
                ("guarded_report", migrated_path, outcome.backup.manifest.store),
            ])
            return outcome

        result = migrate_with_closeout_trace()
        self.assertEqual(
            (
                result.source.backup_schema_version,
                result.source_root_digest_version,
                result.migrated.backup_schema_version,
                result.migrated_root_digest_version,
            ),
            (10, 6, 11, 6),
        )
        self.assertEqual(
            result.backup.path,
            (portable.parent / f"{self.backup.manifest.backup_id}.schema11").resolve(),
        )
        self.assertEqual(result.backup.manifest.canonical, self.backup.manifest.canonical)
        self.assertEqual(result.backup.manifest.closure, self.backup.manifest.closure)
        self.assertEqual(
            result.backup.manifest.evidence_inventory,
            self.backup.manifest.evidence_inventory,
        )
        payload = result.to_payload()
        self.assertEqual(
            payload["schema_version"],
            "mathematical_research.staging_portable_migration_report.v3",
        )
        self.assertEqual(payload["migration"]["source_schema_version"], 10)
        self.assertEqual(payload["migration"]["target_schema_version"], 11)
        self.assertFalse(payload["migration"]["selected_live_root"])
        self.assertFalse(payload["migration"]["mission_auto_resume"])
        self.assertTrue(payload["rollback"]["source_backup_preserved"])
        self.assertEqual(payload["mission_mutations"], 0)
        self.assertFalse(payload["provider_effect"])
        self.assertEqual(payload["canonical_effect"], "none")
        migrated_before = self._files(result.backup.path)
        replay = migrate_with_closeout_trace()
        self.assertEqual(replay.to_payload(), payload)
        self.assertEqual(replay.backup, result.backup)

        for relocated in (portable, result.backup.path):
            with self.subTest(relocated=relocated.name):
                with self.assertRaises(RecoveryError) as source_bound:
                    verify_backup_set(relocated)
                self.assertEqual(
                    source_bound.exception.code, "backup_store_binding_invalid",
                )

        target_parent = fixture.root / "rh-staging" / "workspaces"
        target_parent.mkdir(parents=True)
        target = (target_parent / fixture.mission_id).resolve(strict=False)
        fixture.addCleanup(
            _remove_tree, target, owned_parent=target_parent, ignore_errors=True,
        )
        import_arguments = {
            "backup_directory": result.backup.path,
            "target_root": target,
            "installed_release_root": self.release_root,
            "installed_release_sha": self.release_sha,
            "mission_id": fixture.mission_id,
            "expected_backup_id": result.backup.manifest.backup_id,
            "expected_manifest_sha256": result.backup.manifest.manifest_sha256,
        }
        with self.assertRaises(RecoveryError) as wrong_release:
            import_portable_backup_to_staging(
                **{**import_arguments, "installed_release_sha": "e" * 40},
            )
        self.assertEqual(wrong_release.exception.code, "installed_release_invalid")
        self.assertFalse(target.exists())

        imported = import_portable_backup_to_staging(**import_arguments)
        self.assertEqual(imported.status, "staging_imported_quiesced")
        self.assertEqual(
            imported.writer_owner,
            stable_principal_owner_binding(attest_current_principal()),
        )
        self.assertEqual(imported.source_root_identity, self.backup.manifest.store.root_identity)
        self.assertNotEqual(imported.target_root_identity, imported.source_root_identity)
        self.assertEqual(imported.mission_mutations, 0)
        self.assertFalse(imported.provider_effect)
        self.assertEqual(imported.canonical_effect, "none")
        self.assertTrue(imported.source_material_unchanged)
        staging_store = WorkspaceStore.open(
            WorkspacePaths.from_root(target), expected_project_id=fixture.project_id,
        )
        metadata = staging_store.read_metadata()
        self.assertEqual((metadata["schema_version"], metadata["root_digest_version"]), (11, 6))
        self.assertEqual(metadata["lifecycle"], "quiesced")
        self.assertIsNone(metadata["current_writer_epoch"])
        integrity = staging_store.verify_integrity()
        self.assertEqual(integrity.current_project_commit, result.migrated.project_commit)
        self.assertEqual(integrity.current_root_digest, result.migrated.project_root_digest)
        imported_mission = staging_store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, fixture.mission_id)
        )
        assert imported_mission is not None
        self.assertEqual(imported_mission.payload, source_mission.payload)
        self.assertEqual(imported_mission.payload_digest, source_mission.payload_digest)
        self.assertEqual(self._files(self.backup.path), original_before)
        self.assertEqual(self._files(portable), portable_before)
        self.assertEqual(self._files(result.backup.path), migrated_before)
        self.assertEqual(fixture.store.read_metadata(), source_metadata)

    def test_schema10_portable_migration_rejects_mismatched_pair_source_and_operation(
        self,
    ) -> None:
        original_before = self._files(self.backup.path)
        valid = self._arguments()
        cases = (
            (
                self._arguments(source_schema_version=9),
                "portable_migration_source_unsupported",
            ),
            (
                self._arguments(expected_manifest_sha256="0" * 64),
                "portable_migration_source_mismatch",
            ),
            (
                {**valid, "migration_executor_release_sha": "b" * 40},
                "portable_migration_identity_invalid",
            ),
            (
                {**valid, "migration_operation_id": "migration-" + "0" * 64},
                "portable_migration_identity_invalid",
            ),
        )
        for arguments, code in cases:
            with self.subTest(expected_error=code, operation=arguments["migration_operation_id"]):
                portable = arguments["backup_directory"]
                assert isinstance(portable, Path)
                before = self._files(portable)
                with self.assertRaises(RecoveryError) as rejected:
                    migrate_portable_backup_to_staging(**arguments)
                self.assertEqual(rejected.exception.code, code)
                self.assertEqual(self._files(portable), before)
                self.assertEqual(
                    {item.name for item in portable.parent.iterdir()},
                    {self.backup.manifest.backup_id},
                )
        self.assertEqual(self._files(self.backup.path), original_before)


class StagingSchema9ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        from test_workspace_schema10_migration import initialize_schema9_fixture

        self.fixture = WorkspaceRecoveryTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        with patch.object(
            WorkspaceStore,
            "initialize_direct_mission_workspace",
            side_effect=initialize_schema9_fixture,
        ):
            self.fixture.setUp()

    def test_staging_import_preserves_schema9_root5_without_auto_migration(
        self,
    ) -> None:
        fixture = self.fixture
        backup = fixture.create_backup("schema9-staging-source")
        portable = fixture.portable_copy(backup, "portable-schema9-staging")
        release_root, release_sha = fixture.installed_release_fixture()
        target_parent = fixture.root / "rh-staging" / "workspaces"
        target_parent.mkdir(parents=True)
        target = (target_parent / fixture.mission_id).resolve(strict=False)
        fixture.addCleanup(
            _remove_tree,
            target,
            owned_parent=target_parent,
            ignore_errors=True,
        )

        imported = import_portable_backup_to_staging(
            backup_directory=portable,
            target_root=target,
            installed_release_root=release_root,
            installed_release_sha=release_sha,
            mission_id=fixture.mission_id,
            expected_backup_id=backup.manifest.backup_id,
            expected_manifest_sha256=backup.manifest.manifest_sha256,
        )

        target_store = WorkspaceStore.open(
            WorkspacePaths.from_root(target),
            expected_project_id=fixture.project_id,
        )
        metadata = target_store.read_metadata()
        mission = target_store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, fixture.mission_id)
        )
        self.assertIsNotNone(mission)
        assert mission is not None
        source_mission = fixture.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, fixture.mission_id)
        )
        self.assertIsNotNone(source_mission)
        assert source_mission is not None
        self.assertEqual(
            (metadata["schema_version"], metadata["root_digest_version"]),
            (9, 5),
        )
        self.assertEqual(
            metadata["current_project_commit"], imported.project_commit
        )
        self.assertEqual(
            metadata["current_root_digest"], imported.project_root_digest
        )
        self.assertEqual(metadata["current_writer_epoch"], None)
        self.assertEqual(metadata["lifecycle"], "quiesced")
        self.assertEqual(mission.payload_digest, source_mission.payload_digest)

    def test_portable_staging_migration_is_side_by_side_and_source_preserving(
        self,
    ) -> None:
        fixture = self.fixture
        backup = fixture.create_backup("schema9-portable-migration-source")
        source_generation_id = "prod-20260906-01"
        release_root, release_sha = fixture.installed_release_fixture()
        migration_executor_release_sha = "a" * 40
        migration_operation_id = derive_staging_portable_migration_operation_id(
            source_generation_id=source_generation_id,
            mission_id=fixture.mission_id,
            source_backup_id=backup.manifest.backup_id,
            source_manifest_sha256=backup.manifest.manifest_sha256,
            source_release_sha=release_sha,
            migration_executor_release_sha=migration_executor_release_sha,
        )
        portable = fixture.portable_copy(
            backup,
            f"rh-staging/imports/{migration_operation_id}/backups/"
            f"{backup.manifest.backup_id}",
        )
        migrated_backup_id = f"{backup.manifest.backup_id}.schema10"
        source_before = {
            item.relative_to(portable).as_posix(): digest(item.read_bytes())
            for item in portable.rglob("*")
            if item.is_file()
        }

        result = migrate_portable_backup_to_staging(
            backup_directory=portable,
            source_generation_id=source_generation_id,
            migration_operation_id=migration_operation_id,
            migration_executor_release_sha=migration_executor_release_sha,
            mission_id=fixture.mission_id,
            expected_backup_id=backup.manifest.backup_id,
            expected_manifest_sha256=backup.manifest.manifest_sha256,
            installed_release_root=release_root,
            installed_release_sha=release_sha,
        )

        self.assertIsInstance(result, StagingPortableMigrationResult)
        self.assertEqual(
            result.backup.path,
            (portable.parent / migrated_backup_id).resolve(strict=True),
        )
        self.assertTrue(portable.is_dir())
        self.assertTrue(result.backup.path.is_dir())
        self.assertEqual(
            {
                item.relative_to(portable).as_posix(): digest(item.read_bytes())
                for item in portable.rglob("*")
                if item.is_file()
            },
            source_before,
        )
        self.assertEqual(
            (
                result.source.backup_schema_version,
                result.source_root_digest_version,
                result.migrated.backup_schema_version,
                result.migrated_root_digest_version,
            ),
            (9, 5, 10, 6),
        )
        payload = result.to_payload()
        self.assertEqual(
            payload["schema_version"],
            "mathematical_research.staging_portable_migration_report.v2",
        )
        self.assertEqual(payload["status"], "staging_migrated_side_by_side")
        self.assertEqual(payload["source_generation_id"], source_generation_id)
        self.assertEqual(payload["migration_operation_id"], migration_operation_id)
        self.assertEqual(
            payload["migration_executor_release_sha"],
            migration_executor_release_sha,
        )
        self.assertEqual(payload["source_release_sha"], release_sha)
        self.assertEqual(
            payload["migration"]["operation_id"],
            migration_operation_id,
        )
        self.assertEqual(
            payload["migration"]["operation_sha256"],
            migration_operation_id.removeprefix("migration-"),
        )
        self.assertEqual(
            payload["migration"]["executor_release_sha"],
            migration_executor_release_sha,
        )
        self.assertEqual(
            payload["rollback"],
            {
                "source_backup_preserved": True,
                "source_backup_id": backup.manifest.backup_id,
                "source_manifest_sha256": backup.manifest.manifest_sha256,
                "selected_live_root": False,
                "mission_auto_resume": False,
            },
        )
        self.assertFalse(payload["provider_effect"])
        self.assertEqual(payload["canonical_effect"], "none")
        self.assertEqual(payload["mission_mutations"], 0)
        self.assertTrue(payload["migrated"]["semantic_verification_passed"])
        self.assertTrue(payload["migrated"]["structural_verification_passed"])
        json.dumps(payload)

        with self.assertRaises(RecoveryError) as source_bound:
            verify_backup_set(portable)
        self.assertEqual(source_bound.exception.code, "backup_store_binding_invalid")
        with self.assertRaises(RecoveryError) as migrated_source_bound:
            verify_backup_set(result.backup.path)
        self.assertEqual(
            migrated_source_bound.exception.code,
            "backup_store_binding_invalid",
        )

        replay = migrate_portable_backup_to_staging(
            backup_directory=portable,
            source_generation_id=source_generation_id,
            migration_operation_id=migration_operation_id,
            migration_executor_release_sha=migration_executor_release_sha,
            mission_id=fixture.mission_id,
            expected_backup_id=backup.manifest.backup_id,
            expected_manifest_sha256=backup.manifest.manifest_sha256,
            installed_release_root=release_root,
            installed_release_sha=release_sha,
        )
        self.assertEqual(replay.to_payload(), payload)
        self.assertEqual(replay.backup, result.backup)

    def test_portable_staging_migration_rejects_legacy_unprefixed_project_identity(
        self,
    ) -> None:
        from test_workspace_schema10_migration import initialize_schema9_fixture

        legacy = WorkspaceRecoveryTests(methodName="runTest")
        legacy.fixture_project_id = "riemann_hypothesis"
        with patch.object(
            WorkspaceStore,
            "initialize_direct_mission_workspace",
            side_effect=initialize_schema9_fixture,
        ):
            legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        backup = legacy.create_backup("legacy-project-id-migration-source")
        source_generation_id = "prod-legacy-project-id"
        release_root, release_sha = legacy.installed_release_fixture()
        migration_executor_release_sha = "a" * 40
        migration_operation_id = derive_staging_portable_migration_operation_id(
            source_generation_id=source_generation_id,
            mission_id=legacy.mission_id,
            source_backup_id=backup.manifest.backup_id,
            source_manifest_sha256=backup.manifest.manifest_sha256,
            source_release_sha=release_sha,
            migration_executor_release_sha=migration_executor_release_sha,
        )
        portable = legacy.portable_copy(
            backup,
            f"rh-staging/imports/{migration_operation_id}/backups/"
            f"{backup.manifest.backup_id}",
        )

        with self.assertRaises(RecoveryError) as captured:
            migrate_portable_backup_to_staging(
                backup_directory=portable,
                source_generation_id=source_generation_id,
                migration_operation_id=migration_operation_id,
                migration_executor_release_sha=migration_executor_release_sha,
                mission_id=legacy.mission_id,
                expected_backup_id=backup.manifest.backup_id,
                expected_manifest_sha256=backup.manifest.manifest_sha256,
                installed_release_root=release_root,
                installed_release_sha=release_sha,
            )

        self.assertEqual(
            captured.exception.code,
            "portable_migration_source_unsupported",
        )

    def test_portable_staging_migration_binds_candidate_operations_and_replays(
        self,
    ) -> None:
        fixture = self.fixture
        backup = fixture.create_backup("schema9-portable-migration-ab-source")
        source_generation_id = "prod-20260906-01"
        release_root, release_sha = fixture.installed_release_fixture()
        results: dict[str, StagingPortableMigrationResult] = {}
        source_hashes: dict[str, dict[str, str]] = {}

        for label, executor_sha in (("candidate-a", "c" * 40), ("candidate-b", "d" * 40)):
            operation_id = derive_staging_portable_migration_operation_id(
                source_generation_id=source_generation_id,
                mission_id=fixture.mission_id,
                source_backup_id=backup.manifest.backup_id,
                source_manifest_sha256=backup.manifest.manifest_sha256,
                source_release_sha=release_sha,
                migration_executor_release_sha=executor_sha,
            )
            portable = fixture.portable_copy(
                backup,
                f"rh-staging/imports/{operation_id}/backups/"
                f"{backup.manifest.backup_id}",
            )
            source_hashes[label] = {
                item.relative_to(portable).as_posix(): digest(item.read_bytes())
                for item in portable.rglob("*")
                if item.is_file()
            }
            result = migrate_portable_backup_to_staging(
                backup_directory=portable,
                source_generation_id=source_generation_id,
                migration_operation_id=operation_id,
                migration_executor_release_sha=executor_sha,
                mission_id=fixture.mission_id,
                expected_backup_id=backup.manifest.backup_id,
                expected_manifest_sha256=backup.manifest.manifest_sha256,
                installed_release_root=release_root,
                installed_release_sha=release_sha,
            )
            replay = migrate_portable_backup_to_staging(
                backup_directory=portable,
                source_generation_id=source_generation_id,
                migration_operation_id=operation_id,
                migration_executor_release_sha=executor_sha,
                mission_id=fixture.mission_id,
                expected_backup_id=backup.manifest.backup_id,
                expected_manifest_sha256=backup.manifest.manifest_sha256,
                installed_release_root=release_root,
                installed_release_sha=release_sha,
            )
            self.assertEqual(replay.to_payload(), result.to_payload())
            self.assertEqual(replay.backup, result.backup)
            self.assertEqual(
                {
                    item.relative_to(portable).as_posix(): digest(item.read_bytes())
                    for item in portable.rglob("*")
                    if item.is_file()
                },
                source_hashes[label],
            )
            results[label] = result

        candidate_a = results["candidate-a"]
        candidate_b = results["candidate-b"]
        self.assertNotEqual(
            candidate_a.migration_operation_id,
            candidate_b.migration_operation_id,
        )
        self.assertNotEqual(candidate_a.migration_id, candidate_b.migration_id)
        self.assertNotEqual(candidate_a.backup.path, candidate_b.backup.path)
        self.assertTrue(candidate_a.backup.path.is_dir())
        self.assertTrue(candidate_b.backup.path.is_dir())
        self.assertEqual(candidate_a.source_generation_id, source_generation_id)
        self.assertEqual(candidate_b.source_generation_id, source_generation_id)
        self.assertEqual(candidate_a.migration_executor_release_sha, "c" * 40)
        self.assertEqual(candidate_b.migration_executor_release_sha, "d" * 40)
        with self.assertRaises(RecoveryError) as rebound_executor:
            migrate_portable_backup_to_staging(
                backup_directory=(
                    candidate_a.backup.path.parent / backup.manifest.backup_id
                ),
                source_generation_id=source_generation_id,
                migration_operation_id=candidate_a.migration_operation_id,
                migration_executor_release_sha="d" * 40,
                mission_id=fixture.mission_id,
                expected_backup_id=backup.manifest.backup_id,
                expected_manifest_sha256=backup.manifest.manifest_sha256,
                installed_release_root=release_root,
                installed_release_sha=release_sha,
            )
        self.assertEqual(
            rebound_executor.exception.code,
            "portable_migration_identity_invalid",
        )

    def test_portable_staging_migration_scope_is_failure_closed(self) -> None:
        fixture = self.fixture
        backup = fixture.create_backup("schema9-portable-migration-failure")
        source_generation_id = "prod-20260906-01"
        release_root, release_sha = fixture.installed_release_fixture()
        migration_executor_release_sha = "f" * 40
        migration_operation_id = derive_staging_portable_migration_operation_id(
            source_generation_id=source_generation_id,
            mission_id=fixture.mission_id,
            source_backup_id=backup.manifest.backup_id,
            source_manifest_sha256=backup.manifest.manifest_sha256,
            source_release_sha=release_sha,
            migration_executor_release_sha=migration_executor_release_sha,
        )
        portable = fixture.portable_copy(
            backup,
            f"rh-staging/imports/{migration_operation_id}/backups/"
            f"{backup.manifest.backup_id}",
        )
        source_before = {
            item.relative_to(portable).as_posix(): digest(item.read_bytes())
            for item in portable.rglob("*")
            if item.is_file()
        }

        with patch(
            "research_core.migration_executor.execute_offline_migration",
            side_effect=RuntimeError("injected portable migration failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected portable migration failure",
            ):
                migrate_portable_backup_to_staging(
                    backup_directory=portable,
                    source_generation_id=source_generation_id,
                    migration_operation_id=migration_operation_id,
                    migration_executor_release_sha=(
                        migration_executor_release_sha
                    ),
                    mission_id=fixture.mission_id,
                    expected_backup_id=backup.manifest.backup_id,
                    expected_manifest_sha256=backup.manifest.manifest_sha256,
                    installed_release_root=release_root,
                    installed_release_sha=release_sha,
                )

        with self.assertRaises(RecoveryError) as source_bound:
            verify_backup_set(portable)
        self.assertEqual(source_bound.exception.code, "backup_store_binding_invalid")
        self.assertEqual(
            {
                item.relative_to(portable).as_posix(): digest(item.read_bytes())
                for item in portable.rglob("*")
                if item.is_file()
            },
            source_before,
        )
        self.assertFalse(
            (portable.parent / f"{backup.manifest.backup_id}.schema10").exists()
        )


if __name__ == "__main__":
    unittest.main()
