from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core import migration_executor as migration_executor_module  # noqa: E402
from research_core.context_revision import (  # noqa: E402
    commit_context_revision,
    issue_context_revision,
)
from research_core.formal_session import canonical_formal_session_id  # noqa: E402
from research_core.migration_executor import (  # noqa: E402
    MigrationExecutionError,
    _inspect_database,
    execute_offline_migration,
)
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    ResolvedOwnerRevision,
    prepare_direct_continuation_checkpoint,
    prepare_direct_executive_epoch_checkpointed_event,
)
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core import retrospective_cut as retrospective_cut_module  # noqa: E402
from research_core.retrospective_cut import FrozenRetrospectiveCut  # noqa: E402
from research_core import workspace_recovery as recovery_module  # noqa: E402
from research_core.mission_evidence import (  # noqa: E402
    CaptureScope,
    RawCaptureArtifactInput,
    commit_capture_scope_annotation,
    commit_evidence_meaning,
    commit_raw_capture,
    prepare_capture_scope_annotation,
    prepare_evidence_meaning,
    prepare_raw_capture,
    read_evidence_meaning,
)
from research_core.evidence_store import (  # noqa: E402
    EvidenceItemRevision,
    prepare_evidence_security_disposition,
)
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_recovery import (  # noqa: E402
    RecoveryLifecycle,
    prepare_offline_migration,
    rebackup_verified_offline_migration,
    verify_portable_backup_set,
)
from research_core.workspace_schema import (  # noqa: E402
    APPLICATION_VERSION,
    WorkspaceSchemaError,
    _connect_workspace,
    _iter_statements,
    canonical_payload,
    is_current_store_generation,
    is_direct_mission_generation,
    load_migrations,
    verify_schema,
    verify_schema_contract,
)
from research_core.workspace_store import (  # noqa: E402
    WorkspaceIntegrityError,
    WorkspaceStore,
    _current_a2_scope_holds,
    _current_dependency_lookup,
    _current_mission_continuity,
    _closure_security_records,
    _validated_commit_envelope,
    _verify_current_dependency_projection_full,
    _verified_live_canonical_authority,
    _workspace_root_digest_for_connection,
)
from research_core import workspace_store as store_module  # noqa: E402
from research_core_test_support import (  # noqa: E402
    independent_root6_body_at_commit,
    independent_root6_digest,
    independent_root6_member_mutations,
)
import test_workspace_recovery as recovery_fixture  # noqa: E402

REPO_ROOT = recovery_fixture.REPO_ROOT


def initialize_schema9_fixture(paths, *, project_id, canonical_snapshot, actor, **kwargs):
    """Build the frozen registered schema9 genesis, then use real Store writes."""

    paths.root.mkdir(parents=True)
    paths = WorkspacePaths.from_root(paths.root)
    for directory in (paths.cas_sha256, paths.staging, paths.backups, paths.recovery):
        directory.mkdir(parents=True, exist_ok=True)
    authority = canonical_payload(_verified_live_canonical_authority(canonical_snapshot))
    now = "2026-09-07T00:00:00Z"
    connection = _connect_workspace(paths.database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        for migration in load_migrations()[:9]:
            for statement in _iter_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migration(version,name,digest_sha256,applied_at) "
                "VALUES(?,?,?,?)",
                (migration.version, migration.name, migration.digest_sha256, now),
            )
        connection.execute("PRAGMA user_version=9")
        connection.execute("PRAGMA foreign_keys=ON")
        root = _workspace_root_digest_for_connection(
            connection, project_id=project_id,
            canonical_authority_digest=authority.sha256,
            root_digest_version=5, operating_mode="mission_runtime",
        )
        connection.execute(
            "INSERT INTO workspace_metadata(singleton,project_id,application_version,"
            "schema_version,root_identity,lifecycle,canonical_authority_json,"
            "canonical_authority_digest,current_writer_epoch,current_project_commit,"
            "current_root_digest,transition_head_digest,created_at,updated_at,"
            "operating_mode,root_digest_version) "
            "VALUES(1,?,?,9,?,'offline',?,?,NULL,0,?,NULL,?,?,'mission_runtime',5)",
            (project_id, APPLICATION_VERSION, paths.root_identity, authority.text,
             authority.sha256, root, now, now),
        )
        connection.execute(
            "INSERT INTO project_commit(commit_no,project_id,root_digest,"
            "canonical_authority_digest,transition_head_digest,command_id,"
            "created_by,created_at) VALUES(0,?,?,?,NULL,NULL,?,?)",
            (project_id, root, authority.sha256, actor, now),
        )
        verify_schema(connection, schema_version=9)
    finally:
        connection.close()
    return WorkspaceStore.open(paths, expected_project_id=project_id)


class Schema10ContractTests(unittest.TestCase):
    def test_migrations_0001_through_0009_remain_byte_frozen(self):
        expected = {
            1: "cf4f855aefb956c49b136d80d51ff4ad5b9bae1cf7a4eae04b36dfc4ab18a4b7",
            2: "1d01f35fb0e7d4ea19ed4e9a8d06fa3cd17ee2d42496c70838e20658c5995558",
            3: "24b59b9e82076b50ef8950ea2ce31de46e8de0403b4f991d3ac08ccbc26ab572",
            4: "82b0a3d64e004ffc8797926fd8604eb3587973bf9141f8ca31433f53f5a29c69",
            5: "dcc911051f148645eb533314065e1f0b25ca0323aa5300538a069a08e8cd7a13",
            6: "1986e197826a1fc0b6dfa6ab03d0d6ae0221ff13290519e47fd2361f3b3a9767",
            7: "6083884cf5ed700400dade29c1c932945af4084932b52116ac394b21b21d5b77",
            8: "2246d7420b65c413b9a804faa33bc1e9073e05732dee1ec92af93c3885882c96",
            9: "cd2b34758f9c4b45f4a49956af6fecb6998175e4301f5d57542a4ede06ed09de",
        }
        self.assertEqual(
            {item.version: item.digest_sha256 for item in load_migrations()[:9]},
            expected,
        )

    def test_contract_validation_has_no_sqlite_or_foreign_key_scan(self):
        with tempfile.TemporaryDirectory(prefix="rh-schema10-contract-") as temporary:
            connection = _connect_workspace(Path(temporary) / "workspace.sqlite3")
            self.addCleanup(connection.close)
            connection.execute("PRAGMA foreign_keys=OFF")
            for migration in load_migrations()[:10]:
                for statement in _iter_statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migration VALUES(?,?,?,?)",
                    (migration.version, migration.name, migration.digest_sha256, "fixture"),
                )
            connection.execute("PRAGMA user_version=10")
            connection.execute("PRAGMA foreign_keys=ON")
            traced = []
            connection.set_trace_callback(traced.append)
            contract = verify_schema_contract(connection, schema_version=10)
            self.assertEqual(contract.schema_version, 10)
            self.assertFalse(any("integrity_check" in q or "foreign_key_check" in q for q in traced))
            for table, index in (
                ("transition_journal", "transition_journal_project_commit"),
                ("command_result", "command_result_project_commit"),
            ):
                plan = tuple(connection.execute(
                    f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE project_commit_no=?", (1,),
                ))
                self.assertTrue(any(index in str(row[3]) for row in plan), plan)
            migration_plan = tuple(
                connection.execute(
                    "EXPLAIN QUERY PLAN SELECT execution_id, attempt_id "
                    "FROM migration_execution "
                    "INDEXED BY migration_execution_target_schema "
                    "WHERE target_schema_version = ? LIMIT 2",
                    (10,),
                )
            )
            self.assertTrue(
                any(
                    "migration_execution_target_schema" in str(row[3])
                    for row in migration_plan
                ),
                migration_plan,
            )
            owner_plan = tuple(
                connection.execute(
                    "EXPLAIN QUERY PLAN SELECT transition_id "
                    "FROM workspace_root_contract_transition "
                    "WHERE target_schema_version = ? LIMIT 2",
                    (10,),
                )
            )
            self.assertTrue(
                any(
                    "sqlite_autoindex_workspace_root_contract_transition_5"
                    in str(row[3])
                    for row in owner_plan
                ),
                owner_plan,
            )
            connection.execute("PRAGMA foreign_keys=OFF")
            try:
                with self.assertRaisesRegex(
                    WorkspaceSchemaError,
                    "foreign key enforcement is disabled",
                ):
                    verify_schema_contract(connection, schema_version=10)
            finally:
                connection.execute("PRAGMA foreign_keys=ON")
            for sql, parameters, index in (
                (
                    "SELECT project_commit_no FROM transition_journal "
                    "WHERE command_kind='commit_raw_capture' AND "
                    "json_extract(auxiliary_writes_json, '$[#-1].primary_key.capture_id')=?",
                    ("capture.fixture",), "transition_journal_capture_id",
                ),
                (
                    "SELECT * FROM transition_journal WHERE command_kind=? AND "
                    "json_extract(authorization_json, '$.target_canonical_authority_digest')=? "
                    "ORDER BY sequence_no",
                    ("rebind", "a" * 64), "transition_journal_canonical_target",
                ),
                (
                    "SELECT * FROM continuation_checkpoint WHERE mission_id=? "
                    "AND project_commit_no<? ORDER BY project_commit_no DESC, "
                    "checkpoint_id DESC LIMIT 1",
                    ("mission.fixture", 5), "continuation_checkpoint_mission_commit",
                ),
                (
                    "SELECT rowid AS seek_rowid, capture_id FROM raw_capture "
                    "INDEXED BY raw_capture_mission_rowid_seek "
                    "WHERE mission_id=? AND rowid<=? ORDER BY rowid LIMIT ?",
                    ("mission.fixture", 100, 2),
                    "raw_capture_mission_rowid_seek",
                ),
                (
                    "SELECT rowid AS seek_rowid, capture_id FROM raw_capture "
                    "INDEXED BY raw_capture_mission_rowid_seek "
                    "WHERE mission_id=? AND rowid<=? AND rowid>? "
                    "ORDER BY rowid LIMIT ?",
                    ("mission.fixture", 100, 1, 2),
                    "raw_capture_mission_rowid_seek",
                ),
            ):
                plan = tuple(connection.execute("EXPLAIN QUERY PLAN " + sql, parameters))
                self.assertTrue(any(index in str(row[3]) for row in plan), plan)
                self.assertFalse(
                    any("USE TEMP B-TREE FOR ORDER BY" in str(row[3]) for row in plan),
                    plan,
                )
            traced.clear()
            self.assertEqual(verify_schema(connection, schema_version=10).integrity_check, "ok")
            self.assertTrue(any("integrity_check" in q for q in traced))
            self.assertTrue(any("foreign_key_check" in q for q in traced))
            connection.execute("CREATE TEMP TABLE shadow(value)")
            with self.assertRaisesRegex(WorkspaceSchemaError, "temporary"):
                verify_schema_contract(connection, schema_version=10)
            connection.execute("DROP TABLE temp.shadow")
            connection.execute("ATTACH DATABASE ':memory:' AS unexpected")
            with self.assertRaisesRegex(WorkspaceSchemaError, "attached"):
                verify_schema_contract(connection, schema_version=10)
            connection.close()

    def test_generation_selection_is_exact(self):
        self.assertFalse(is_current_store_generation(10, 6, "mission_runtime"))
        self.assertFalse(is_current_store_generation(9, 5, "mission_runtime"))
        self.assertTrue(is_direct_mission_generation(9, 5, "mission_runtime"))
        self.assertTrue(is_direct_mission_generation(10, 6, "mission_runtime"))
        for pair in ((9, 6), (10, 5), (8, 4), (11, 5)):
            self.assertFalse(is_direct_mission_generation(*pair, "mission_runtime"))


class Schema10MigrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery_fixture.WorkspaceRecoveryTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        with patch.object(
            WorkspaceStore, "initialize_direct_mission_workspace",
            side_effect=initialize_schema9_fixture,
        ):
            self.fixture.setUp()

    def test_schema9_frozen_retrospective_cut_uses_its_registered_contract(self):
        fixture = self.fixture
        lease, _checkpoint = fixture.complete_checkpoint_and_keep_writer()
        try:
            checkpoint = fixture.store.read_continuation_checkpoint(
                executive_epoch_id=fixture.epoch_id
            )
            self.assertIsNotNone(checkpoint)
            assert checkpoint is not None
            cut = fixture.root / "retrospective-schema9"
            cut.mkdir()
            report = fixture.store.export_verified_backup(
                cut / "workspace.sqlite3"
            )
            shutil.copytree(fixture.paths.root / "cas", cut / "cas")
            expected = {
                "mission_id": fixture.mission_id,
                "selected_release_sha": "b" * 40,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "project_commit": report.project_commit,
            }
            envelope = {
                "mission_id": fixture.mission_id,
                "selected_release_sha": "b" * 40,
                "checkpoint_ref": {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "payload_sha256": checkpoint["payload_digest"],
                },
                "store_backup": {
                    **asdict(report),
                    "target": str(report.target),
                },
            }
            (cut / "cut.json").write_text(
                json.dumps(envelope), encoding="utf-8"
            )

            # Omitting expected_binding exercises the actual owner-frozen
            # schema9-only branch, not the adaptable test seam.
            with patch.object(
                retrospective_cut_module, "EXPECTED_CUT_BINDING", expected
            ):
                with FrozenRetrospectiveCut(cut) as reader:
                    metadata = reader.read("table:workspace_metadata")["rows"][0]
                    self.assertEqual(
                        (
                            metadata["schema_version"],
                            metadata["root_digest_version"],
                            metadata["operating_mode"],
                        ),
                        (9, 5, "mission_runtime"),
                    )
                    self.assertTrue(reader.inventory()["retained_history"])
        finally:
            fixture.release_writer(lease)

    def test_schema9_takeover_open_uses_exact_registered_generation(self):
        fixture = self.fixture
        backup = fixture.create_backup("schema9-takeover-open")

        def permit_for(root: Path):
            paths = WorkspacePaths.from_root(root)
            high_watermark = backup.manifest.store.writer_epoch_high_watermark
            plan = recovery_module.TakeoverPlan(
                restored_root=paths.root,
                source_root_identity=backup.manifest.store.root_identity,
                target_root_identity=paths.root_identity,
                backup_manifest_sha256=backup.manifest.manifest_sha256,
                canonical=backup.manifest.canonical,
                prior_epoch=high_watermark,
                prior_owner="source-writer" if high_watermark else None,
                proposed_epoch=high_watermark + 1,
                lifecycle=RecoveryLifecycle.TAKEOVER_PENDING,
                fence_evidence_sha256=(
                    fixture.fence_reference.evidence_payload_sha256
                ),
                fence_evidence_id=fixture.fence_reference.evidence_id,
                fence_evidence_revision=fixture.fence_reference.evidence_revision,
                restored_root_sha256="a" * 64,
                read_only_seal_sha256="b" * 64,
                reconciliation=recovery_module.ExternalReconciliation(),
                _issuer_token=recovery_module._RECOVERY_ISSUER_TOKEN,
            )
            return paths, recovery_module.TakeoverPermit(
                plan=plan,
                command_id="test.schema9.open-takeover-target",
                actor="test.schema9",
                explicit=True,
                canonical_freshness_sha256=backup.manifest.canonical.sha256,
                canonical_authority_root=REPO_ROOT,
                _issuer_token=recovery_module._RECOVERY_ISSUER_TOKEN,
            )

        paths, permit = permit_for(backup.path)
        before = hashlib.sha256(paths.database.read_bytes()).hexdigest()
        opened = WorkspaceStore.open_takeover_target(
            paths,
            permit,
            expected_project_id=fixture.project_id,
        )
        metadata = opened.read_metadata()
        self.assertEqual(
            (metadata["schema_version"], metadata["root_digest_version"]),
            (9, 5),
        )
        self.assertEqual(hashlib.sha256(paths.database.read_bytes()).hexdigest(), before)

        for field, value in (
            ("root_digest_version", 4),
            ("operating_mode", "inactive_foundation"),
            ("schema_version", 10),
        ):
            with self.subTest(field=field, value=value):
                copied = fixture.portable_copy(
                    backup, f"schema9-takeover-{field}"
                )
                fixture.unlock_fixture_tree(copied)
                database = copied / "workspace.sqlite3"
                os.chmod(
                    database,
                    stat.S_IMODE(database.stat().st_mode) | stat.S_IWUSR,
                )
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        f"UPDATE workspace_metadata SET {field} = ? "
                        "WHERE singleton = 1",
                        (value,),
                    )
                    connection.commit()
                changed_paths, changed_permit = permit_for(copied)
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "takeover target generation is not a direct Mission Store",
                ):
                    WorkspaceStore.open_takeover_target(
                        changed_paths,
                        changed_permit,
                        expected_project_id=fixture.project_id,
                    )

    @staticmethod
    def _rows(database):
        connection = sqlite3.connect(database)
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            rows = {}
            for table in tables:
                if table in {"workspace_metadata", "schema_migration"}:
                    continue
                # Compare every pre-schema10 field exactly. The new optional
                # journal projections must remain NULL for all historical rows.
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
                           if row[1] not in {
                               "changed_head_rows_json",
                               "result_payload_digest",
                               "current_dependency_root_digest",
                               "current_dependency_entry_count",
                               "capture_coverage_root_digest",
                               "capture_coverage_entry_count",
                               "project_commit_no" if table == "evidence_item_revision" else "",
                           }]
                selected = ", ".join(f'"{column}"' for column in columns)
                rows[table] = tuple(connection.execute(f'SELECT {selected} FROM "{table}" ORDER BY rowid'))
            return rows
        finally:
            connection.close()

    @staticmethod
    def _schema_contract_rows(database):
        connection = sqlite3.connect(database)
        try:
            return (
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                tuple(
                    connection.execute(
                        "SELECT * FROM workspace_metadata ORDER BY singleton"
                    )
                ),
                tuple(
                    connection.execute(
                        "SELECT * FROM schema_migration ORDER BY version"
                    )
                ),
            )
        finally:
            connection.close()

    def _advance_strategy_revision(
        self,
        store: WorkspaceStore,
        *,
        command_id: str,
        marker: str,
    ):
        metadata = store.read_metadata()
        lease = store.claim_writer(
            owner=f"test.schema10.{marker}",
            creation_basis=f"advance Strategy for {marker}",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=self.fixture.authority_digest,
        )
        head = store.get_head(
            store_module.TypedWorkspaceId(
                store_module.IdentityKind.STRATEGY,
                "strategy.recovery",
            )
        )
        assert head is not None
        payload = json.loads(store_module.canonical_json_bytes(head.payload))
        payload["integrated_comparison"] = (
            str(payload["integrated_comparison"]) + f" [{marker}]"
        )
        try:
            store.commit_strategy_revision(
                executive_epoch_id=self.fixture.epoch_id,
                mission_id=self.fixture.mission_id,
                strategy_id="strategy.recovery",
                payload=payload,
                expected_head_revision=head.reference.revision,
                expected_head_payload_digest=head.payload_digest,
                dependency_heads=None,
                lease=lease,
                command_id=command_id,
                actor=f"test.schema10.{marker}",
                expected_canonical_authority_digest=self.fixture.authority_digest,
            )
            return store.get_head(
                store_module.TypedWorkspaceId(
                    store_module.IdentityKind.STRATEGY,
                    "strategy.recovery",
                )
            )
        finally:
            current = store.read_metadata()
            if current["current_writer_epoch"] is not None:
                store.release_writer(
                    lease,
                    expected_project_commit=int(current["current_project_commit"]),
                    expected_root_digest=str(current["current_root_digest"]),
                    expected_canonical_authority_digest=self.fixture.authority_digest,
                )

    def _seed_terminal_formal_session(self):
        """Persist one real root5 open-then-terminal formal Session history."""

        fixture = self.fixture
        store = fixture.store
        metadata = store.read_metadata()
        lease = store.claim_writer(
            owner="test.schema10.formal-session-audit",
            creation_basis="seed root5 formal Session audit history",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=fixture.authority_digest,
        )
        try:
            mission = store.get_head(
                store_module.TypedWorkspaceId(
                    store_module.IdentityKind.MISSION,
                    fixture.mission_id,
                )
            )
            strategy = store.get_head(
                store_module.TypedWorkspaceId(
                    store_module.IdentityKind.STRATEGY,
                    "strategy.recovery",
                )
            )
            assert mission is not None and strategy is not None
            mission_ref = {
                "kind": "mission",
                "identity": fixture.mission_id,
                "revision": mission.reference.revision,
                "payload_sha256": mission.payload_digest,
            }
            context_record = issue_context_revision(
                authority=fixture.epoch_authority,
                context_id="context.schema10.formal-session-audit",
                purpose="Exercise migrated root5 formal Session audit custody.",
                question="Does the full audit reuse its one validated journal index?",
                indispensable_ground=(
                    {
                        "reference": mission_ref,
                        "why": "it fixes the exact Mission whose Session is audited",
                    },
                ),
                owner_source_references=(
                    {
                        "reference": mission_ref,
                        "retrieval": "direct current Mission owner revision",
                        "provenance": "active Executive Epoch Mission root",
                    },
                ),
                known_omissions=("unrelated formal work",),
                restricted_uses=("regression fixture only",),
                independence_treatment={"method": "bounded exact regression"},
                restrictions=("no canonical mathematical effect",),
                invalidation_conditions=(
                    {
                        "reference": mission_ref,
                        "condition": "the active Mission root changes",
                    },
                ),
            )
            commit_context_revision(
                store,
                authority=fixture.epoch_authority,
                record=context_record,
                lease=lease,
                actor="test.schema10.formal-session-audit",
                command_id="schema9.formal-session-audit.context",
            )
            context = store.get_head(
                store_module.TypedWorkspaceId(
                    store_module.IdentityKind.CONTEXT,
                    str(context_record.document["context_id"]),
                )
            )
            assert context is not None
            strategy_ref = {
                "kind": "strategy",
                "identity": "strategy.recovery",
                "revision": strategy.reference.revision,
                "payload_sha256": strategy.payload_digest,
            }
            context_ref = {
                "kind": "context",
                "identity": str(context_record.document["context_id"]),
                "revision": context.reference.revision,
                "payload_sha256": context.payload_digest,
            }
            selected_bet_sha256 = "7" * 64
            session_id = canonical_formal_session_id(
                project_id=fixture.project_id,
                mission_ref=mission_ref,
                strategy_ref=strategy_ref,
                selected_bet_sha256=selected_bet_sha256,
                context_ref=context_ref,
            )
            dependencies = {
                f"{reference['kind']}:{reference['identity']}": (
                    int(reference["revision"]),
                    str(reference["payload_sha256"]),
                )
                for reference in (mission_ref, strategy_ref, context_ref)
            }
            open_payload = {
                "schema_version": 1,
                "kind": "formal_session",
                "project_id": fixture.project_id,
                "mission_id": fixture.mission_id,
                "session_id": session_id,
                "lifecycle": "open",
                "mission_ref": mission_ref,
                "strategy_ref": strategy_ref,
                "selected_bet_sha256": selected_bet_sha256,
                "context_ref": context_ref,
                "terminal_binding": None,
            }
            opened = store.commit_formal_session_revision(
                executive_epoch_id=fixture.epoch_id,
                mission_id=fixture.mission_id,
                session_id=session_id,
                payload=open_payload,
                expected_head_revision=None,
                expected_head_payload_digest=None,
                dependency_heads=dependencies,
                lease=lease,
                command_id="schema9.formal-session-audit.open",
                actor="test.schema10.formal-session-audit",
                expected_canonical_authority_digest=fixture.authority_digest,
            )
            self.assertEqual(opened.changed_heads[0].revision, 1)
            terminal_payload = {
                **open_payload,
                "lifecycle": "terminal",
                "terminal_binding": {
                    "attempt_result_ref": {
                        "kind": "workstation_attempt_result",
                        "attempt_id": "attempt.schema9.formal-session-audit",
                        "session_id": session_id,
                        "attempt_state": "failed",
                        "result_digest_sha256": "8" * 64,
                    },
                    "raw_capture_ref": None,
                },
            }
            terminal = store.commit_formal_session_revision(
                executive_epoch_id=fixture.epoch_id,
                mission_id=fixture.mission_id,
                session_id=session_id,
                payload=terminal_payload,
                expected_head_revision=1,
                expected_head_payload_digest=canonical_payload(open_payload).sha256,
                dependency_heads=dependencies,
                lease=lease,
                command_id="schema9.formal-session-audit.terminal",
                actor="test.schema10.formal-session-audit",
                expected_canonical_authority_digest=fixture.authority_digest,
            )
            return session_id, (opened.project_commit, terminal.project_commit)
        finally:
            current = store.read_metadata()
            if current["current_writer_epoch"] is not None:
                store.release_writer(
                    lease,
                    expected_project_commit=int(current["current_project_commit"]),
                    expected_root_digest=str(current["current_root_digest"]),
                    expected_canonical_authority_digest=fixture.authority_digest,
                )

    def _seed_legacy_current_dependencies(
        self,
        *,
        a2_scope_count=1,
        directive_lifecycle="active",
    ):
        """Add valid root5-only A2/security state and rebind its current root."""

        if directive_lifecycle not in {"active", "closed"}:
            raise ValueError("fixture directive lifecycle must be active or closed")

        fixture = self.fixture
        blob_record = replace(
            fixture.cas.ingest_bytes(
                b"legacy disposed payload",
                original_name="legacy-disposed.bin",
                media_type="application/octet-stream",
            ).record,
            availability_state="verified_available",
        )
        blob_sha256 = blob_record.sha256
        evidence = EvidenceItemRevision(
            evidence_id="evidence.legacy.security",
            revision=1,
            subtype="security_disposition",
            subject={"scope": "legacy migration fixture"},
            exact_scope="one exact unavailable legacy revision",
            rigor="diagnostic",
            limitations=("security disposition only",),
            non_inferences=("does not establish RH",),
            security_classification="restricted",
            retention="security-policy",
            blob_roles=(("primary", blob_sha256),),
            availability_state="unavailable",
        )
        disposition = prepare_evidence_security_disposition(
            evidence=evidence,
            authorization={
                "authorization_kind": "migration_fixture",
                "authorized_by": "owner",
                "scope": {
                    "evidence_id": evidence.evidence_id,
                    "revision": evidence.revision,
                },
            },
            reason="Exact legacy disposition.",
            blob_sha256s=(blob_sha256,),
            directive_id="directive.legacy.security",
            tombstone_id="tombstone.legacy.security",
        )
        scope_ids = tuple(
            f"scope.legacy.{number:04d}" for number in range(a2_scope_count)
        )
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            metadata = connection.execute(
                "SELECT * FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            assert metadata is not None
            connection.execute(
                "INSERT INTO alert_event VALUES "
                "('alert.legacy.a2', 'A2', ?, 1, 'fixture')",
                (canonical_payload({"summary": "legacy A2"}).text,),
            )
            connection.execute(
                "INSERT INTO hold VALUES "
                "('hold.legacy.a2', 'alert.legacy.a2', ?, 'open', 'fixture')",
                (canonical_payload(scope_ids).text,),
            )
            connection.execute(
                "INSERT INTO blob(sha256, byte_length, media_type, encoding, "
                "integrity_state, availability_state, logical_cas_path, "
                "first_verified_at, last_verified_at, quarantine_state, "
                "quarantine_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    blob_record.sha256,
                    blob_record.length,
                    blob_record.media_type,
                    blob_record.encoding,
                    blob_record.integrity_state,
                    blob_record.availability_state,
                    blob_record.logical_path,
                    blob_record.first_verified_at,
                    blob_record.last_verified_at,
                    blob_record.quarantine_state,
                    blob_record.quarantine_reason,
                ),
            )
            evidence_payload = evidence.to_payload()
            connection.execute(
                "INSERT INTO evidence_item_revision("
                "evidence_id, revision, subtype, subject_json, exact_scope_json, "
                "rigor, limitations_json, non_inferences_json, security_json, "
                "retention_json, availability_state, canonical_effect, "
                "payload_digest, created_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'none', ?, 'fixture')",
                (
                    evidence.evidence_id,
                    evidence.revision,
                    evidence.subtype,
                    canonical_payload(evidence.subject).text,
                    canonical_payload({"scope": evidence.exact_scope}).text,
                    evidence.rigor,
                    canonical_payload(list(evidence.limitations)).text,
                    canonical_payload(list(evidence.non_inferences)).text,
                    canonical_payload(
                        {"classification": evidence.security_classification}
                    ).text,
                    canonical_payload({"policy": evidence.retention}).text,
                    evidence.availability_state,
                    canonical_payload(evidence_payload).sha256,
                ),
            )
            connection.execute(
                "INSERT INTO evidence_blob VALUES (?, ?, 0, 'primary', ?)",
                (evidence.evidence_id, evidence.revision, blob_sha256),
            )
            current_commit = int(metadata["current_project_commit"])
            evidence_digest = canonical_payload(evidence_payload).sha256
            connection.execute(
                "INSERT INTO evidence_item_head VALUES (?, ?, ?, ?)",
                (
                    evidence.evidence_id,
                    evidence.revision,
                    evidence_digest,
                    current_commit,
                ),
            )
            directive = disposition.directive
            tombstone = disposition.tombstone
            connection.execute(
                "INSERT INTO deletion_directive VALUES (?, ?, ?, ?, 'fixture')",
                (
                    directive.directive_id,
                    canonical_payload(
                        {
                            "authorization_sha256": (
                                disposition.authorization_sha256
                            )
                        }
                    ).text,
                    canonical_payload(
                        {
                            "reason": directive.reason,
                            "blob_sha256s": list(directive.blob_sha256s),
                            "evidence_references": [
                                list(item)
                                for item in directive.evidence_references
                            ],
                        }
                    ).text,
                    directive_lifecycle,
                ),
            )
            connection.execute(
                "INSERT INTO evidence_tombstone VALUES "
                "(?, ?, ?, ?, ?, 'fixture')",
                (
                    tombstone.tombstone_id,
                    tombstone.evidence_id,
                    tombstone.evidence_revision,
                    tombstone.directive_id,
                    canonical_payload(
                        {"reason_sha256": tombstone.reason_sha256}
                    ).text,
                ),
            )

            # This fixture represents legacy rows emitted by the last root5
            # command.  Bind every added row to that already authoritative
            # transition so migration exercises a valid, recoverable source
            # rather than laundering unjournaled bytes.
            added_rows = (
                (store_module.AuxiliaryTable.ALERT_EVENT, {"alert_id": "alert.legacy.a2"}),
                (store_module.AuxiliaryTable.HOLD, {"hold_id": "hold.legacy.a2"}),
                (store_module.AuxiliaryTable.BLOB, {"sha256": blob_sha256}),
                (
                    store_module.AuxiliaryTable.EVIDENCE_ITEM_REVISION,
                    {"evidence_id": evidence.evidence_id, "revision": evidence.revision},
                ),
                (
                    store_module.AuxiliaryTable.EVIDENCE_BLOB,
                    {
                        "evidence_id": evidence.evidence_id,
                        "evidence_revision": evidence.revision,
                        "ordinal": 0,
                    },
                ),
                (
                    store_module.AuxiliaryTable.DELETION_DIRECTIVE,
                    {"directive_id": directive.directive_id},
                ),
                (
                    store_module.AuxiliaryTable.EVIDENCE_TOMBSTONE,
                    {"tombstone_id": tombstone.tombstone_id},
                ),
            )
            journal_row = connection.execute(
                "SELECT * FROM transition_journal WHERE project_commit_no = ?",
                (current_commit,),
            ).fetchone()
            assert journal_row is not None
            auxiliary_writes = list(json.loads(str(journal_row["auxiliary_writes_json"])))
            for table, primary_key in added_rows:
                spec = store_module._ROOT5_AUXILIARY_SPECS[table]
                predicate = " AND ".join(f"{column} = ?" for column in spec.primary_key)
                row = connection.execute(
                    f"SELECT * FROM {table.value} WHERE {predicate}",
                    tuple(primary_key[column] for column in spec.primary_key),
                ).fetchone()
                assert row is not None
                auxiliary_writes.append(
                    {
                        "table": table.value,
                        "primary_key": primary_key,
                        "row_digest": store_module._row_digest(
                            table.value,
                            {column: row[column] for column in spec.columns},
                        ),
                    }
                )
            auxiliary_payload = canonical_payload(auxiliary_writes)
            evidence_head_advances = list(
                json.loads(str(journal_row["evidence_head_advances_json"]))
            )
            evidence_head_advances.append(
                {
                    "evidence_id": evidence.evidence_id,
                    "expected_revision": None,
                    "expected_payload_digest": None,
                    "target_revision": evidence.revision,
                    "target_payload_digest": evidence_digest,
                }
            )
            evidence_head_payload = canonical_payload(evidence_head_advances)
            authorization_digest = canonical_payload(
                json.loads(str(journal_row["authorization_json"]))
            ).sha256
            transition_body = {
                "sequence_no": int(journal_row["sequence_no"]),
                "project_id": str(journal_row["project_id"]),
                "project_commit_no": int(journal_row["project_commit_no"]),
                "command_id": str(journal_row["command_id"]),
                "command_kind": str(journal_row["command_kind"]),
                "request_digest": str(journal_row["request_digest"]),
                "actor": str(journal_row["actor"]),
                "writer_epoch": int(journal_row["writer_epoch"]),
                "changed_heads": json.loads(str(journal_row["changed_heads_json"])),
                "auxiliary_writes_digest": auxiliary_payload.sha256,
                "evidence_head_advances_digest": evidence_head_payload.sha256,
                "authorization_digest": authorization_digest,
                "canonical_effect": str(journal_row["canonical_effect"]),
                "predecessor_digest": journal_row["predecessor_digest"],
                "created_at": str(journal_row["created_at"]),
            }
            transition_digest = hashlib.sha256(
                store_module.canonical_json_bytes(transition_body)
            ).hexdigest()
            connection.execute(
                "UPDATE transition_journal SET auxiliary_writes_json = ?, "
                "auxiliary_writes_digest = ?, evidence_head_advances_json = ?, "
                "evidence_head_advances_digest = ?, digest_sha256 = ? "
                "WHERE project_commit_no = ?",
                (
                    auxiliary_payload.text,
                    auxiliary_payload.sha256,
                    evidence_head_payload.text,
                    evidence_head_payload.sha256,
                    transition_digest,
                    current_commit,
                ),
            )

            root_digest = _workspace_root_digest_for_connection(
                connection,
                project_id=fixture.project_id,
                canonical_authority_digest=fixture.authority_digest,
                root_digest_version=5,
                operating_mode="mission_runtime",
            )
            result_row = connection.execute(
                "SELECT result_json FROM command_result "
                "WHERE project_commit_no = ?",
                (current_commit,),
            ).fetchone()
            assert result_row is not None
            result = json.loads(str(result_row["result_json"]))
            result["root_digest"] = root_digest
            result["transition_digest"] = transition_digest
            result["auxiliary_writes_digest"] = auxiliary_payload.sha256
            result["auxiliary_write_count"] = len(auxiliary_writes)
            result["evidence_head_advances_digest"] = (
                evidence_head_payload.sha256
            )
            result_payload = canonical_payload(result)
            connection.execute(
                "UPDATE command_result SET result_json = ?, result_digest = ? "
                "WHERE project_commit_no = ?",
                (result_payload.text, result_payload.sha256, current_commit),
            )
            connection.execute(
                "UPDATE project_commit SET root_digest = ?, "
                "transition_head_digest = ? WHERE commit_no = ?",
                (root_digest, transition_digest, current_commit),
            )
            connection.execute(
                "UPDATE workspace_metadata SET current_root_digest = ?, "
                "transition_head_digest = ? "
                "WHERE singleton = 1",
                (root_digest, transition_digest),
            )
            connection.commit()
        WorkspaceStore.open(
            fixture.paths,
            expected_project_id=fixture.project_id,
        ).verify_integrity()
        return scope_ids, evidence, blob_sha256, disposition

    def test_schema9_backup_migration_rebackup_and_portable_verification(self):
        fixture = self.fixture
        with fixture.store.direct_recovery_read_scope():
            self.assertEqual(
                fixture.store.read_root_retrieval_cut()["route"],
                "legacy_root_exhaustive_compatibility",
            )
        source = fixture.create_backup("schema9-source")
        source_hash = hashlib.sha256(source.path.joinpath("workspace.sqlite3").read_bytes()).hexdigest()
        portable9 = verify_portable_backup_set(source.path)
        self.assertTrue(portable9.semantic_verification_passed)
        self.assertEqual(source.manifest.store.schema_version, 9)
        before = self._rows(fixture.paths.database)
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            self.assertNotIn(
                "project_commit_no",
                {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(evidence_item_revision)"
                    )
                },
            )
        plan = prepare_offline_migration(
            backup=source, target_schema_version=10, lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan, backup=source, migration_id="schema10-migration", actor="test.schema10",
        )
        self.assertEqual(result.target_schema_version, 10)
        migrated = rebackup_verified_offline_migration(
            source_backup=source, migration_result=result, backup_id="schema10-rebackup",
            authority_repo_root=REPO_ROOT,
        )
        self.assertEqual(migrated.manifest.store.schema_version, 10)
        self.assertEqual(migrated.manifest.store.root_digest_version, 6)
        self.assertEqual(migrated.manifest.store.project_commit_id, source.manifest.store.project_commit_id + 1)
        self.assertEqual(migrated.manifest.canonical, source.manifest.canonical)
        self.assertEqual(migrated.manifest.evidence_inventory, source.manifest.evidence_inventory)
        inspection_database = fixture.root / "schema10-rebackup-inspection.sqlite3"
        shutil.copyfile(migrated.path / "workspace.sqlite3", inspection_database)
        with closing(_connect_workspace(inspection_database)) as connection:
            metadata = connection.execute(
                "SELECT * FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(
                store_module._root_retrieval_route_from_connection(
                    connection,
                    metadata,
                ),
                "migrated_root5_exhaustive_compatibility",
            )
            contract = verify_schema_contract(connection, schema_version=10)
            body, stored_root = independent_root6_body_at_commit(
                connection,
                schema_contract={
                    "schema_version": contract.schema_version,
                    "migration_set_digest": contract.migration_digest,
                    "schema_object_digest": contract.schema_object_digest,
                },
                project_commit_no=int(metadata["current_project_commit"]),
            )
            transition = connection.execute(
                "SELECT source_project_commit, source_root_digest, "
                "source_transition_head_digest, canonical_authority_digest, "
                "target_schema_version, target_migration_set_digest, "
                "target_schema_object_digest FROM "
                "workspace_root_contract_transition "
                "WHERE target_schema_version = 10"
            ).fetchone()
            self.assertIsNotNone(transition)
            assert transition is not None
            self.assertEqual(len(body), 10)
            self.assertEqual(
                body["schema_contract"],
                {
                    "schema_version": int(transition["target_schema_version"]),
                    "migration_set_digest": str(
                        transition["target_migration_set_digest"]
                    ),
                    "schema_object_digest": str(
                        transition["target_schema_object_digest"]
                    ),
                },
            )
            self.assertEqual(
                body["predecessor_project_commit_no"],
                int(transition["source_project_commit"]),
            )
            self.assertEqual(
                body["predecessor_root_digest"],
                str(transition["source_root_digest"]),
            )
            self.assertEqual(
                body["predecessor_transition_head_digest"],
                (
                    None
                    if transition["source_transition_head_digest"] is None
                    else str(transition["source_transition_head_digest"])
                ),
            )
            self.assertEqual(
                body["canonical_authority_digest"],
                str(transition["canonical_authority_digest"]),
            )
            self.assertIsNotNone(body["predecessor_transition_head_digest"])
            self.assertIsNotNone(body["transition_head_digest"])
            self.assertEqual(independent_root6_digest(body), stored_root)
            nulled_members = set()
            for member, mutation in independent_root6_member_mutations(body):
                with self.subTest(root6_member=member):
                    self.assertNotEqual(
                        independent_root6_digest(mutation),
                        stored_root,
                    )
                if mutation[member] is None:
                    nulled_members.add(member)
            self.assertEqual(
                nulled_members,
                {
                    "predecessor_project_commit_no",
                    "predecessor_root_digest",
                    "predecessor_transition_head_digest",
                    "transition_head_digest",
                },
            )
        portable10 = verify_portable_backup_set(migrated.path)
        self.assertTrue(portable10.semantic_verification_passed)
        self.assertEqual(portable10.project_root_digest, migrated.manifest.store.project_root_digest)
        after = self._rows(migrated.path / "workspace.sqlite3")
        with closing(sqlite3.connect(migrated.path / "workspace.sqlite3")) as connection:
            projections = tuple(connection.execute(
                "SELECT changed_head_rows_json, result_payload_digest "
                "FROM transition_journal ORDER BY sequence_no"
            ))
            migration_result = json.loads(
                str(
                    connection.execute(
                        "SELECT result_json FROM command_result "
                        "ORDER BY project_commit_no DESC LIMIT 1"
                    ).fetchone()[0]
                )
            )
            migrated_evidence_origins = tuple(
                connection.execute(
                    "SELECT project_commit_no FROM evidence_item_revision "
                    "ORDER BY evidence_id, revision"
                )
            )
        migration_result_digest = canonical_payload("schema10_root6_applied").sha256
        self.assertEqual(
            projections[:-1],
            ((None, None),) * len(before["transition_journal"]),
        )
        self.assertEqual(projections[-1], ("[]", migration_result_digest))
        self.assertEqual(migration_result["result"], "schema10_root6_applied")
        self.assertEqual(
            canonical_payload(migration_result["result"]).sha256,
            migration_result_digest,
        )
        self.assertTrue(migrated_evidence_origins)
        self.assertTrue(all(row == (None,) for row in migrated_evidence_origins))
        appended = {"writer_epoch", "project_commit", "transition_journal", "command_result",
                    "migration_execution", "workspace_root_contract_transition"}
        for table, rows in before.items():
            if table in appended:
                self.assertEqual(after[table][:len(rows)], rows, table)
                self.assertEqual(len(after[table]), len(rows) + 1, table)
            else:
                self.assertEqual(after[table], rows, table)
        self.assertEqual(self._rows(fixture.paths.database), before)
        self.assertEqual(hashlib.sha256(source.path.joinpath("workspace.sqlite3").read_bytes()).hexdigest(), source_hash)

        # These operations consume the earned result/backup bytes. An unrelated
        # full source, target or logical reconstruction must not be hidden here.
        with (
            patch.object(
                migration_executor_module, "_inspect_database",
                side_effect=AssertionError("unchanged migration target was audited again"),
            ),
            patch.object(
                migration_executor_module, "_logical_database_digest",
                side_effect=AssertionError("unchanged migration logical digest was rebuilt"),
            ),
            patch.object(
                recovery_module, "_verify_backup_for_trust_transition",
                side_effect=AssertionError("unchanged migration source was audited again"),
            ),
        ):
            self.assertIs(
                migration_executor_module._verify_issued_offline_migration_result(
                    result, plan=plan, backup=source,
                ),
                result,
            )
            self.assertIs(
                migration_executor_module._verify_offline_migration_result_copy(
                    result, plan=plan, backup=source,
                    database=migrated.path / "workspace.sqlite3",
                ),
                result,
            )
            self.assertEqual(
                migration_executor_module._inspect_passed_offline_migration(
                    migration_id=result.migration_id, plan=plan, backup=source,
                    target_backup=migrated,
                ),
                result.to_payload(),
            )
            forged_store = replace(
                migrated.manifest.store, project_root_digest="a" * 64,
            )
            with self.assertRaises(MigrationExecutionError):
                migration_executor_module._inspect_passed_offline_migration(
                    migration_id=result.migration_id, plan=plan, backup=source,
                    target_backup=replace(
                        migrated, manifest=replace(migrated.manifest, store=forged_store),
                    ),
                )
            with self.assertRaises(MigrationExecutionError) as forged:
                replace(result, _artifact_files=()).verify_issued()
            self.assertEqual(forged.exception.code, "migration_result_not_issued")

            copied = fixture.root / "altered-migration-copy.sqlite3"
            shutil.copyfile(result.database, copied)
            with copied.open("ab") as handle:
                handle.write(b"not the audited image")
            os.chmod(copied, stat.S_IREAD)
            with self.assertRaises(MigrationExecutionError) as changed_copy:
                migration_executor_module._verify_offline_migration_result_copy(
                    result, plan=plan, backup=source, database=copied,
                )
            self.assertEqual(changed_copy.exception.code, "migration_result_copy_invalid")

            for name in ("attempt-0001.outcome.json", "workspace.sqlite3"):
                with self.subTest(changed_artifact_file=name):
                    path = result.artifact / name
                    original = path.read_bytes()
                    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                    try:
                        path.write_bytes(original + b" ")
                        os.chmod(path, stat.S_IREAD)
                        with self.assertRaises(MigrationExecutionError) as changed_result:
                            migration_executor_module._verify_issued_offline_migration_result(
                                result, plan=plan, backup=source,
                            )
                        self.assertEqual(changed_result.exception.code, "migration_result_stale")
                    finally:
                        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                        path.write_bytes(original)
                        os.chmod(path, stat.S_IREAD)

            receipt = result.artifact / "attempt-0001.outcome.json"
            original = receipt.read_bytes()
            os.chmod(receipt, stat.S_IREAD | stat.S_IWRITE)
            try:
                changed = json.loads(original)
                changed["completed_at"] = "altered-completion"
                receipt.write_bytes(canonical_payload(changed).text.encode("utf-8"))
                os.chmod(receipt, stat.S_IREAD)
                with self.assertRaises(MigrationExecutionError):
                    migration_executor_module._inspect_passed_offline_migration(
                        migration_id=result.migration_id, plan=plan, backup=source,
                        target_backup=migrated,
                    )
            finally:
                os.chmod(receipt, stat.S_IREAD | stat.S_IWRITE)
                receipt.write_bytes(original)
                os.chmod(receipt, stat.S_IREAD)

    def test_schema9_orphan_target_execution_fails_audit_and_reconciliation(self):
        fixture = self.fixture
        source = fixture.create_backup("schema9-before-orphan-target-execution")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )

        def insert_orphan(database: Path) -> None:
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "INSERT INTO migration_execution("
                    "execution_id, attempt_id, source_schema_version, "
                    "target_schema_version, verified_backup_manifest_sha256, "
                    "applied_history_sha256, plan_sha256, started_at, completed_at, "
                    "status, canonical_effect) "
                    "VALUES (?, ?, 9, 10, ?, ?, ?, ?, ?, 'applied', 'none')",
                    (
                        "migration.orphan-schema10",
                        "orphan-schema10.attempt.0001",
                        "1" * 64,
                        "2" * 64,
                        "3" * 64,
                        "2026-09-07T00:00:00Z",
                        "2026-09-07T00:00:01Z",
                    ),
                )
                connection.commit()

        insert_orphan(fixture.paths.database)
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "pre-root6 Store has a schema10 migration transition or witness",
        ):
            fixture.store.verify_integrity()

        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary) / "workspace.sqlite3"
            shutil.copy2(source.path / "workspace.sqlite3", staged)
            os.chmod(
                staged,
                stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
            )
            insert_orphan(staged)
            connection = _connect_workspace(staged)
            try:
                logical_sha256 = (
                    migration_executor_module._logical_database_digest(
                        connection
                    )
                )
                inspection = _inspect_database(
                    connection,
                    state={
                        "migration_id": "reject-orphan-schema10-execution",
                        "source_logical_sha256": logical_sha256,
                    },
                    plan=plan,
                    verified=source,
                    plan_sha256="4" * 64,
                )
            finally:
                connection.close()
        self.assertEqual(inspection.classification, "ambiguous")

    def test_migrated_route_cannot_become_native_by_deleting_provenance(self):
        fixture = self.fixture
        source = fixture.create_backup("schema9-route-provenance-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id="schema10-route-provenance",
            actor="test.schema10",
        )
        source_uri = f"{result.database.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, isolation_level=None)
        ) as source_connection, closing(
            _connect_workspace(fixture.paths.database)
        ) as target_connection:
            source_connection.backup(target_connection)
        fixture.store = WorkspaceStore.open(
            fixture.paths,
            expected_project_id=fixture.project_id,
        )

        lease, _checkpoint = fixture.complete_checkpoint_and_keep_writer()
        fixture.release_writer(lease)
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            transition = connection.execute(
                "SELECT transition_id, migration_execution_id "
                "FROM workspace_root_contract_transition "
                "WHERE target_schema_version = 10"
            ).fetchone()
            self.assertIsNotNone(transition)
            assert transition is not None
            connection.execute(
                "DELETE FROM workspace_root_transition_retained_head "
                "WHERE transition_id = ?",
                (transition["transition_id"],),
            )
            connection.execute(
                "DELETE FROM workspace_root_contract_transition "
                "WHERE transition_id = ?",
                (transition["transition_id"],),
            )
            connection.execute(
                "DELETE FROM migration_execution WHERE execution_id = ?",
                (transition["migration_execution_id"],),
            )
            connection.commit()

        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "native root retrieval lacks its authentic root6 genesis",
        ):
            WorkspaceStore.open(
                fixture.paths,
                expected_project_id=fixture.project_id,
            )

    def test_schema9_capture_coverage_seeds_once_and_advances_under_root6(self):
        fixture = self.fixture
        metadata = fixture.store.read_metadata()
        source_lease = fixture.store.claim_writer(
            owner="test.schema10.coverage-source",
            creation_basis="seed exact schema9 Capture coverage owners",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=fixture.authority_digest,
        )
        try:
            capture = prepare_raw_capture(
                fixture.store,
                mission_id=fixture.mission_id,
                executive_epoch_id=fixture.epoch_id,
                capture_kind="output",
                observation_id="schema9.coverage-migration",
                assignment_id="schema9.coverage-migration",
                provenance={"kind": "schema9_migration_fixture"},
                completion={"lifecycle": "completed"},
                artifacts=(
                    RawCaptureArtifactInput(
                        role="first",
                        logical_name="first.txt",
                        content_bytes=b"first schema9 migration artifact",
                    ),
                    RawCaptureArtifactInput(
                        role="second",
                        logical_name="second.txt",
                        content_bytes=b"second schema9 migration artifact",
                    ),
                ),
            )
            commit_raw_capture(
                fixture.store,
                cas=fixture.cas,
                record=capture,
                lease=source_lease,
                actor="test.schema10.coverage-source",
            )
            first_scope = CaptureScope(
                capture.capture_id,
                0,
                {"coverage": "complete artifact"},
            )
            source_evidence = prepare_evidence_meaning(
                authority=fixture.epoch_authority,
                evidence_id="evidence.schema9.coverage-migration",
                statement="The first exact schema9 artifact was reviewed.",
                exact_scope="the first exact schema9 Capture artifact",
                strength="exact_finite_identity",
                semantic_role="result",
                authority_basis="direct review of exact captured material",
                sources=(
                    first_scope,
                    first_scope,
                    CaptureScope(
                        capture.capture_id,
                        1,
                        {"coverage": "partial artifact"},
                    ),
                ),
                non_inferences=("does not establish a mathematical theorem",),
            )
            commit_evidence_meaning(
                fixture.store,
                record=source_evidence,
                lease=source_lease,
                actor="test.schema10.coverage-source",
            )
            source_evidence_read = read_evidence_meaning(
                fixture.store,
                evidence_id=source_evidence.evidence_id,
            )
            persisted_target = prepare_evidence_meaning(
                authority=fixture.epoch_authority,
                evidence_id=source_evidence.evidence_id,
                statement="The second exact schema9 artifact was reviewed.",
                exact_scope="the second exact schema9 Capture artifact",
                strength="exact_finite_identity",
                semantic_role="result",
                authority_basis="direct review of exact captured material",
                sources=(
                    CaptureScope(
                        capture.capture_id,
                        1,
                        {"coverage": "complete artifact"},
                    ),
                ),
                non_inferences=("does not establish a mathematical theorem",),
                expected_head_revision=source_evidence_read.revision,
                expected_head_payload_digest=source_evidence_read.payload_digest,
            )
            persisted_payload = persisted_target.evidence.to_payload()
            persisted_digest = canonical_payload(persisted_payload).sha256
            persisted_source = persisted_target.sources[0].to_payload(0)
            persisted_record = dict(persisted_payload)
            persisted_record.pop("revision")
            persisted_semantic_payload = {
                "record": persisted_record,
                "sources": [persisted_source],
                "interpreted_inputs": [],
            }
            persisted_auxiliary = (
                store_module.AuxiliaryWrite(
                    store_module.AuxiliaryTable.EVIDENCE_ITEM_REVISION,
                    {
                        "evidence_id": persisted_target.evidence_id,
                        "revision": persisted_target.revision,
                        "subtype": persisted_target.evidence.subtype,
                        "subject_json": persisted_payload["subject"],
                        "exact_scope_json": {
                            "scope": persisted_target.evidence.exact_scope
                        },
                        "rigor": persisted_target.evidence.rigor,
                        "limitations_json": list(
                            persisted_target.evidence.limitations
                        ),
                        "non_inferences_json": list(
                            persisted_target.evidence.non_inferences
                        ),
                        "security_json": {
                            "classification": (
                                persisted_target.evidence.security_classification
                            )
                        },
                        "retention_json": {
                            "policy": persisted_target.evidence.retention
                        },
                        "availability_state": (
                            persisted_target.evidence.availability_state
                        ),
                        "payload_digest": persisted_digest,
                    },
                ),
                store_module.AuxiliaryWrite(
                    store_module.AuxiliaryTable.EVIDENCE_CAPTURE_SOURCE,
                    {
                        "evidence_id": persisted_target.evidence_id,
                        "evidence_revision": persisted_target.revision,
                        "source_ordinal": 0,
                        "capture_id": persisted_source["capture_id"],
                        "artifact_ordinal": persisted_source["artifact_ordinal"],
                        "exact_scope_json": persisted_source["exact_scope"],
                    },
                ),
            )
            original_family_scope = store_module._validate_family_scope

            def allow_legacy_persisted_revision(**scope):
                if (
                    scope["family"] is store_module.RevisionCommandFamily.EVIDENCE
                    and scope["command_kind"]
                    == "commit_evidence_meaning_revision"
                    and not scope["evidence_head_advances"]
                ):
                    self.assertFalse(scope["writes"])
                    self.assertEqual(
                        {item.table for item in scope["auxiliary_writes"]},
                        {
                            store_module.AuxiliaryTable.EVIDENCE_ITEM_REVISION,
                            store_module.AuxiliaryTable.EVIDENCE_CAPTURE_SOURCE,
                        },
                    )
                    return
                original_family_scope(**scope)

            # Schema9 retained compatibility includes exact journaled revisions
            # created before a later contiguous head advance.  Patch only the
            # current ingress guard to reproduce that older valid stored state.
            with patch.object(
                store_module,
                "_validate_family_scope",
                side_effect=allow_legacy_persisted_revision,
            ):
                persisted_revision_outcome = (
                    fixture.store._apply_successor_storage_command(
                        family=store_module.RevisionCommandFamily.EVIDENCE,
                        lease=source_lease,
                        command_id="schema9.coverage.persist-target-revision",
                        actor="test.schema10.coverage-source",
                        command_kind="commit_evidence_meaning_revision",
                        executive_epoch_id=fixture.epoch_id,
                        request={
                            "evidence_id": persisted_target.evidence_id,
                            "historical_fixture": "persist_target_without_head",
                        },
                        semantic_payload=persisted_semantic_payload,
                        auxiliary_writes=persisted_auxiliary,
                        evidence_head_advances=(),
                        target_head_key=(
                            f"evidence:{source_evidence.evidence_id}"
                        ),
                        expected_target_revision=(
                            source_evidence_read.revision
                        ),
                        expected_target_payload_digest=(
                            source_evidence_read.payload_digest
                        ),
                        expected_canonical_authority_digest=(
                            fixture.authority_digest
                        ),
                    )
                )
            persisted_revision_commit = int(
                persisted_revision_outcome.project_commit
            )
            annotation = prepare_capture_scope_annotation(
                authority=fixture.epoch_authority,
                annotation_id="annotation.schema9.coverage-migration",
                capture_id=capture.capture_id,
                exact_scope={
                    "artifact_ordinal": 0,
                    "coverage": "complete artifact",
                },
                lifecycle="active",
            )
            commit_capture_scope_annotation(
                fixture.store,
                record=annotation,
                lease=source_lease,
                actor="test.schema10.coverage-source",
            )
        finally:
            current = fixture.store.read_metadata()
            if current["current_writer_epoch"] is not None:
                fixture.store.release_writer(
                    source_lease,
                    expected_project_commit=int(current["current_project_commit"]),
                    expected_root_digest=str(current["current_root_digest"]),
                    expected_canonical_authority_digest=fixture.authority_digest,
                )

        source = fixture.create_backup("schema9-coverage-source")
        source_database = source.path / "workspace.sqlite3"
        source_sha256 = hashlib.sha256(source_database.read_bytes()).hexdigest()
        source_rows = self._rows(source_database)
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id="schema10-coverage-migration",
            actor="test.schema10",
        )
        self.assertEqual(
            hashlib.sha256(source_database.read_bytes()).hexdigest(),
            source_sha256,
        )
        self.assertEqual(self._rows(source_database), source_rows)
        migrated_rows = self._rows(result.database)
        for table in (
            "raw_capture",
            "raw_capture_artifact",
            "evidence_item_revision",
            "evidence_capture_source",
            "evidence_item_head",
            "capture_scope_annotation_revision",
            "capture_scope_annotation_head",
        ):
            self.assertEqual(migrated_rows[table], source_rows[table], table)

        def coverage_state(database):
            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                metadata_row = connection.execute(
                    "SELECT current_project_commit FROM workspace_metadata "
                    "WHERE singleton = 1"
                ).fetchone()
                assert metadata_row is not None
                current_commit = int(metadata_row["current_project_commit"])
                journal = connection.execute(
                    "SELECT capture_coverage_root_digest, "
                    "capture_coverage_entry_count FROM transition_journal "
                    "WHERE project_commit_no = ?",
                    (current_commit,),
                ).fetchone()
                assert journal is not None
                commitment = store_module._CaptureCoverageCommitment(
                    str(journal["capture_coverage_root_digest"]),
                    int(journal["capture_coverage_entry_count"]),
                )
                tree = store_module._CaptureCoverageMutation(
                    connection,
                    project_id=fixture.project_id,
                    commitment=commitment,
                )
                counts = tuple(
                    tree.lookup(
                        mission_id=fixture.mission_id,
                        capture_id=capture.capture_id,
                        artifact_ordinal=ordinal,
                    )
                    for ordinal in (0, 1)
                )
                artifact_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM raw_capture_artifact"
                    ).fetchone()[0]
                )
                pre_root6_with_projection = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transition_journal "
                        "WHERE project_commit_no < ? AND "
                        "(capture_coverage_root_digest IS NOT NULL OR "
                        "capture_coverage_entry_count IS NOT NULL)",
                        (current_commit,),
                    ).fetchone()[0]
                )
                return commitment, counts, artifact_count, pre_root6_with_projection

        migrated_commitment, migrated_counts, artifact_count, pre_root6 = (
            coverage_state(result.database)
        )
        self.assertEqual(migrated_counts, (2, 0))
        self.assertEqual(migrated_commitment.entry_count, artifact_count)
        self.assertEqual(pre_root6, 0)

        source_uri = f"{result.database.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, isolation_level=None)
        ) as source_connection, closing(
            _connect_workspace(fixture.paths.database)
        ) as target_connection:
            source_connection.backup(target_connection)
        migrated_store = WorkspaceStore.open(
            fixture.paths,
            expected_project_id=fixture.project_id,
        )
        fixture.store = migrated_store
        metadata = migrated_store.read_metadata()
        intervening_migration_commit = int(metadata["current_project_commit"])
        headless_target_read = read_evidence_meaning(
            migrated_store,
            evidence_id=persisted_target.evidence_id,
            revision=persisted_target.revision,
        )
        self.assertEqual(headless_target_read.payload_digest, persisted_digest)
        successor_lease = migrated_store.claim_writer(
            owner="test.schema10.coverage-successor",
            creation_basis="advance one inherited Evidence coverage owner",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=fixture.authority_digest,
        )
        try:
            head_advance_outcome = migrated_store._apply_successor_storage_command(
                family=store_module.RevisionCommandFamily.EVIDENCE,
                lease=successor_lease,
                command_id="schema10.coverage.first-inherited-advance",
                actor="test.schema10.coverage-successor",
                command_kind="commit_evidence_meaning_revision",
                executive_epoch_id=fixture.epoch_id,
                request={
                    "evidence_id": persisted_target.evidence_id,
                    "head_only": True,
                },
                semantic_payload=persisted_semantic_payload,
                auxiliary_writes=(),
                evidence_head_advances=(
                    store_module.EvidenceHeadAdvance(
                        evidence_id=persisted_target.evidence_id,
                        target_revision=persisted_target.revision,
                        target_payload_digest=persisted_digest,
                        expected_revision=source_evidence_read.revision,
                        expected_payload_digest=(
                            source_evidence_read.payload_digest
                        ),
                    ),
                ),
                target_head_key=f"evidence:{persisted_target.evidence_id}",
                expected_target_revision=source_evidence_read.revision,
                expected_target_payload_digest=(
                    source_evidence_read.payload_digest
                ),
                expected_canonical_authority_digest=fixture.authority_digest,
            )
            head_advance_commit = int(head_advance_outcome.project_commit)
        finally:
            current = migrated_store.read_metadata()
            if current["current_writer_epoch"] is not None:
                migrated_store.release_writer(
                    successor_lease,
                    expected_project_commit=int(current["current_project_commit"]),
                    expected_root_digest=str(current["current_root_digest"]),
                    expected_canonical_authority_digest=fixture.authority_digest,
                )
        successor_commitment, successor_counts, successor_artifacts, _ = (
            coverage_state(fixture.paths.database)
        )
        self.assertEqual(successor_counts, (1, 1))
        self.assertEqual(successor_commitment.entry_count, successor_artifacts)
        self.assertNotEqual(
            successor_commitment.root_digest,
            migrated_commitment.root_digest,
        )
        successor_rows = self._rows(fixture.paths.database)
        for table in (
            "raw_capture",
            "raw_capture_artifact",
            "evidence_item_revision",
            "evidence_capture_source",
            "capture_scope_annotation_revision",
        ):
            self.assertEqual(successor_rows[table], migrated_rows[table], table)
        migrated_store.verify_integrity()
        with migrated_store._connection(read_only=True) as connection:
            connection.execute("BEGIN")
            journal_index = store_module._validated_journal_index(
                connection,
                project_id=fixture.project_id,
            )
            complete_facts = store_module._direct_recovery_facts_from_connection(
                connection,
                project_id=fixture.project_id,
                cut_project_commit=None,
                journal_index=journal_index,
            )
            cut_facts = store_module._direct_recovery_facts_from_connection(
                connection,
                project_id=fixture.project_id,
                cut_project_commit=intervening_migration_commit,
                journal_index=journal_index,
            )

            complete_head = next(
                item
                for item in complete_facts["head_history"]
                if item["kind"] == "evidence"
                and item["identity"] == persisted_target.evidence_id
            )
            retained_target = next(
                item
                for item in complete_facts["retained_history"]
                if item["source_family"] == "evidence"
                and item["identity"] == persisted_target.evidence_id
                and int(item["revision"]) == persisted_target.revision
            )
            cut_head = next(
                item
                for item in cut_facts["head_history"]
                if item["kind"] == "evidence"
                and item["identity"] == persisted_target.evidence_id
            )
            retained_target_at_cut = next(
                item
                for item in cut_facts["retained_history"]
                if item["source_family"] == "evidence"
                and item["identity"] == persisted_target.evidence_id
                and int(item["revision"]) == persisted_target.revision
            )
            selected_target = next(
                item
                for item in complete_head["revisions"]
                if int(item["revision"]) == persisted_target.revision
            )
            self.assertEqual(
                int(retained_target["project_commit"]),
                persisted_revision_commit,
            )
            self.assertEqual(
                int(retained_target_at_cut["project_commit"]),
                persisted_revision_commit,
            )
            self.assertEqual(
                int(selected_target["project_commit"]),
                head_advance_commit,
            )
            self.assertEqual(
                int(cut_head["at_cut_revision"]),
                source_evidence_read.revision,
            )
            self.assertLess(
                persisted_revision_commit,
                intervening_migration_commit,
            )
            self.assertLess(intervening_migration_commit, head_advance_commit)

        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            migrated_pointers = tuple(
                connection.execute(
                    "SELECT revision, project_commit_no "
                    "FROM evidence_item_revision WHERE evidence_id = ? "
                    "ORDER BY revision",
                    (source_evidence.evidence_id,),
                )
            )
            self.assertEqual(migrated_pointers, ((1, None), (2, None)))
            original_pointer = migrated_pointers[0][1]
            self.assertIsNone(original_pointer)
            connection.execute(
                "UPDATE evidence_item_revision SET project_commit_no = 1 "
                "WHERE evidence_id = ? AND revision = 1",
                (source_evidence.evidence_id,),
            )
            connection.commit()
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "exact-origin pointer is invalid",
        ):
            migrated_store.verify_integrity()
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            connection.execute(
                "UPDATE evidence_item_revision SET project_commit_no = NULL "
                "WHERE evidence_id = ? AND revision = 1",
                (source_evidence.evidence_id,),
            )
            connection.commit()
        migrated_store.verify_integrity()

    def test_migrated_root5_formal_session_audit_reuses_one_journal_index(self):
        fixture = self.fixture
        session_id, source_commits = self._seed_terminal_formal_session()
        source = fixture.create_backup("schema9-formal-session-audit-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id="schema10-formal-session-audit",
            actor="test.schema10",
        )
        source_uri = f"{result.database.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, isolation_level=None)
        ) as source_connection, closing(
            _connect_workspace(fixture.paths.database)
        ) as target_connection:
            source_connection.backup(target_connection)
        migrated = WorkspaceStore.open(
            fixture.paths,
            expected_project_id=fixture.project_id,
        )

        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            inherited = tuple(
                connection.execute(
                    "SELECT project_commit_no, changed_heads_json, "
                    "changed_head_rows_json FROM transition_journal "
                    "WHERE project_commit_no IN (?, ?) ORDER BY project_commit_no",
                    source_commits,
                )
            )
            self.assertEqual(
                tuple(int(row[0]) for row in inherited),
                source_commits,
            )
            self.assertEqual(
                tuple(json.loads(str(row[1])) for row in inherited),
                (
                    [f"session:{session_id}@1"],
                    [f"session:{session_id}@2"],
                ),
            )
            self.assertEqual(
                tuple(row[2] for row in inherited),
                (None, None),
            )
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM session_revision WHERE object_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                ),
                2,
            )

        with patch.object(
            store_module,
            "_validated_journal_index",
            wraps=store_module._validated_journal_index,
        ) as journal_index_build, patch.object(
            store_module,
            "_direct_recovery_facts_from_connection",
            wraps=store_module._direct_recovery_facts_from_connection,
        ) as recovery_scan, patch.object(
            store_module,
            "_validated_schema_v10_root_transition",
            wraps=store_module._validated_schema_v10_root_transition,
        ) as schema_transition_validation, patch.object(
            store_module,
            "_validate_root6_retained_auxiliary_rows",
            wraps=store_module._validate_root6_retained_auxiliary_rows,
        ) as retained_auxiliary_validation, patch.object(
            store_module,
            "_require_formal_session_capture_dependency",
            wraps=store_module._require_formal_session_capture_dependency,
        ) as session_dependency:
            report = migrated.verify_integrity()
        journal_index_build.assert_called_once()
        recovery_scan.assert_called_once()
        schema_transition_validation.assert_called_once()
        retained_auxiliary_validation.assert_called_once()
        self.assertEqual(session_dependency.call_count, 2)
        self.assertEqual(
            tuple(
                call.kwargs["owner_origin_commit"]
                for call in session_dependency.call_args_list
            ),
            source_commits,
        )
        self.assertEqual(
            tuple(
                call.kwargs["document"]["lifecycle"]
                for call in session_dependency.call_args_list
            ),
            ("open", "terminal"),
        )
        self.assertTrue(
            all(
                call.kwargs["session_id"] == session_id
                for call in session_dependency.call_args_list
            )
        )
        self.assertEqual(report.schema_version, 10)
        self.assertGreaterEqual(report.current_project_commit, source_commits[-1] + 1)

    def test_schema9_full_audit_reuses_history_index_for_capture_origins(self):
        fixture = self.fixture
        metadata = fixture.store.read_metadata()
        lease = fixture.store.claim_writer(
            owner="test.schema9.full-audit-index",
            creation_basis="seed one retained Capture for full-audit index reuse",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=fixture.authority_digest,
        )
        try:
            capture = prepare_raw_capture(
                fixture.store,
                mission_id=fixture.mission_id,
                executive_epoch_id=fixture.epoch_id,
                capture_kind="output",
                observation_id="schema9.full-audit-index",
                assignment_id="schema9.full-audit-index",
                provenance={"kind": "schema9_full_audit_regression"},
                artifacts=(
                    RawCaptureArtifactInput(
                        role="worker_output",
                        logical_name="schema9-full-audit.txt",
                        content_bytes=b"schema9 full-audit retained Capture",
                        media_type="text/plain",
                        encoding="utf-8",
                    ),
                ),
            )
            commit_raw_capture(
                fixture.store,
                cas=fixture.cas,
                record=capture,
                lease=lease,
                actor="test.schema9.full-audit-index",
                command_id="schema9.full-audit-index.capture",
            )
            mission = fixture.store.get_head(
                store_module.TypedWorkspaceId(
                    store_module.IdentityKind.MISSION,
                    fixture.mission_id,
                )
            )
            strategy = fixture.store.get_head(
                store_module.TypedWorkspaceId(
                    store_module.IdentityKind.STRATEGY,
                    "strategy.recovery",
                )
            )
            branch = fixture.store.get_head(
                store_module.TypedWorkspaceId(
                    store_module.IdentityKind.BRANCH,
                    "branch.recovery",
                )
            )
            assert mission is not None and strategy is not None and branch is not None
            mission_root = OwnerRevisionRef(
                "mission",
                fixture.mission_id,
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
                    mission_id=fixture.mission_id,
                    validated_document=mission.payload,
                    is_current_head=True,
                ),
                strategy_root.revision_identity: ResolvedOwnerRevision(
                    reference=strategy_root,
                    mission_id=fixture.mission_id,
                    validated_document=strategy.payload,
                    outgoing_refs=(branch_root,),
                    is_current_head=True,
                ),
                branch_root.revision_identity: ResolvedOwnerRevision(
                    reference=branch_root,
                    mission_id=fixture.mission_id,
                    validated_document=branch.payload,
                    is_current_head=True,
                ),
            }
            current = fixture.store.read_metadata()
            checkpoint = prepare_direct_continuation_checkpoint(
                mission_root=mission_root,
                strategy_root=strategy_root,
                resolver=lambda reference: resolved.get(
                    reference.revision_identity
                ),
                pending_capture_locators=(
                    {
                        "capture_id": capture.capture_id,
                        "artifact_ordinal": 0,
                    },
                ),
                predecessor_checkpoint=None,
                authoring_epoch_id=fixture.epoch_id,
                project_commit=int(current["current_project_commit"]) + 1,
                schema_version=1,
            )
            chain = fixture.store.read_executive_epoch_events(
                executive_epoch_id=fixture.epoch_id,
                mission_id=fixture.mission_id,
            )
            event = prepare_direct_executive_epoch_checkpointed_event(checkpoint)
            with fixture.store.direct_checkpoint_write_scope():
                fixture.store.commit_direct_continuation_checkpoint(
                    mission_id=fixture.mission_id,
                    executive_epoch_id=fixture.epoch_id,
                    checkpoint_document=checkpoint,
                    event=event,
                    expected_event_ordinal=int(chain[-1]["event_ordinal"]),
                    expected_event_digest=str(chain[-1]["event_digest"]),
                    lease=lease,
                    command_id="schema9.full-audit-index.checkpoint",
                    actor="test.schema9.full-audit-index",
                    expected_canonical_authority_digest=fixture.authority_digest,
                )
        finally:
            current = fixture.store.read_metadata()
            if current["current_writer_epoch"] is not None:
                fixture.store.release_writer(
                    lease,
                    expected_project_commit=int(current["current_project_commit"]),
                    expected_root_digest=str(current["current_root_digest"]),
                    expected_canonical_authority_digest=fixture.authority_digest,
                )

        traced: list[str] = []
        readbacks: list[Mapping[str, Mapping[str, object] | None]] = []
        cache_bindings: list[tuple[bool, bool, bool]] = []
        prior_scopes = (
            store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(),
            store_module._ACTIVE_EXPLICIT_HISTORY_READ_CACHE.get(),
            store_module._ACTIVE_COMMIT_ENVELOPE_CACHE.get(),
        )
        original_lifecycle_validation = (
            store_module._validate_successor_lifecycle_storage
        )

        def validate_and_probe(connection, *, cas=None):
            audit_index = store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get()
            history_scope = store_module._ACTIVE_EXPLICIT_HISTORY_READ_CACHE.get()
            commit_scope = store_module._ACTIVE_COMMIT_ENVELOPE_CACHE.get()
            cache_bindings.append(
                (
                    audit_index is not None
                    and audit_index.connection is connection,
                    history_scope is not None
                    and history_scope[0] is connection,
                    commit_scope is not None
                    and commit_scope[0] is connection,
                )
            )
            connection.set_trace_callback(traced.append)
            try:
                result = original_lifecycle_validation(connection, cas=cas)
                readbacks.append(
                    store_module._validated_raw_capture_reads_from_connection(
                        connection,
                        project_id=fixture.project_id,
                        capture_ids=(capture.capture_id,),
                    )
                )
            finally:
                connection.set_trace_callback(None)
            return result

        with patch.object(
            store_module,
            "_validated_journal_index",
            wraps=store_module._validated_journal_index,
        ) as journal_index_build, patch.object(
            store_module,
            "_validate_successor_lifecycle_storage",
            side_effect=validate_and_probe,
        ), patch.object(
            store_module,
            "_validated_raw_capture_reads_from_connection",
            wraps=store_module._validated_raw_capture_reads_from_connection,
        ) as capture_reads:
            report = fixture.store.verify_integrity()

        journal_index_build.assert_called_once()
        self.assertGreaterEqual(capture_reads.call_count, 2)
        self.assertEqual(cache_bindings, [(True, True, True)])
        self.assertEqual(len(readbacks), 1)
        self.assertTrue(
            all(readback[capture.capture_id] is not None for readback in readbacks)
        )
        normalized_queries = tuple(" ".join(query.lower().split()) for query in traced)
        self.assertFalse(
            any(
                "from transition_journal where project_commit_no" in query
                or "from command_result where project_commit_no" in query
                for query in normalized_queries
            ),
            normalized_queries,
        )
        self.assertEqual(report.schema_version, 9)
        self.assertEqual(
            (
                store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(),
                store_module._ACTIVE_EXPLICIT_HISTORY_READ_CACHE.get(),
                store_module._ACTIVE_COMMIT_ENVELOPE_CACHE.get(),
            ),
            prior_scopes,
        )
        with patch.object(
            store_module,
            "_validate_successor_lifecycle_storage",
            side_effect=WorkspaceIntegrityError("forced audit-cache unwind"),
        ), self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "forced audit-cache unwind",
        ):
            fixture.store.verify_integrity()
        self.assertEqual(
            (
                store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(),
                store_module._ACTIVE_EXPLICIT_HISTORY_READ_CACHE.get(),
                store_module._ACTIVE_COMMIT_ENVELOPE_CACHE.get(),
            ),
            prior_scopes,
        )

        migration_cache_bindings: list[tuple[bool, bool, bool]] = []
        migration_index_build_deltas: list[int] = []
        migration_queries: list[str] = []
        migration_readbacks: list[Mapping[str, Mapping[str, object] | None]] = []
        source = fixture.create_backup("schema9-migration-audit-index-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )

        with patch.object(
            store_module,
            "_validated_journal_index",
            wraps=store_module._validated_journal_index,
        ) as migration_index_build:

            def validate_migration_source_and_probe(connection, *, cas=None):
                if not (
                    connection.in_transaction
                    and int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
                    and int(connection.execute("PRAGMA user_version").fetchone()[0]) == 9
                ):
                    return original_lifecycle_validation(connection, cas=cas)
                audit_index = store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get()
                history_scope = store_module._ACTIVE_EXPLICIT_HISTORY_READ_CACHE.get()
                commit_scope = store_module._ACTIVE_COMMIT_ENVELOPE_CACHE.get()
                migration_cache_bindings.append(
                    (
                        audit_index is not None
                        and audit_index.connection is connection,
                        history_scope is not None
                        and history_scope[0] is connection,
                        commit_scope is not None
                        and commit_scope[0] is connection,
                    )
                )
                index_builds_before = migration_index_build.call_count
                connection.set_trace_callback(migration_queries.append)
                try:
                    result = original_lifecycle_validation(connection, cas=cas)
                    migration_readbacks.append(
                        store_module._validated_raw_capture_reads_from_connection(
                            connection,
                            project_id=fixture.project_id,
                            capture_ids=(capture.capture_id,),
                        )
                    )
                finally:
                    connection.set_trace_callback(None)
                migration_index_build_deltas.append(
                    migration_index_build.call_count - index_builds_before
                )
                return result

            with patch.object(
                store_module,
                "_validate_successor_lifecycle_storage",
                side_effect=validate_migration_source_and_probe,
            ), patch.object(
                store_module,
                "_validated_raw_capture_reads_from_connection",
                wraps=store_module._validated_raw_capture_reads_from_connection,
            ) as migration_capture_reads:
                migration = execute_offline_migration(
                    plan=plan,
                    backup=source,
                    migration_id="schema10-migration-audit-index",
                    actor="test.schema10",
                )

        self.assertEqual(migration.target_schema_version, 10)
        self.assertEqual(migration_cache_bindings, [(True, True, True)])
        self.assertEqual(migration_index_build_deltas, [0])
        self.assertGreaterEqual(migration_capture_reads.call_count, 2)
        self.assertEqual(len(migration_readbacks), 1)
        self.assertIsNotNone(migration_readbacks[0][capture.capture_id])
        normalized_migration_queries = tuple(
            " ".join(query.lower().split()) for query in migration_queries
        )
        self.assertFalse(
            any(
                "from transition_journal where project_commit_no" in query
                or "from command_result where project_commit_no" in query
                for query in normalized_migration_queries
            ),
            normalized_migration_queries,
        )
        self.assertEqual(
            (
                store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(),
                store_module._ACTIVE_EXPLICIT_HISTORY_READ_CACHE.get(),
                store_module._ACTIVE_COMMIT_ENVELOPE_CACHE.get(),
            ),
            prior_scopes,
        )

    def test_migrated_root5_strategy_origin_retains_exhaustive_source_authentication(self):
        fixture = self.fixture
        root5_revision = self._advance_strategy_revision(
            fixture.store,
            command_id="schema9.strategy.revision-2",
            marker="root5-revision-2",
        )
        assert root5_revision is not None
        self.assertEqual(root5_revision.reference.revision, 2)
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            root5_origin_commit = int(connection.execute(
                "SELECT project_commit_no FROM strategy_head "
                "WHERE object_id = 'strategy.recovery'"
            ).fetchone()[0])
        self.assertGreater(root5_origin_commit, 0)
        source = fixture.create_backup("schema9-historical-origin-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id="schema10-historical-origin",
            actor="test.schema10",
        )
        # Materialize the verified executor image back into this test's exact
        # disposable workspace root so physical-root and WAL bindings remain
        # realistic. The immutable migration artifact itself stays untouched.
        source_uri = f"{result.database.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, isolation_level=None)
        ) as source_connection, closing(
            _connect_workspace(fixture.paths.database)
        ) as target_connection:
            source_connection.backup(target_connection)
        migrated = WorkspaceStore.open(
            fixture.paths,
            expected_project_id=fixture.project_id,
        )
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            retained = connection.execute(
                "SELECT revision, source_project_commit "
                "FROM workspace_root_transition_retained_head "
                "WHERE kind = 'strategy' AND object_id = 'strategy.recovery' "
                "AND revision = 2"
            ).fetchone()
            superseded = connection.execute(
                "SELECT 1 FROM workspace_root_transition_retained_head "
                "WHERE kind = 'strategy' AND object_id = 'strategy.recovery' "
                "AND revision = 1"
            ).fetchone()
        self.assertEqual(retained, (2, root5_origin_commit))
        self.assertIsNone(superseded)

        with (
            patch.object(
                store_module,
                "_validated_journal_index",
                side_effect=AssertionError("current inherited head indexed history"),
            ),
            patch.object(
                store_module,
                "_direct_recovery_facts_from_connection",
                side_effect=AssertionError("current inherited head reconstructed history"),
            ),
        ):
            current = migrated.get_head(store_module.TypedWorkspaceId(
                store_module.IdentityKind.STRATEGY,
                "strategy.recovery",
            ))
        assert current is not None
        self.assertEqual(current.reference.revision, 2)
        self.assertEqual(current.payload_digest, root5_revision.payload_digest)

        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM strategy_revision "
                "WHERE object_id = 'strategy.recovery' AND revision = 2"
            ).fetchone()
            assert row is not None
            original = dict(row)
            forged_document = json.loads(str(row["payload_json"]))
            forged_document["integrated_comparison"] = "coordinated current-head tamper"
            forged_payload = canonical_payload(forged_document)
            forged_values = {
                column: row[column]
                for column in store_module._REVISION_DIGEST_COLUMNS
            }
            forged_values["payload_json"] = forged_payload.text
            forged_values["payload_digest"] = forged_payload.sha256
            forged_row_digest = store_module._row_digest(
                "strategy_revision",
                forged_values,
            )
            connection.execute(
                "UPDATE strategy_revision SET payload_json = ?, "
                "payload_digest = ?, row_digest = ? "
                "WHERE object_id = 'strategy.recovery' AND revision = 2",
                (forged_payload.text, forged_payload.sha256, forged_row_digest),
            )
            connection.execute(
                "UPDATE strategy_head SET payload_digest = ? "
                "WHERE object_id = 'strategy.recovery'",
                (forged_payload.sha256,),
            )
            connection.commit()
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "not an authenticated current frontier member",
        ):
            migrated.get_head(store_module.TypedWorkspaceId(
                store_module.IdentityKind.STRATEGY,
                "strategy.recovery",
            ))
        with self.assertRaises(WorkspaceIntegrityError):
            migrated.verify_integrity()
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            connection.execute(
                "UPDATE strategy_revision SET payload_json = ?, "
                "payload_digest = ?, row_digest = ? "
                "WHERE object_id = 'strategy.recovery' AND revision = 2",
                (
                    original["payload_json"],
                    original["payload_digest"],
                    original["row_digest"],
                ),
            )
            connection.execute(
                "UPDATE strategy_head SET payload_digest = ? "
                "WHERE object_id = 'strategy.recovery'",
                (original["payload_digest"],),
            )
            connection.commit()
        self.assertEqual(
            migrated.get_head(store_module.TypedWorkspaceId(
                store_module.IdentityKind.STRATEGY,
                "strategy.recovery",
            )).reference.revision,
            2,
        )

        root6_revision = self._advance_strategy_revision(
            migrated,
            command_id="schema10.strategy.revision-3",
            marker="root6-revision-3",
        )
        assert root6_revision is not None
        self.assertEqual(root6_revision.reference.revision, 3)
        with patch.object(
            store_module,
            "_validate_root6_retained_auxiliary_rows",
            wraps=store_module._validate_root6_retained_auxiliary_rows,
        ) as source_validation:
            historical = migrated.read_strategy_revision(
                mission_id=fixture.mission_id,
                strategy_id="strategy.recovery",
                revision=2,
            )
        self.assertEqual(historical["revision"], 2)
        source_validation.assert_called_once()
        migrated.verify_integrity()

        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            hook_node_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM root_hook_index_node"
                ).fetchone()[0]
            )
            root6_changed_rows = json.loads(
                str(
                    connection.execute(
                        "SELECT changed_head_rows_json FROM transition_journal "
                        "WHERE command_id = 'schema10.strategy.revision-3'"
                    ).fetchone()[0]
                )
            )
            marker = connection.execute(
                "SELECT 1 FROM current_dependency_node "
                "WHERE family = 'inherited_current_head' "
                "AND json_extract(scope_json, '$.kind') = 'strategy' "
                "AND json_extract(scope_json, '$.object_id') = 'strategy.recovery'"
            ).fetchone()
            root6_head = connection.execute(
                "SELECT revision, payload_digest, project_commit_no "
                "FROM strategy_head WHERE object_id = 'strategy.recovery'"
            ).fetchone()
            assert root6_head is not None
            connection.execute(
                "UPDATE strategy_head SET revision = 2, payload_digest = ?, "
                "project_commit_no = ? WHERE object_id = 'strategy.recovery'",
                (root5_revision.payload_digest, root5_origin_commit),
            )
            connection.commit()
        self.assertEqual(hook_node_count, 0)
        self.assertTrue(root6_changed_rows)
        self.assertTrue(
            all("root_hook_indexes" not in row for row in root6_changed_rows)
        )
        self.assertIsNone(marker)
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "not an authenticated current frontier member",
        ):
            migrated.get_head(store_module.TypedWorkspaceId(
                store_module.IdentityKind.STRATEGY,
                "strategy.recovery",
            ))
        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            connection.execute(
                "UPDATE strategy_head SET revision = ?, payload_digest = ?, "
                "project_commit_no = ? WHERE object_id = 'strategy.recovery'",
                root6_head,
            )
            connection.commit()

        with closing(sqlite3.connect(fixture.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM strategy_revision "
                "WHERE object_id = 'strategy.recovery' AND revision = 2"
            ).fetchone()
            assert row is not None
            document = json.loads(str(row["payload_json"]))
            document["integrated_comparison"] = "coordinated historical tamper"
            payload = canonical_payload(document)
            revision_values = {
                column: row[column]
                for column in store_module._REVISION_DIGEST_COLUMNS
            }
            revision_values["payload_json"] = payload.text
            revision_values["payload_digest"] = payload.sha256
            row_digest = store_module._row_digest(
                "strategy_revision",
                revision_values,
            )
            connection.execute(
                "UPDATE strategy_revision SET payload_json = ?, "
                "payload_digest = ?, row_digest = ? "
                "WHERE object_id = 'strategy.recovery' AND revision = 2",
                (payload.text, payload.sha256, row_digest),
            )
            connection.commit()
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "root5 source inventory commitment mismatch",
        ):
            migrated.read_strategy_revision(
                mission_id=fixture.mission_id,
                strategy_id="strategy.recovery",
                revision=2,
            )

    def test_schema9_v1_checkpoint_migrates_and_precedes_a_root6_v2_checkpoint(self):
        fixture = self.fixture
        source_lease, checkpoint_v1 = fixture.complete_checkpoint_and_keep_writer()
        self.assertEqual(checkpoint_v1.document["schema_version"], 1)
        fixture.release_writer(source_lease)
        source = fixture.create_backup("schema9-checkpoint-lineage-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id="schema10-checkpoint-lineage",
            actor="test.schema10",
        )
        source_uri = f"{result.database.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, isolation_level=None)
        ) as source_connection, closing(
            _connect_workspace(fixture.paths.database)
        ) as target_connection:
            source_connection.backup(target_connection)
        migrated = WorkspaceStore.open(
            fixture.paths,
            expected_project_id=fixture.project_id,
        )
        fixture.store = migrated

        retained_v1 = migrated.read_continuation_checkpoint(
            checkpoint_id=str(checkpoint_v1.document["checkpoint_id"]),
        )
        assert retained_v1 is not None
        self.assertEqual(retained_v1["document"]["schema_version"], 1)
        with migrated.direct_recovery_read_scope():
            v1_facts = migrated.read_direct_recovery_facts(
                cut_project_commit=int(retained_v1["project_commit_no"]),
            )
            v1_sections = migrated.materialize_continuation_checkpoint_sections(
                retained_v1,
                recovery_facts=v1_facts,
            )
        self.assertEqual(
            canonical_payload(v1_sections["transitive_owner_refs"]).text,
            canonical_payload(
                retained_v1["document"]["transitive_owner_refs"]
            ).text,
        )

        metadata = migrated.read_metadata()
        successor_lease = migrated.claim_writer(
            owner="test.schema10.checkpoint-lineage",
            creation_basis="write one v2 successor to a migrated v1 checkpoint",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=fixture.authority_digest,
        )
        try:
            successor_epoch = fixture.start_successor_epoch(
                successor_lease,
                checkpoint_v1,
            )
            mission = migrated.get_head(store_module.TypedWorkspaceId(
                store_module.IdentityKind.MISSION,
                fixture.mission_id,
            ))
            strategy = migrated.get_head(store_module.TypedWorkspaceId(
                store_module.IdentityKind.STRATEGY,
                "strategy.recovery",
            ))
            assert mission is not None and strategy is not None
            current = migrated.read_metadata()
            checkpoint_v2 = prepare_direct_continuation_checkpoint(
                mission_root=OwnerRevisionRef(
                    "mission",
                    fixture.mission_id,
                    mission.reference.revision,
                    mission.payload_digest,
                ),
                strategy_root=OwnerRevisionRef(
                    "strategy",
                    "strategy.recovery",
                    strategy.reference.revision,
                    strategy.payload_digest,
                ),
                unresolved_pointers=(),
                predecessor_checkpoint=checkpoint_v1.to_reference(),
                authoring_epoch_id=successor_epoch,
                project_commit=int(current["current_project_commit"]) + 1,
                schema_version=2,
            )
            events = migrated.read_executive_epoch_events(
                executive_epoch_id=successor_epoch,
                mission_id=fixture.mission_id,
            )
            event = prepare_direct_executive_epoch_checkpointed_event(
                checkpoint_v2
            )
            with migrated.direct_checkpoint_write_scope():
                migrated.commit_direct_continuation_checkpoint(
                    mission_id=fixture.mission_id,
                    executive_epoch_id=successor_epoch,
                    checkpoint_document=checkpoint_v2,
                    event=event,
                    expected_event_ordinal=int(events[-1]["event_ordinal"]),
                    expected_event_digest=str(events[-1]["event_digest"]),
                    lease=successor_lease,
                    command_id="schema10.checkpoint-lineage.v2",
                    actor="test.schema10",
                    expected_canonical_authority_digest=fixture.authority_digest,
                )
        finally:
            fixture.release_writer(successor_lease)

        retained_v2 = migrated.read_continuation_checkpoint(
            checkpoint_id=str(checkpoint_v2.document["checkpoint_id"]),
        )
        assert retained_v2 is not None
        self.assertEqual(retained_v2["document"]["schema_version"], 2)
        self.assertEqual(
            retained_v2["document"]["predecessor_checkpoint"],
            checkpoint_v1.to_reference(),
        )
        migrated.verify_integrity()
        reconstruction = MissionInterface(
            store=migrated,
            cas=fixture.cas,
            expected_mission_id=fixture.mission_id,
        ).reconstruct()
        self.assertEqual(
            reconstruction["recovery_opening"]["cut"]["document"]["schema_version"],
            2,
        )
        self.assertEqual(
            reconstruction["recovery_opening"]["cut"]["document"]
            ["predecessor_checkpoint"],
            checkpoint_v1.to_reference(),
        )

    def test_schema9_current_dependencies_migrate_to_authenticated_projection(self):
        fixture = self.fixture
        scope_ids, evidence, blob_sha256, disposition = (
            self._seed_legacy_current_dependencies(a2_scope_count=129)
        )
        source = fixture.create_backup("schema9-projection-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id="schema10-projection-migration",
            actor="test.schema10",
        )
        inspection_database = fixture.root / "schema10-projection-inspection.sqlite3"
        shutil.copyfile(result.database, inspection_database)
        with closing(_connect_workspace(inspection_database)) as connection:
            metadata = connection.execute(
                "SELECT * FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            assert metadata is not None
            current_commit = int(metadata["current_project_commit"])
            current_journal = _validated_commit_envelope(
                connection,
                project_id=fixture.project_id,
                commit_no=current_commit,
            )
            _verify_current_dependency_projection_full(
                connection,
                project_id=fixture.project_id,
                current_project_commit=current_commit,
                current_journal=current_journal,
            )
            projection = connection.execute(
                "SELECT root_scope_key, root_digest, entry_count "
                "FROM current_dependency_projection WHERE singleton = 1"
            ).fetchone()
            assert projection is not None
            inherited_current_heads = int(connection.execute(
                "SELECT COUNT(*) FROM current_dependency_node "
                "WHERE family = 'inherited_current_head'"
            ).fetchone()[0])
            retained_frontier = int(connection.execute(
                "SELECT COUNT(*) FROM workspace_root_transition_retained_head"
            ).fetchone()[0])
            self.assertEqual(inherited_current_heads, retained_frontier)
            self.assertEqual(
                int(projection["entry_count"]),
                len(scope_ids) + 6 + inherited_current_heads,
            )
            candidate_routes = store_module._current_candidate_a1_projection_routes(
                connection,
                project_id=fixture.project_id,
                mission_id=fixture.mission_id,
            )
            self.assertEqual(len(candidate_routes), 1)
            self.assertEqual(
                candidate_routes[0]["candidate_id"],
                "candidate.recovery-closure",
            )
            root_node = connection.execute(
                "SELECT height FROM current_dependency_node WHERE scope_key = ?",
                (projection["root_scope_key"],),
            ).fetchone()
            assert root_node is not None
            self.assertLessEqual(
                int(root_node["height"]),
                2 * int(projection["entry_count"]).bit_length(),
            )

            with patch.object(
                store_module,
                "_derive_current_dependency_entries",
                side_effect=AssertionError("routine lookup ran the full rederivation"),
            ):
                selected_holds = _current_a2_scope_holds(
                    connection,
                    project_id=fixture.project_id,
                    scope_id=scope_ids[-1],
                )
                absent = _current_dependency_lookup(
                    connection,
                    project_id=fixture.project_id,
                    family="a2_scope",
                    scope={"scope_id": "scope.absent"},
                )
                directives, tombstones = _closure_security_records(
                    connection,
                    project_id=fixture.project_id,
                    root_digest_version=6,
                    blob_sha256s=(blob_sha256,),
                    evidence_references=((evidence.evidence_id, evidence.revision),),
                    journal_index=None,
                    auxiliary_specs=store_module._ROOT6_AUXILIARY_SPECS,
                )
                continuity = _current_mission_continuity(
                    connection,
                    project_id=fixture.project_id,
                    mission_id=fixture.mission_id,
                )
            self.assertEqual(
                tuple(item["hold_id"] for item in selected_holds),
                ("hold.legacy.a2",),
            )
            self.assertIsNone(absent)
            self.assertEqual(
                tuple(item.directive_id for item in directives),
                (disposition.directive.directive_id,),
            )
            self.assertEqual(
                tuple(item.tombstone_id for item in tombstones),
                (disposition.tombstone.tombstone_id,),
            )
            self.assertIsNotNone(continuity)

            evidence_row = connection.execute(
                "SELECT * FROM evidence_item_revision "
                "WHERE evidence_id = ? AND revision = ?",
                (evidence.evidence_id, evidence.revision),
            ).fetchone()
            evidence_head = connection.execute(
                "SELECT * FROM evidence_item_head WHERE evidence_id = ?",
                (evidence.evidence_id,),
            ).fetchone()
            assert evidence_row is not None and evidence_head is not None
            owner_created_at = connection.execute(
                "SELECT created_at FROM transition_journal "
                "WHERE project_commit_no = ?",
                (int(evidence_head["project_commit_no"]),),
            ).fetchone()
            assert owner_created_at is not None
            self.assertNotEqual(
                str(evidence_row["created_at"]),
                str(owner_created_at["created_at"]),
            )
            with patch.object(
                store_module,
                "_validated_journal_index",
                side_effect=AssertionError(
                    "current migrated Evidence indexed complete history"
                ),
            ):
                self.assertEqual(
                    store_module._require_historical_evidence_revision_origin(
                        connection,
                        project_id=fixture.project_id,
                        row=evidence_row,
                    ),
                    int(evidence_head["project_commit_no"]),
                )

            node_selects = []
            connection.set_trace_callback(node_selects.append)
            _current_dependency_lookup(
                connection,
                project_id=fixture.project_id,
                family="a2_scope",
                scope={"scope_id": "scope.absent.again"},
            )
            connection.set_trace_callback(None)
            proof_reads = [
                statement
                for statement in node_selects
                if "FROM current_dependency_node" in statement
            ]
            self.assertTrue(proof_reads)
            self.assertTrue(
                all("WHERE scope_key =" in statement for statement in proof_reads),
                proof_reads,
            )

    def test_closed_security_directive_releases_shared_blob_but_not_tombstoned_evidence(self):
        fixture = self.fixture
        _scope_ids, evidence, blob_sha256, disposition = (
            self._seed_legacy_current_dependencies(
                directive_lifecycle="closed"
            )
        )
        source = fixture.create_backup("schema9-closed-security-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id="schema10-closed-security-migration",
            actor="test.schema10",
        )
        inspection_database = fixture.root / "schema10-closed-security-inspection.sqlite3"
        shutil.copyfile(result.database, inspection_database)
        with closing(_connect_workspace(inspection_database)) as connection:
            connection.execute("BEGIN")
            metadata = connection.execute(
                "SELECT current_project_commit FROM workspace_metadata "
                "WHERE singleton = 1"
            ).fetchone()
            assert metadata is not None
            current_commit = int(metadata["current_project_commit"])
            _verify_current_dependency_projection_full(
                connection,
                project_id=fixture.project_id,
                current_project_commit=current_commit,
                current_journal=_validated_commit_envelope(
                    connection,
                    project_id=fixture.project_id,
                    commit_no=current_commit,
                ),
            )
            self.assertEqual(
                _closure_security_records(
                    connection,
                    project_id=fixture.project_id,
                    root_digest_version=6,
                    blob_sha256s=(blob_sha256,),
                    evidence_references=(),
                    journal_index=None,
                    auxiliary_specs=store_module._ROOT6_AUXILIARY_SPECS,
                ),
                ((), ()),
            )
            directives, tombstones = _closure_security_records(
                connection,
                project_id=fixture.project_id,
                root_digest_version=6,
                blob_sha256s=(),
                evidence_references=((evidence.evidence_id, evidence.revision),),
                journal_index=None,
                auxiliary_specs=store_module._ROOT6_AUXILIARY_SPECS,
            )
            self.assertEqual(
                tuple(item.directive_id for item in directives),
                (disposition.directive.directive_id,),
            )
            self.assertEqual(directives[0].lifecycle, "closed")
            self.assertEqual(
                tuple(item.tombstone_id for item in tombstones),
                (disposition.tombstone.tombstone_id,),
            )
            store_module._require_ordinary_security_availability(
                connection,
                project_id=fixture.project_id,
                root_digest_version=6,
                blob_sha256s=(blob_sha256,),
            )
            with self.assertRaises(store_module._OrdinarySecurityDispositionError):
                store_module._require_ordinary_security_availability(
                    connection,
                    project_id=fixture.project_id,
                    root_digest_version=6,
                    evidence_references=(
                        (evidence.evidence_id, evidence.revision),
                    ),
                )
            self.assertIsNone(
                _current_dependency_lookup(
                    connection,
                    project_id=fixture.project_id,
                    family="security_blob",
                    scope={"blob_sha256": blob_sha256},
                )
            )

    def test_full_projection_audit_rejects_missing_extra_altered_and_wrong_state(self):
        fixture = self.fixture
        scope_ids, _evidence, _blob_sha256, _disposition = (
            self._seed_legacy_current_dependencies(a2_scope_count=3)
        )
        source = fixture.create_backup("schema9-projection-tamper-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id="schema10-projection-tamper",
            actor="test.schema10",
        )
        tamper_database = fixture.root / "projection-tamper.sqlite3"
        shutil.copyfile(result.database, tamper_database)
        with closing(_connect_workspace(tamper_database)) as connection:
            metadata = connection.execute(
                "SELECT * FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            assert metadata is not None
            current_commit = int(metadata["current_project_commit"])
            current_journal = _validated_commit_envelope(
                connection,
                project_id=fixture.project_id,
                commit_no=current_commit,
            )

            def verify():
                _verify_current_dependency_projection_full(
                    connection,
                    project_id=fixture.project_id,
                    current_project_commit=current_commit,
                    current_journal=current_journal,
                )

            rows = tuple(
                connection.execute(
                    "SELECT scope_key, family FROM current_dependency_node "
                    "ORDER BY scope_key"
                )
            )
            selected_key = next(
                str(row["scope_key"])
                for row in rows
                if str(row["family"]) == "a2_scope"
            )
            candidate_state_row = connection.execute(
                "SELECT state_json FROM current_dependency_node "
                "WHERE family = 'candidate_a1'"
            ).fetchone()
            assert candidate_state_row is not None
            candidate_state = json.loads(str(candidate_state_row["state_json"]))
            candidate_hold_id = candidate_state["hold_id"]
            scenarios = (
                (
                    "missing",
                    "DELETE FROM current_dependency_node WHERE scope_key = ?",
                    (selected_key,),
                ),
                (
                    "altered",
                    "UPDATE current_dependency_node SET state_json = '{}' "
                    "WHERE scope_key = ?",
                    (selected_key,),
                ),
                (
                    "wrongly-resolved",
                    "UPDATE hold SET lifecycle = 'resolved' WHERE hold_id = ?",
                    ("hold.legacy.a2",),
                ),
                (
                    "candidate-wrongly-resolved",
                    "UPDATE hold SET lifecycle = 'resolved' WHERE hold_id = ?",
                    (candidate_hold_id,),
                ),
            )
            for name, sql, parameters in scenarios:
                with self.subTest(corruption=name):
                    connection.execute("SAVEPOINT projection_tamper")
                    connection.execute(sql, parameters)
                    with self.assertRaises(WorkspaceIntegrityError):
                        verify()
                    connection.execute("ROLLBACK TO projection_tamper")
                    connection.execute("RELEASE projection_tamper")

            connection.execute("SAVEPOINT projection_extra")
            extra = store_module._new_current_dependency_node(
                fixture.project_id,
                "a2_scope",
                {"scope_id": "scope.extra"},
                {"holds": []},
            )
            connection.execute(
                "INSERT INTO current_dependency_node("
                "scope_key, family, scope_json, state_json, state_digest, "
                "left_scope_key, left_digest, left_height, right_scope_key, "
                "right_digest, right_height, height, subtree_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(
                    getattr(extra, column)
                    for column in (
                        "scope_key",
                        "family",
                        "scope_json",
                        "state_json",
                        "state_digest",
                        "left_scope_key",
                        "left_digest",
                        "left_height",
                        "right_scope_key",
                        "right_digest",
                        "right_height",
                        "height",
                        "subtree_digest",
                    )
                ),
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "missing or extra nodes",
            ):
                verify()
            connection.execute("ROLLBACK TO projection_extra")
            connection.execute("RELEASE projection_extra")

            # A duplicate current security disposition is invalid even when
            # its individual row shape and foreign keys are otherwise exact.
            connection.execute("SAVEPOINT projection_duplicate")
            connection.execute(
                "INSERT INTO evidence_tombstone "
                "SELECT 'tombstone.legacy.duplicate', evidence_id, "
                "evidence_revision, directive_id, reason_json, created_at "
                "FROM evidence_tombstone "
                "WHERE tombstone_id = 'tombstone.legacy.security'"
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "not one exact current projection",
            ):
                verify()
            connection.execute("ROLLBACK TO projection_duplicate")
            connection.execute("RELEASE projection_duplicate")
            self.assertEqual(len(scope_ids), 3)

    def test_schema10_transition_failure_rolls_back_ddl_and_history(self):
        fixture = self.fixture
        source = fixture.create_backup("schema9-rollback-source")
        plan = prepare_offline_migration(
            backup=source, target_schema_version=10, lifecycle=RecoveryLifecycle.QUIESCED,
        )
        before = self._rows(fixture.paths.database)
        before_schema_contract = self._schema_contract_rows(fixture.paths.database)
        source_database = source.path / "workspace.sqlite3"
        source_bytes = source_database.read_bytes()
        for fault_point in (
            "after_migration_statement",
            "after_schema_v10_journal",
            "before_migration_commit",
        ):
            with self.subTest(fault_point=fault_point):
                reached = []

                def fail(point, details):
                    reached.append(point)
                    if point == fault_point:
                        raise RuntimeError("injected schema10 migration fault")

                with self.assertRaises(MigrationExecutionError) as captured:
                    execute_offline_migration(
                        plan=plan, backup=source,
                        migration_id="rollback-" + fault_point.replace("_", "-"),
                        actor="test.schema10", fault_hook=fail,
                    )
                self.assertEqual(captured.exception.code, "migration_execution_failed")
                self.assertIn(fault_point, reached)
                artifact = captured.exception.artifact
                self.assertIsNotNone(artifact)
                outcome = json.loads((artifact / "attempt-0001.outcome.json").read_text())
                self.assertTrue(outcome["rollback_exact"])
                self.assertEqual(outcome["pre_logical_sha256"], outcome["post_logical_sha256"])
                self.assertEqual(outcome["schema_version"], 9)
                self.assertEqual(self._rows(artifact / "workspace.sqlite3"), before)
                self.assertEqual(
                    self._schema_contract_rows(artifact / "workspace.sqlite3"),
                    before_schema_contract,
                )
                self.assertEqual(source_database.read_bytes(), source_bytes)

    def test_migration_receipt_publication_is_atomic_and_no_replace(self):
        payload = {
            "format": "research-workspace-migration-attempt-v1",
            "attempt_id": "atomic-receipt.attempt.0001",
            "status": "passed",
        }
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "attempt-0001.outcome.json"
            pending = target.with_name(f".{target.name}.pending")
            observed = []
            real_link = os.link

            def observe_publication(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                self.assertEqual(source_path, pending)
                self.assertEqual(destination_path, target)
                self.assertFalse(destination_path.exists())
                self.assertEqual(
                    json.loads(source_path.read_text(encoding="utf-8")),
                    payload,
                )
                observed.append(destination_path)
                return real_link(source_path, destination_path)

            with patch.object(
                migration_executor_module.os,
                "link",
                side_effect=observe_publication,
            ):
                migration_executor_module._write_exclusive(target, payload)

            self.assertEqual(observed, [target])
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                payload,
            )
            self.assertFalse(pending.exists())
            with self.assertRaises(FileExistsError):
                migration_executor_module._write_exclusive(
                    target,
                    {**payload, "status": "failed"},
                )
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                payload,
            )

    def test_migration_temp_cleanup_requires_exact_artifact_identity(self):
        source = self.fixture.create_backup("temp-owner-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        migration_id = "temp-owner-binding"

        def stop_after_publication(point, _details):
            if point == "after_artifact_published":
                raise RuntimeError("stop after initial artifact publication")

        with self.assertRaisesRegex(RuntimeError, "initial artifact publication"):
            execute_offline_migration(
                plan=plan,
                backup=source,
                migration_id=migration_id,
                actor="test.schema10.owner",
                fault_hook=stop_after_publication,
            )
        artifact = self.fixture.paths.recovery / f"migration-{migration_id}"
        pending = artifact / ".attempt-0001.outcome.json.pending"
        pending.write_bytes(b'{"unowned":"must remain"}')

        other_source = self.fixture.create_backup("temp-other-source")
        other_plan = prepare_offline_migration(
            backup=other_source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        with self.assertRaises(MigrationExecutionError) as captured:
            execute_offline_migration(
                plan=other_plan,
                backup=other_source,
                migration_id=migration_id,
                actor="test.schema10.other-owner",
            )
        self.assertEqual(captured.exception.code, "migration_artifact_stale")
        self.assertEqual(pending.read_bytes(), b'{"unowned":"must remain"}')

    def test_attempt_10000_is_rejected_before_artifact_mutation(self):
        source = self.fixture.create_backup("attempt-limit-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        migration_id = "schema10-attempt-limit"

        def stop_after_publication(point, _details):
            if point == "after_artifact_published":
                raise RuntimeError("stop after initial artifact publication")

        with self.assertRaisesRegex(RuntimeError, "initial artifact publication"):
            execute_offline_migration(
                plan=plan,
                backup=source,
                migration_id=migration_id,
                actor="test.schema10.attempt-limit-setup",
                fault_hook=stop_after_publication,
            )
        artifact = self.fixture.paths.recovery / f"migration-{migration_id}"
        state_path = artifact / "migration_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(
            {
                "attempt_count": 9999,
                "active_attempt_id": None,
                "status": "retryable",
                "last_attempt_id": f"{migration_id}.attempt.9999",
                "pre_logical_sha256": state["source_logical_sha256"],
                "post_logical_sha256": state["source_logical_sha256"],
            }
        )
        state_path.write_bytes(
            migration_executor_module.canonical_json_bytes(state)
        )
        abandoned_temporary = artifact / ".attempt-9999.outcome.json.pending"
        abandoned_temporary.write_bytes(b'{"incomplete"')

        # Receipt shape and contiguity are covered at the parser boundary. Model
        # its validated output here rather than materialize 19,998 receipt files.
        started_receipts = {
            number: {"attempt_id": f"{migration_id}.attempt.{number:04d}"}
            for number in range(1, 10000)
        }
        closed_outcome = {
            "status": "failed",
            "rollback_exact": True,
            "pre_logical_sha256": state["source_logical_sha256"],
            "post_logical_sha256": state["source_logical_sha256"],
            "schema_version": plan.current_schema_version,
        }
        outcome_receipts = dict.fromkeys(range(1, 10000), closed_outcome)
        before_files = {
            path.name: path.read_bytes()
            for path in artifact.iterdir()
            if path.is_file()
        }

        with (
            patch.object(
                migration_executor_module,
                "_attempt_receipts",
                return_value=(started_receipts, outcome_receipts),
            ),
            patch.object(
                migration_executor_module,
                "_reconcile_atomic_write_temporaries",
                side_effect=AssertionError("attempt limit reconciled a temporary"),
            ) as reconcile_temporaries,
            patch.object(
                migration_executor_module,
                "_connect_workspace",
                side_effect=AssertionError("attempt limit opened a writable database"),
            ) as connect_writable,
            patch.object(
                migration_executor_module,
                "_write_exclusive",
                side_effect=AssertionError("attempt limit published a receipt"),
            ) as write_receipt,
            patch.object(
                migration_executor_module,
                "_replace_state",
                side_effect=AssertionError("attempt limit replaced migration state"),
            ) as replace_state,
        ):
            with self.assertRaises(MigrationExecutionError) as captured:
                execute_offline_migration(
                    plan=plan,
                    backup=source,
                    migration_id=migration_id,
                    actor="test.schema10.attempt-limit",
                )

        self.assertEqual(captured.exception.code, "migration_attempt_limit_reached")
        self.assertEqual(captured.exception.artifact, artifact)
        reconcile_temporaries.assert_not_called()
        connect_writable.assert_not_called()
        write_receipt.assert_not_called()
        replace_state.assert_not_called()
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in artifact.iterdir()
                if path.is_file()
            },
            before_files,
        )
        self.assertTrue(abandoned_temporary.exists())
        self.assertFalse(any("10000" in path.name for path in artifact.iterdir()))

    def test_attempt_9999_post_commit_replay_finalizes_the_same_attempt(self):
        source = self.fixture.create_backup("attempt-limit-post-commit-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        migration_id = "schema10-attempt-limit-post-commit"

        def stop_after_publication(point, _details):
            if point == "after_artifact_published":
                raise RuntimeError("stop after initial artifact publication")

        with self.assertRaisesRegex(RuntimeError, "initial artifact publication"):
            execute_offline_migration(
                plan=plan,
                backup=source,
                migration_id=migration_id,
                actor="test.schema10.attempt-limit-post-commit-setup",
                fault_hook=stop_after_publication,
            )
        artifact = self.fixture.paths.recovery / f"migration-{migration_id}"
        state_path = artifact / "migration_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(
            {
                "attempt_count": 9998,
                "active_attempt_id": None,
                "status": "retryable",
                "last_attempt_id": f"{migration_id}.attempt.9998",
                "pre_logical_sha256": state["source_logical_sha256"],
                "post_logical_sha256": state["source_logical_sha256"],
            }
        )
        state_path.write_bytes(
            migration_executor_module.canonical_json_bytes(state)
        )
        prior_started = {
            number: {"attempt_id": f"{migration_id}.attempt.{number:04d}"}
            for number in range(1, 9999)
        }
        closed_outcome = {
            "status": "failed",
            "rollback_exact": True,
            "pre_logical_sha256": state["source_logical_sha256"],
            "post_logical_sha256": state["source_logical_sha256"],
            "schema_version": plan.current_schema_version,
        }
        prior_outcomes = dict.fromkeys(range(1, 9999), closed_outcome)

        def fail_after_commit(point, _details):
            if point == "after_migration_commit_before_return":
                raise RuntimeError("injected attempt-9999 post-commit fault")

        with patch.object(
            migration_executor_module,
            "_attempt_receipts",
            return_value=(prior_started, prior_outcomes),
        ):
            with self.assertRaises(MigrationExecutionError) as captured:
                execute_offline_migration(
                    plan=plan,
                    backup=source,
                    migration_id=migration_id,
                    actor="test.schema10.attempt-9999",
                    fault_hook=fail_after_commit,
                )
        self.assertEqual(captured.exception.code, "migration_reconciliation_required")
        attempt_id = f"{migration_id}.attempt.9999"
        started_path = artifact / "attempt-9999.started.json"
        outcome_path = artifact / "attempt-9999.outcome.json"
        self.assertEqual(
            json.loads(started_path.read_text(encoding="utf-8"))["attempt_id"],
            attempt_id,
        )
        self.assertFalse(outcome_path.exists())
        abandoned_temporary = artifact / ".attempt-9999.outcome.json.pending"
        abandoned_temporary.write_bytes(b'{"incomplete"')

        def receipts_after_commit(**_kwargs):
            started = dict(prior_started)
            started[9999] = json.loads(started_path.read_text(encoding="utf-8"))
            outcomes = dict(prior_outcomes)
            if outcome_path.exists():
                outcomes[9999] = json.loads(outcome_path.read_text(encoding="utf-8"))
            return started, outcomes

        # The adjacent complete-tree replay test owns final result reissue. This
        # synthetic-history seam stops there after proving the exact outcome and
        # passed state are finalized without allocating or executing attempt 10000.
        with (
            patch.object(
                migration_executor_module,
                "_attempt_receipts",
                side_effect=receipts_after_commit,
            ),
            patch.object(
                migration_executor_module,
                "_apply_offline_migration_plan",
                side_effect=AssertionError("post-COMMIT replay reran migration SQL"),
            ) as apply_migration,
            patch.object(
                migration_executor_module,
                "_issue_result_from_inspection",
            ) as reissue_result,
            patch.object(
                migration_executor_module, "_verify_issued_offline_migration_result",
            ),
        ):
            reissue_result.return_value.attempt_id = attempt_id
            result = execute_offline_migration(
                plan=plan,
                backup=source,
                migration_id=migration_id,
                actor="test.schema10.attempt-9999-replay",
            )

        self.assertEqual(result.attempt_id, attempt_id)
        apply_migration.assert_not_called()
        self.assertEqual(
            [item.kwargs["require_read_only"] for item in reissue_result.call_args_list],
            [False],
        )
        self.assertFalse(abandoned_temporary.exists())
        self.assertEqual(
            json.loads(outcome_path.read_text(encoding="utf-8"))["status"],
            "passed",
        )
        passed_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(passed_state["status"], "passed")
        self.assertEqual(passed_state["attempt_count"], 9999)
        self.assertEqual(passed_state["successful_attempt_id"], attempt_id)
        self.assertFalse(any("10000" in path.name for path in artifact.iterdir()))
        database_uri = f"{(artifact / 'workspace.sqlite3').resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True)) as connection:
            executions = connection.execute(
                "SELECT attempt_id FROM migration_execution"
            ).fetchall()
        self.assertEqual(executions, [(attempt_id,)])

    def test_schema10_post_commit_fault_replays_one_exact_committed_attempt(self):
        fixture = self.fixture
        source = fixture.create_backup("schema9-post-commit-source")
        plan = prepare_offline_migration(
            backup=source,
            target_schema_version=10,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        source_database = source.path / "workspace.sqlite3"
        source_bytes = source_database.read_bytes()
        reached = []

        def fail(point, details):
            reached.append(point)
            if point == "after_migration_commit_before_return":
                raise RuntimeError("injected post-commit pre-return fault")

        migration_id = "schema10-post-commit-replay"
        with self.assertRaises(MigrationExecutionError) as captured:
            execute_offline_migration(
                plan=plan,
                backup=source,
                migration_id=migration_id,
                actor="test.schema10",
                fault_hook=fail,
            )
        self.assertEqual(
            captured.exception.code,
            "migration_reconciliation_required",
        )
        self.assertIn("after_migration_commit_before_return", reached)
        artifact = captured.exception.artifact
        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertFalse((artifact / "attempt-0001.outcome.json").exists())
        state = json.loads(
            (artifact / "migration_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "reconciliation_required")
        self.assertEqual(state["attempt_count"], 1)
        abandoned_temporaries = (
            artifact / ".attempt-0001.outcome.json.pending",
            artifact / ".migration_state.json.pending",
            artifact / ".migration_state.json.4242.tmp",
        )
        for temporary in abandoned_temporaries:
            temporary.write_bytes(b'{"incomplete"')
        with closing(sqlite3.connect(artifact / "workspace.sqlite3")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM migration_execution"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM workspace_root_contract_transition "
                    "WHERE target_schema_version = 10"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM transition_journal "
                    "WHERE command_kind = 'workspace_schema_root_transition'"
                ).fetchone()[0],
                1,
            )

        result = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id=migration_id,
            actor="test.schema10.replay",
        )
        self.assertTrue(all(not path.exists() for path in abandoned_temporaries))
        self.assertEqual(result.attempt_id, f"{migration_id}.attempt.0001")
        result_payload = result.to_payload()
        outcome = json.loads(
            (artifact / "attempt-0001.outcome.json").read_text(encoding="utf-8")
        )
        self.assertEqual(outcome["status"], "passed")
        self.assertEqual(
            json.loads(
                (artifact / "migration_state.json").read_text(encoding="utf-8")
            )["status"],
            "passed",
        )
        self.assertFalse((artifact / "attempt-0002.started.json").exists())

        replay = execute_offline_migration(
            plan=plan,
            backup=source,
            migration_id=migration_id,
            actor="test.schema10.second-replay",
        )
        self.assertEqual(replay.to_payload(), result_payload)
        self.assertFalse((artifact / "attempt-0002.started.json").exists())
        source_uri = f"{result.database.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, isolation_level=None)
        ) as source_connection, closing(
            _connect_workspace(fixture.paths.database)
        ) as target_connection:
            source_connection.backup(target_connection)
        fixture.store = WorkspaceStore.open(
            fixture.paths,
            expected_project_id=fixture.project_id,
        )
        fixture.store.verify_integrity()
        self.assertEqual(source_database.read_bytes(), source_bytes)


if __name__ == "__main__":
    unittest.main()
