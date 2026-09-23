from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core import migration_executor as migration_module  # noqa: E402
from research_core.candidate_revision import (  # noqa: E402
    candidate_a1_requirement,
    candidate_schema_digest_v4,
)
from research_core.evidence_store import EvidenceCAS  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.migration_executor import (  # noqa: E402
    MigrationExecutionError,
    _inspect_database,
    _validate_plan,
    execute_offline_migration,
)
from research_core.observability import (  # noqa: E402
    CaptureWorkspaceObserver,
    EventKind,
    ObservationEnvironment,
    preparation_progress_sink,
)
from research_core.workspace_recovery import (  # noqa: E402
    BackupSet,
    OfflineMigrationPlan,
    RecoveryError,
    RecoveryLifecycle,
    _load_persisted_backup_evidence,
    _protect_closed_tree,
    _remove_tree,
    create_bound_backup,
    prepare_offline_migration,
    verify_backup_set,
)
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_schema import (  # noqa: E402
    APPLICATION_VERSION,
    IdentityKind,
    LEGACY_MISSION_RUNTIME_APPLICATION_VERSION,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    TypedWorkspaceId,
    WorkspaceSchemaError,
    _MigrationCapability,
    _OfflineMigrationExecution,
    _apply_offline_migration_plan,
    _connect_workspace,
    _expected_schema_object_digest_for_version,
    _offline_migration_capability,
    expected_schema_object_digest,
    _iter_statements,
    load_migrations,
    migration_set_digest,
    schema_object_digest,
)
from research_core.workspace_store import (  # noqa: E402
    WorkspaceIntegrityError,
    WorkspaceStore,
    _validated_schema_v7_root_transition,
    _validated_schema_v8_root_refresh,
    _workspace_root_digest_for_connection,
    _verify_historical_schema8_snapshot,
)
from research_core_test_support import resolve_verified_test_source_commit  # noqa: E402


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _direct_genesis_fixture(
    *,
    project_id: str,
    mission_id: str,
    branch_id: str,
    strategy_id: str,
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


_CRASH_WORKER_ENV = "RESEARCH_CORE_MIGRATION_CRASH_WORKER"


def _run_crash_worker() -> None:
    configuration = json.loads(os.environ[_CRASH_WORKER_ENV])
    source_commit = str(configuration["source_commit"])
    with patch(
        "research_core.workspace_recovery._git_head",
        return_value=source_commit,
    ):
        backup = verify_backup_set(Path(configuration["backup_path"]))
        plan = prepare_offline_migration(
            backup=backup,
            target_schema_version=3,
            lifecycle=RecoveryLifecycle.OFFLINE,
        )

        def crash(point, details):
            if point == configuration["crash_point"]:
                os._exit(86)

        execute_offline_migration(
            plan=plan,
            backup=backup,
            migration_id=str(configuration["migration_id"]),
            actor="test.crash-worker",
            fault_hook=crash,
        )
    os._exit(87)


class SchemaV7MigrationTests(unittest.TestCase):
    """Exercise the pure DDL/legacy-preservation checkpoint before root v4."""

    @staticmethod
    def _apply_through(
        connection: sqlite3.Connection,
        target: int,
    ) -> None:
        for migration in load_migrations()[:target]:
            for statement in _iter_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migration("
                "version, name, digest_sha256, applied_at"
                ") VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.digest_sha256,
                    "2026-08-10T12:00:00Z",
                ),
            )

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        payload_json: str,
    ) -> None:
        connection.execute(
            "INSERT INTO session_revision("
            "object_id, revision, payload_json, payload_digest, "
            "predecessor_revision, created_actor, created_session_id, "
            "created_evidence_id, authorization_json, terminal_history_json, "
            "created_at, row_digest"
            ") VALUES (?, 1, ?, ?, NULL, 'test.schema-v7', NULL, NULL, "
            "'{}', '[]', ?, ?)",
            (
                session_id,
                payload_json,
                digest(payload_json.encode("utf-8")),
                "2026-08-10T12:00:00Z",
                digest((session_id + ".row").encode("utf-8")),
            ),
        )

    @classmethod
    def _v6_connection(
        cls,
        database: str | Path = ":memory:",
        *,
        root_identity: str = "1" * 64,
    ) -> sqlite3.Connection:
        connection = (
            sqlite3.connect(":memory:", isolation_level=None)
            if str(database) == ":memory:"
            else _connect_workspace(Path(database))
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = OFF")
        cls._apply_through(connection, 6)
        connection.execute(
            "INSERT INTO workspace_metadata("
            "singleton, project_id, application_version, schema_version, "
            "root_identity, lifecycle, canonical_authority_json, "
            "canonical_authority_digest, current_writer_epoch, "
            "current_project_commit, current_root_digest, "
            "transition_head_digest, created_at, updated_at, operating_mode, "
            "root_digest_version"
            ") VALUES (1, 'riemann_hypothesis', ?, 6, ?, 'quiesced', '{}', "
            "?, NULL, 0, ?, NULL, ?, ?, 'mission_runtime', 3)",
            (
                APPLICATION_VERSION,
                root_identity,
                "2" * 64,
                "3" * 64,
                "2026-08-10T12:00:00Z",
                "2026-08-10T12:00:00Z",
            ),
        )
        return connection

    @staticmethod
    def _seal_v6_genesis(connection: sqlite3.Connection) -> None:
        authority_digest = digest(b"{}")
        root_digest = _workspace_root_digest_for_connection(
            connection,
            project_id="riemann_hypothesis",
            canonical_authority_digest=authority_digest,
            root_digest_version=3,
            operating_mode="mission_runtime",
        )
        connection.execute(
            "UPDATE workspace_metadata SET canonical_authority_digest = ?, "
            "current_root_digest = ?, current_project_commit = 0, "
            "transition_head_digest = NULL WHERE singleton = 1",
            (authority_digest, root_digest),
        )
        connection.execute(
            "INSERT INTO project_commit("
            "commit_no, project_id, root_digest, canonical_authority_digest, "
            "transition_head_digest, command_id, created_by, created_at"
            ") VALUES (0, 'riemann_hypothesis', ?, ?, NULL, NULL, "
            "'test.schema-v7', '2026-08-10T12:00:00Z')",
            (root_digest, authority_digest),
        )
        connection.execute("PRAGMA user_version = 6")
        connection.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def _v7_transition_fixture(
        cls,
        database: Path,
        *,
        suffix: str,
        root_identity: str = "1" * 64,
        populated: bool = True,
    ) -> tuple[
        sqlite3.Connection,
        _OfflineMigrationExecution,
        _MigrationCapability,
        tuple[tuple[int, str, str], ...],
        tuple[tuple[str, object], ...],
    ]:
        connection = cls._v6_connection(database, root_identity=root_identity)
        if populated:
            result_backed = cls._insert_v6_prepared(
                connection,
                suffix=f"{suffix}-result",
                with_result_reference=True,
            )
            cls._insert_v6_result_dependents(
                connection,
                fixture=result_backed,
                suffix=f"{suffix}-result",
            )
            cls._insert_v6_prepared(
                connection,
                suffix=f"{suffix}-prepared",
                with_result_reference=False,
            )
        cls._seal_v6_genesis(connection)
        source_metadata_row = connection.execute(
            "SELECT * FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        source_metadata = tuple(
            (key, source_metadata_row[key]) for key in source_metadata_row.keys()
        )
        history_payload = [
            {
                "version": int(row["version"]),
                "name": str(row["name"]),
                "digest_sha256": str(row["digest_sha256"]),
                "applied_at": str(row["applied_at"]),
            }
            for row in connection.execute(
                "SELECT version, name, digest_sha256, applied_at "
                "FROM schema_migration ORDER BY version"
            )
        ]
        execution = _OfflineMigrationExecution(
            execution_id=f"migration.schema-v7-{suffix}",
            attempt_id=f"schema-v7-{suffix}.attempt.0001",
            source_schema_version=6,
            target_schema_version=7,
            verified_backup_manifest_sha256=digest(
                f"backup.schema-v7-{suffix}".encode("utf-8")
            ),
            applied_history_sha256=digest(canonical_json_bytes(history_payload)),
            plan_sha256=digest(f"plan.schema-v7-{suffix}".encode("utf-8")),
            started_at="2026-08-10T12:02:00Z",
            completed_at="2026-08-10T12:03:00Z",
        )
        migration = load_migrations()[6]
        capability = _offline_migration_capability(
            database,
            root_identity=root_identity,
        )
        return (
            connection,
            execution,
            capability,
            ((migration.version, migration.name, migration.digest_sha256),),
            source_metadata,
        )

    @classmethod
    def _insert_v6_prepared(
        cls,
        connection: sqlite3.Connection,
        *,
        suffix: str,
        with_result_reference: bool,
    ) -> dict[str, str]:
        session_id = f"session.{suffix}"
        intent_id = f"intent.{suffix}"
        attempt_id = f"attempt.{suffix}"
        payload_json = json.dumps(
            {"kind": "legacy_session", "suffix": suffix},
            sort_keys=True,
            separators=(",", ":"),
        )
        cls._insert_revision(
            connection,
            session_id=session_id,
            payload_json=payload_json,
        )
        intent_digest = digest((intent_id + ".envelope").encode("utf-8"))
        intent_json = json.dumps(
            {"intent_id": intent_id}, sort_keys=True, separators=(",", ":")
        )
        created_at = "2026-08-10T12:01:00Z"
        connection.execute(
            "INSERT INTO outbox_intent("
            "intent_id, session_id, session_revision, envelope_digest, "
            "envelope_json, lifecycle, created_at"
            ") VALUES (?, ?, 1, ?, ?, 'pending', ?)",
            (intent_id, session_id, intent_digest, intent_json, created_at),
        )
        connection.execute(
            "INSERT INTO session_attempt_binding("
            "binding_id, intent_id, intent_digest, project_id, mission_id, "
            "session_id, session_revision, attempt_id, attempt_chain_id, "
            "attempt_sequence, workstation_journal_schema_version, "
            "workstation_attempt_state, fence_id, fence_digest, "
            "coordination_id, coordination_digest, authorization_id, "
            "authorization_digest, prepared_record_digest, created_at"
            ") VALUES (?, ?, ?, 'riemann_hypothesis', 'mission.v6', ?, 1, ?, "
            "?, 1, 5, 'prepared', ?, ?, 'coordination.v6', ?, "
            "'authorization.v6', ?, ?, ?)",
            (
                f"binding.{suffix}",
                intent_id,
                intent_digest,
                session_id,
                attempt_id,
                f"chain.{suffix}",
                f"fence.{suffix}",
                digest((suffix + ".fence").encode("utf-8")),
                digest(b"coordination.v6"),
                digest(b"authorization.v6"),
                digest((suffix + ".record").encode("utf-8")),
                created_at,
            ),
        )
        if with_result_reference:
            connection.execute(
                "INSERT INTO attempt_reference("
                "attempt_id, session_id, session_revision, intent_digest, "
                "result_digest, observed_lifecycle, settlement_state, "
                "unknown_effect"
                ") VALUES (?, ?, 1, ?, ?, 'succeeded', 'settled', 0)",
                (
                    attempt_id,
                    session_id,
                    intent_digest,
                    digest((suffix + ".result").encode("utf-8")),
                ),
            )
        return {
            "session_id": session_id,
            "payload_json": payload_json,
            "intent_id": intent_id,
            "intent_json": intent_json,
            "intent_digest": intent_digest,
            "attempt_id": attempt_id,
            "result_digest": digest((suffix + ".result").encode("utf-8")),
        }

    @staticmethod
    def _insert_v6_result_dependents(
        connection: sqlite3.Connection,
        *,
        fixture: dict[str, str],
        suffix: str,
    ) -> None:
        receipt_id = f"receipt.{suffix}"
        evidence_id = f"evidence.{suffix}"
        closure_id = f"closure.{suffix}"
        evidence_binding_id = f"evidence-binding.{suffix}"
        created_at = "2026-08-10T12:01:30Z"
        envelope_json = json.dumps(
            {"attempt_id": fixture["attempt_id"], "result": suffix},
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO inbox_receipt("
            "receipt_id, attempt_id, envelope_digest, envelope_json, lifecycle, "
            "created_at) VALUES (?, ?, ?, ?, 'received', ?)",
            (
                receipt_id,
                fixture["attempt_id"],
                fixture["result_digest"],
                envelope_json,
                created_at,
            ),
        )
        evidence_payload_digest = digest((suffix + ".evidence").encode("utf-8"))
        connection.execute(
            "INSERT INTO evidence_item_revision("
            "evidence_id, revision, subtype, subject_json, exact_scope_json, "
            "rigor, limitations_json, non_inferences_json, security_json, "
            "retention_json, availability_state, canonical_effect, "
            "payload_digest, created_at) VALUES (?, 1, 'research_attempt_result', "
            "'{}', '{}', 'bounded', '[]', '[]', '{}', '{}', "
            "'verified_available', 'none', ?, ?)",
            (evidence_id, evidence_payload_digest, created_at),
        )
        closure_digest = digest((suffix + ".closure").encode("utf-8"))
        connection.execute(
            "INSERT INTO closure_manifest("
            "closure_id, root_kind, root_digest, members_json, contract_json, "
            "created_at) VALUES (?, 'evidence_item', ?, '[]', '{}', ?)",
            (closure_id, closure_digest, created_at),
        )
        connection.execute(
            "INSERT INTO attempt_evidence_binding("
            "binding_id, attempt_id, result_receipt_id, result_digest, "
            "evidence_id, evidence_revision, evidence_payload_digest, "
            "closure_id, closure_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                evidence_binding_id,
                fixture["attempt_id"],
                receipt_id,
                fixture["result_digest"],
                evidence_id,
                evidence_payload_digest,
                closure_id,
                closure_digest,
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO session_settlement("
            "settlement_id, session_id, session_revision, "
            "attempt_references_json, charge_basis_json, "
            "release_conversion_json, evidence_binding_id, command_id, "
            "created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (
                f"settlement.{suffix}",
                fixture["session_id"],
                json.dumps([fixture["attempt_id"]], separators=(",", ":")),
                '{"vcpu_hours":1}',
                '{"vcpu_hours":0}',
                evidence_binding_id,
                f"command.settlement.{suffix}",
                created_at,
            ),
        )

    @staticmethod
    def _apply_v7(connection: sqlite3.Connection) -> None:
        migration = load_migrations()[6]
        for statement in _iter_statements(migration.sql):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migration("
            "version, name, digest_sha256, applied_at"
            ") VALUES (?, ?, ?, ?)",
            (
                migration.version,
                migration.name,
                migration.digest_sha256,
                "2026-08-10T12:02:00Z",
            ),
        )

    @classmethod
    def _insert_v7_attempt(
        cls,
        connection: sqlite3.Connection,
        *,
        suffix: str,
    ) -> dict[str, str]:
        session_id = f"session.v7.{suffix}"
        intent_id = f"intent.v7.{suffix}"
        allocation_id = f"allocation.v7.{suffix}"
        attempt_id = f"attempt.v7.{suffix}"
        chain_id = f"chain.v7.{suffix}"
        binding_id = f"binding.v7.{suffix}"
        receipt_id = f"receipt.v7.{suffix}.1"
        disposition_id = f"disposition.v7.{suffix}.1"
        created_at = "2026-08-10T12:04:00Z"
        cls._insert_revision(
            connection,
            session_id=session_id,
            payload_json=json.dumps(
                {"kind": "current_session", "suffix": suffix},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        intent_digest = digest((suffix + ".intent").encode("utf-8"))
        connection.execute(
            "INSERT INTO outbox_intent("
            "intent_id, session_id, session_revision, envelope_digest, "
            "envelope_json, lifecycle, created_at) "
            "VALUES (?, ?, 1, ?, '{}', 'pending', ?)",
            (intent_id, session_id, intent_digest, created_at),
        )
        allocation_sha256 = digest((suffix + ".allocation").encode("utf-8"))
        digest_values = [
            digest((suffix + label).encode("utf-8"))
            for label in (
                ".plan",
                ".policy",
                ".authorization",
                ".reservation",
                ".resource-envelope",
                ".resource-enforcement",
                ".allocation-row",
            )
        ]
        connection.execute(
            "INSERT INTO session_attempt_allocation("
            "allocation_id, allocation_contract_version, intent_id, session_id, "
            "session_revision, planned_session_digest, attempt_policy_json, "
            "attempt_policy_sha256, authorization_id, authorization_sha256, "
            "reservation_vector_json, reservation_vector_sha256, "
            "resource_envelope_json, resource_envelope_sha256, "
            "resource_enforcement_json, resource_enforcement_sha256, "
            "allocation_sha256, created_at, row_digest) "
            "VALUES (?, 1, ?, ?, 1, ?, '{}', ?, ?, ?, '{}', ?, '{}', ?, "
            "'{}', ?, ?, ?, ?)",
            (
                allocation_id,
                intent_id,
                session_id,
                digest_values[0],
                digest_values[1],
                f"authorization.v7.{suffix}",
                digest_values[2],
                digest_values[3],
                digest_values[4],
                digest_values[5],
                allocation_sha256,
                created_at,
                digest_values[6],
            ),
        )
        fence_id = f"fence.v7.{suffix}"
        fence_sha256 = digest((suffix + ".fence").encode("utf-8"))
        intent_binding_digest = digest((suffix + ".intent-binding").encode("utf-8"))
        journal_record_sha256 = digest((suffix + ".journal").encode("utf-8"))
        staged_inventory_sha256 = digest((suffix + ".staged").encode("utf-8"))
        prepared_receipt_sha256 = digest((suffix + ".prepared").encode("utf-8"))
        connection.execute(
            "INSERT INTO attempt_reference("
            "attempt_id, reference_contract_version, session_id, session_revision, "
            "execution_intent_id, intent_sha256, allocation_id, allocation_sha256, "
            "attempt_chain_id, attempt_sequence, previous_attempt_id, fence_id, "
            "fence_sha256, intent_binding_digest, journal_record_sha256, "
            "staged_inventory_sha256, prepared_receipt_sha256, "
            "journal_schema_version, attempt_owner, result_digest, "
            "observed_lifecycle, settlement_state, unknown_effect, created_at) "
            "VALUES (?, 7, ?, 1, ?, ?, ?, ?, ?, 1, NULL, ?, ?, ?, ?, ?, ?, 5, "
            "'workstation_control', NULL, NULL, NULL, NULL, ?)",
            (
                attempt_id,
                session_id,
                intent_id,
                intent_digest,
                allocation_id,
                allocation_sha256,
                chain_id,
                fence_id,
                fence_sha256,
                intent_binding_digest,
                journal_record_sha256,
                staged_inventory_sha256,
                prepared_receipt_sha256,
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO session_attempt_binding("
            "binding_id, binding_contract_version, intent_id, intent_digest, "
            "allocation_id, project_id, mission_id, session_id, session_revision, "
            "attempt_id, attempt_chain_id, attempt_sequence, previous_attempt_id, "
            "retry_authorization_disposition_id, retry_authorization_action, "
            "workstation_journal_schema_version, workstation_attempt_state, "
            "fence_id, fence_digest, intent_binding_digest, journal_record_sha256, "
            "staged_inventory_sha256, prepared_receipt_sha256, coordination_id, "
            "coordination_digest, authorization_id, authorization_digest, created_at) "
            "VALUES (?, 7, ?, ?, ?, 'riemann_hypothesis', 'mission.v7', ?, 1, ?, ?, "
            "1, NULL, NULL, NULL, 5, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding_id,
                intent_id,
                intent_digest,
                allocation_id,
                session_id,
                attempt_id,
                chain_id,
                fence_id,
                fence_sha256,
                intent_binding_digest,
                journal_record_sha256,
                staged_inventory_sha256,
                prepared_receipt_sha256,
                f"coordination.v7.{suffix}",
                digest((suffix + ".coordination").encode("utf-8")),
                f"authorization.v7.{suffix}",
                digest((suffix + ".binding-authorization").encode("utf-8")),
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO prepared_attempt_receipt("
            "attempt_id, binding_id, receipt_contract_version, receipt_json, "
            "receipt_sha256, created_at, row_digest) VALUES (?, ?, 1, '{}', ?, ?, ?)",
            (
                attempt_id,
                binding_id,
                prepared_receipt_sha256,
                created_at,
                digest((suffix + ".prepared-row").encode("utf-8")),
            ),
        )
        result_digest = digest((suffix + ".result").encode("utf-8"))
        observation_sha256 = digest((suffix + ".observation").encode("utf-8"))
        connection.execute(
            "INSERT INTO inbox_receipt("
            "receipt_id, receipt_contract_version, attempt_id, execution_intent_id, "
            "attempt_chain_id, attempt_sequence, observation_sequence, "
            "predecessor_receipt_id, predecessor_receipt_sha256, envelope_digest, "
            "envelope_json, termination_observation_sha256, lifecycle, created_at) "
            "VALUES (?, 7, ?, ?, ?, 1, 1, NULL, NULL, ?, '{}', ?, 'received', ?)",
            (
                receipt_id,
                attempt_id,
                intent_id,
                chain_id,
                result_digest,
                observation_sha256,
                created_at,
            ),
        )
        disposition_sha256 = digest((suffix + ".disposition").encode("utf-8"))
        connection.execute(
            "INSERT INTO session_attempt_disposition_transition("
            "disposition_id, disposition_contract_version, session_id, "
            "session_revision, attempt_id, transition_sequence, "
            "predecessor_disposition_id, predecessor_disposition_sha256, "
            "result_receipt_id, result_digest, termination_observation_sha256, "
            "action, normalized_reason, disposition_sha256, created_at, row_digest) "
            "VALUES (?, 1, ?, 1, ?, 1, NULL, NULL, ?, ?, ?, "
            "'terminal_operational', 'provider_failed', ?, ?, ?)",
            (
                disposition_id,
                session_id,
                attempt_id,
                receipt_id,
                result_digest,
                observation_sha256,
                disposition_sha256,
                created_at,
                digest((suffix + ".disposition-row").encode("utf-8")),
            ),
        )
        return {
            "session_id": session_id,
            "intent_id": intent_id,
            "intent_digest": intent_digest,
            "allocation_id": allocation_id,
            "allocation_sha256": allocation_sha256,
            "attempt_id": attempt_id,
            "chain_id": chain_id,
            "binding_id": binding_id,
            "prepared_receipt_sha256": prepared_receipt_sha256,
            "receipt_id": receipt_id,
            "result_digest": result_digest,
            "observation_sha256": observation_sha256,
            "disposition_id": disposition_id,
            "disposition_sha256": disposition_sha256,
        }

    def test_fresh_schema_parses_to_exact_current_table_set(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertEqual(
            tuple(item.version for item in load_migrations()),
            (1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        )
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys = OFF")
        self._apply_through(connection, 10)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertTrue(REQUIRED_TABLES.issubset(tables))
        self.assertEqual(schema_object_digest(connection), expected_schema_object_digest())
        transition_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(workspace_root_contract_transition)"
            )
        }
        self.assertTrue(
            {
                "command_id",
                "writer_epoch",
                "canonical_authority_digest",
            }.issubset(transition_columns)
        )
        connection.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())

    def test_schema7_to8_refresh_preserves_legacy_attempt_rows_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "workspace.sqlite3"
            connection, execution7, capability7, pending7, _metadata = (
                self._v7_transition_fixture(database, suffix="schema8-retained")
            )
            self.addCleanup(connection.close)
            _apply_offline_migration_plan(
                connection,
                pending_migrations=pending7,
                execution=execution7,
                capability=capability7,
            )
            retained_tables = (
                "attempt_reference",
                "session_attempt_binding",
                "prepared_attempt_receipt",
            )
            before = {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY 1"
                    )
                )
                for table in retained_tables
            }
            history_payload = [
                {
                    "version": int(row["version"]),
                    "name": str(row["name"]),
                    "digest_sha256": str(row["digest_sha256"]),
                    "applied_at": str(row["applied_at"]),
                }
                for row in connection.execute(
                    "SELECT * FROM schema_migration ORDER BY version"
                )
            ]
            execution8 = _OfflineMigrationExecution(
                execution_id="migration.schema-v8-retained",
                attempt_id="schema-v8-retained.attempt.0001",
                source_schema_version=7,
                target_schema_version=8,
                verified_backup_manifest_sha256="a" * 64,
                applied_history_sha256=digest(
                    canonical_json_bytes(history_payload)
                ),
                plan_sha256="b" * 64,
                started_at="2026-08-13T00:00:00Z",
                completed_at="2026-08-13T00:01:00Z",
            )
            migration8 = load_migrations()[7]
            _apply_offline_migration_plan(
                connection,
                pending_migrations=((
                    migration8.version,
                    migration8.name,
                    migration8.digest_sha256,
                ),),
                execution=execution8,
                capability=_offline_migration_capability(
                    database,
                    root_identity=capability7.root_identity,
                ),
            )
            after = {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY 1"
                    )
                )
                for table in retained_tables
            }
            self.assertEqual(after, before)
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                8,
            )
            metadata = connection.execute(
                "SELECT schema_version, root_digest_version, application_version, "
                "current_project_commit FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            self.assertEqual(
                tuple(metadata),
                (8, 4, LEGACY_MISSION_RUNTIME_APPLICATION_VERSION, 2),
            )
            self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())
            connection.execute("PRAGMA journal_mode = DELETE")
            self.assertEqual(
                _verify_historical_schema8_snapshot(connection).schema_version,
                8,
            )
            connection.close()

    def test_v6_rows_migrate_byte_exact_without_v7_authority_synthesis(self) -> None:
        connection = self._v6_connection()
        self.addCleanup(connection.close)
        with_result = self._insert_v6_prepared(
            connection,
            suffix="result",
            with_result_reference=True,
        )
        self._insert_v6_result_dependents(
            connection,
            fixture=with_result,
            suffix="result",
        )
        prepared_only = self._insert_v6_prepared(
            connection,
            suffix="prepared",
            with_result_reference=False,
        )
        self._insert_revision(
            connection,
            session_id="session.legacy-only",
            payload_json='{"kind":"legacy-only"}',
        )
        connection.execute(
            "INSERT INTO attempt_reference("
            "attempt_id, session_id, session_revision, intent_digest, "
            "result_digest, observed_lifecycle, settlement_state, unknown_effect"
            ") VALUES ('attempt.legacy-only', 'session.legacy-only', 1, ?, ?, "
            "'failed', 'unsettled', 0)",
            (digest(b"legacy.intent"), digest(b"legacy.result")),
        )
        before_sessions = tuple(
            connection.execute(
                "SELECT object_id, revision, payload_json, payload_digest, row_digest "
                "FROM session_revision ORDER BY object_id, revision"
            )
        )
        before_intents = tuple(
            connection.execute(
                "SELECT intent_id, envelope_json, envelope_digest "
                "FROM outbox_intent ORDER BY intent_id"
            )
        )
        legacy_tables = (
            "inbox_receipt",
            "attempt_evidence_binding",
            "session_settlement",
        )
        legacy_columns = {
            table: tuple(
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            for table in legacy_tables
        }
        legacy_rows_before = {
            table: tuple(
                tuple(row)
                for row in connection.execute(
                    f"SELECT {', '.join(legacy_columns[table])} FROM {table} "
                    f"ORDER BY {legacy_columns[table][0]}"
                )
            )
            for table in legacy_tables
        }

        self._apply_v7(connection)

        after_sessions = tuple(
            connection.execute(
                "SELECT object_id, revision, payload_json, payload_digest, row_digest "
                "FROM session_revision ORDER BY object_id, revision"
            )
        )
        after_intents = tuple(
            connection.execute(
                "SELECT intent_id, envelope_json, envelope_digest "
                "FROM outbox_intent ORDER BY intent_id"
            )
        )
        self.assertEqual(after_sessions, before_sessions)
        self.assertEqual(after_intents, before_intents)
        for table in legacy_tables:
            self.assertEqual(
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {', '.join(legacy_columns[table])} FROM {table} "
                        f"ORDER BY {legacy_columns[table][0]}"
                    )
                ),
                legacy_rows_before[table],
            )
        rows = {
            str(row["attempt_id"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM attempt_reference ORDER BY attempt_id"
            )
        }
        self.assertEqual(rows[with_result["attempt_id"]]["reference_contract_version"], 6)
        self.assertEqual(rows[prepared_only["attempt_id"]]["reference_contract_version"], 6)
        self.assertIsNone(rows[prepared_only["attempt_id"]]["result_digest"])
        self.assertIsNone(rows[prepared_only["attempt_id"]]["observed_lifecycle"])
        self.assertIsNone(rows[prepared_only["attempt_id"]]["settlement_state"])
        self.assertIsNone(rows[prepared_only["attempt_id"]]["unknown_effect"])
        self.assertEqual(rows["attempt.legacy-only"]["reference_contract_version"], 1)
        self.assertEqual(
            rows[with_result["attempt_id"]]["intent_sha256"],
            with_result["intent_digest"],
        )
        for table in (
            "session_attempt_allocation",
            "prepared_attempt_receipt",
            "session_attempt_disposition_transition",
            "workspace_root_contract_transition",
        ):
            self.assertEqual(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
            )
        connection.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())

    def test_v7_legacy_projection_preserves_the_exact_root3_digest(self) -> None:
        connection = self._v6_connection()
        self.addCleanup(connection.close)
        result_backed = self._insert_v6_prepared(
            connection,
            suffix="root3-result",
            with_result_reference=True,
        )
        self._insert_v6_result_dependents(
            connection,
            fixture=result_backed,
            suffix="root3-result",
        )
        prepared_only = self._insert_v6_prepared(
            connection,
            suffix="root3-prepared",
            with_result_reference=False,
        )
        self._insert_revision(
            connection,
            session_id="session.root3-contract1",
            payload_json='{"kind":"root3-contract1"}',
        )
        connection.execute(
            "INSERT INTO attempt_reference("
            "attempt_id, session_id, session_revision, intent_digest, "
            "result_digest, observed_lifecycle, settlement_state, unknown_effect"
            ") VALUES ('attempt.root3-contract1', 'session.root3-contract1', 1, "
            "?, ?, 'failed', 'unsettled', 0)",
            (digest(b"root3.contract1.intent"), digest(b"root3.contract1.result")),
        )
        metadata = connection.execute(
            "SELECT project_id, canonical_authority_digest "
            "FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        assert metadata is not None
        source_root3 = _workspace_root_digest_for_connection(
            connection,
            project_id=str(metadata["project_id"]),
            canonical_authority_digest=str(metadata["canonical_authority_digest"]),
            root_digest_version=3,
            operating_mode="mission_runtime",
        )
        connection.execute(
            "UPDATE workspace_metadata SET current_root_digest = ? WHERE singleton = 1",
            (source_root3,),
        )

        self._apply_v7(connection)

        projected_root3 = _workspace_root_digest_for_connection(
            connection,
            project_id=str(metadata["project_id"]),
            canonical_authority_digest=str(metadata["canonical_authority_digest"]),
            root_digest_version=3,
            operating_mode="mission_runtime",
        )
        self.assertEqual(projected_root3, source_root3)
        self.assertEqual(
            connection.execute(
                "SELECT current_root_digest FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()[0],
            source_root3,
        )
        projected_attempts = tuple(
            (str(row["attempt_id"]), int(row["reference_contract_version"]))
            for row in connection.execute(
                "SELECT attempt_id, reference_contract_version "
                "FROM attempt_reference WHERE reference_contract_version = 1 "
                "OR (reference_contract_version = 6 AND result_digest IS NOT NULL) "
                "ORDER BY attempt_id"
            )
        )
        self.assertEqual(
            projected_attempts,
            tuple(
                sorted(
                    (
                        (result_backed["attempt_id"], 6),
                        ("attempt.root3-contract1", 1),
                    )
                )
            ),
        )
        prepared_projection = connection.execute(
            "SELECT reference_contract_version, result_digest "
            "FROM attempt_reference WHERE attempt_id = ?",
            (prepared_only["attempt_id"],),
        ).fetchone()
        assert prepared_projection is not None
        self.assertEqual(int(prepared_projection["reference_contract_version"]), 6)
        self.assertIsNone(prepared_projection["result_digest"])
        self.assertNotIn(
            prepared_only["attempt_id"],
            {attempt_id for attempt_id, _version in projected_attempts},
        )

    def test_v7_private_transition_commits_one_exact_root4_origin(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            database = Path(temporary) / "workspace.sqlite3"
            connection = self._v6_connection(database)
            self.addCleanup(connection.close)
            result_backed = self._insert_v6_prepared(
                connection,
                suffix="transition-result",
                with_result_reference=True,
            )
            self._insert_v6_result_dependents(
                connection,
                fixture=result_backed,
                suffix="transition-result",
            )
            self._insert_v6_prepared(
                connection,
                suffix="transition-prepared",
                with_result_reference=False,
            )
            self._seal_v6_genesis(connection)
            source_root = str(
                connection.execute(
                    "SELECT current_root_digest FROM workspace_metadata WHERE singleton = 1"
                ).fetchone()[0]
            )
            history_payload = [
                {
                    "version": int(row["version"]),
                    "name": str(row["name"]),
                    "digest_sha256": str(row["digest_sha256"]),
                    "applied_at": str(row["applied_at"]),
                }
                for row in connection.execute(
                    "SELECT version, name, digest_sha256, applied_at "
                    "FROM schema_migration ORDER BY version"
                )
            ]
            execution = _OfflineMigrationExecution(
                execution_id="migration.schema-v7-private",
                attempt_id="schema-v7-private.attempt.0001",
                source_schema_version=6,
                target_schema_version=7,
                verified_backup_manifest_sha256=digest(b"backup.schema-v7-private"),
                applied_history_sha256=digest(canonical_json_bytes(history_payload)),
                plan_sha256=digest(b"plan.schema-v7-private"),
                started_at="2026-08-10T12:02:00Z",
                completed_at="2026-08-10T12:03:00Z",
            )
            migration = load_migrations()[6]
            capability = _offline_migration_capability(
                database,
                root_identity="1" * 64,
            )

            _apply_offline_migration_plan(
                connection,
                pending_migrations=(
                    (migration.version, migration.name, migration.digest_sha256),
                ),
                execution=execution,
                capability=capability,
            )

            validated = _validated_schema_v7_root_transition(
                connection,
                execution=execution,
            )
            metadata = connection.execute(
                "SELECT * FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            transition = connection.execute(
                "SELECT * FROM workspace_root_contract_transition"
            ).fetchone()
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                7,
            )
            self.assertEqual(
                int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                1,
            )
            self.assertEqual(int(metadata["schema_version"]), 7)
            self.assertEqual(int(metadata["root_digest_version"]), 4)
            self.assertEqual(int(metadata["current_project_commit"]), 1)
            self.assertEqual(str(transition["source_root_digest"]), source_root)
            self.assertEqual(validated.project_commit_no, 1)
            self.assertEqual(validated.root_digest, str(metadata["current_root_digest"]))
            authorization = json.loads(
                str(
                    connection.execute(
                        "SELECT authorization_json FROM transition_journal "
                        "WHERE command_kind = 'workspace_schema_root_transition'"
                    ).fetchone()[0]
                )
            )
            self.assertEqual(
                authorization["migration_started_at"],
                execution.started_at,
            )
            self.assertEqual(
                authorization["applied_history_sha256"],
                execution.applied_history_sha256,
            )
            self.assertEqual(
                authorization["source_migration_set_digest"],
                migration_set_digest(load_migrations()[:6]),
            )
            self.assertEqual(
                authorization["source_schema_object_digest"],
                _expected_schema_object_digest_for_version(6),
            )
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM workspace_root_contract_transition"
                    ).fetchone()[0]
                ),
                1,
            )
            self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())
            connection.close()

    def test_v7_transition_faults_roll_back_and_retry_once(self) -> None:
        fault_points = (
            "after_v7_ddl",
            "after_schema_v7_history",
            "after_schema_v7_migration_execution",
            "after_schema_v7_user_version",
            "after_schema_v7_writer_epoch",
            "after_schema_v7_transition_row",
            "after_schema_v7_commit_placeholder",
            "after_schema_v7_journal",
            "after_schema_v7_root_computed",
            "after_schema_v7_command_result",
            "after_schema_v7_metadata",
            "before_migration_commit",
        )
        migration = load_migrations()[6]
        last_statement = len(tuple(_iter_statements(migration.sql)))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            for index, selected_point in enumerate(fault_points, start=1):
                with self.subTest(fault_point=selected_point):
                    database = Path(temporary) / f"fault-{index}.sqlite3"
                    (
                        connection,
                        execution,
                        capability,
                        pending,
                        source_metadata,
                    ) = self._v7_transition_fixture(
                        database,
                        suffix=f"fault-{index}",
                    )
                    try:

                        def inject(point, details):
                            is_final_ddl = (
                                selected_point == "after_v7_ddl"
                                and point == "after_migration_statement"
                                and int(details["statement_index"]) == last_statement
                            )
                            if point == selected_point or is_final_ddl:
                                raise RuntimeError(f"injected fault: {selected_point}")

                        with self.assertRaisesRegex(RuntimeError, "injected fault"):
                            _apply_offline_migration_plan(
                                connection,
                                pending_migrations=pending,
                                execution=execution,
                                capability=capability,
                                fault_hook=inject,
                            )
                        metadata = connection.execute(
                            "SELECT * FROM workspace_metadata WHERE singleton = 1"
                        ).fetchone()
                        self.assertEqual(
                            tuple((key, metadata[key]) for key in metadata.keys()),
                            source_metadata,
                        )
                        self.assertEqual(
                            int(connection.execute("PRAGMA user_version").fetchone()[0]),
                            6,
                        )
                        self.assertEqual(
                            int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                            1,
                        )
                        self.assertEqual(
                            tuple(
                                int(row[0])
                                for row in connection.execute(
                                    "SELECT version FROM schema_migration ORDER BY version"
                                )
                            ),
                            (1, 2, 3, 4, 5, 6),
                        )
                        self.assertIsNone(
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='workspace_root_contract_transition'"
                            ).fetchone()
                        )
                        for table, expected_count in (
                            ("project_commit", 1),
                            ("writer_epoch", 0),
                            ("transition_journal", 0),
                            ("command_result", 0),
                            ("migration_execution", 0),
                        ):
                            self.assertEqual(
                                int(
                                    connection.execute(
                                        f"SELECT COUNT(*) FROM {table}"
                                    ).fetchone()[0]
                                ),
                                expected_count,
                            )

                        _apply_offline_migration_plan(
                            connection,
                            pending_migrations=pending,
                            execution=execution,
                            capability=capability,
                        )
                        validated = _validated_schema_v7_root_transition(
                            connection,
                            execution=execution,
                        )
                        self.assertEqual(validated.project_commit_no, 1)
                        self.assertEqual(
                            int(
                                connection.execute(
                                    "SELECT COUNT(*) FROM workspace_root_contract_transition"
                                ).fetchone()[0]
                            ),
                            1,
                        )
                    finally:
                        connection.close()

    def test_migrated_root4_store_is_valid_historical_not_current_authority(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            paths = WorkspacePaths.from_root(root)
            (
                connection,
                execution,
                capability,
                pending,
                _source_metadata,
            ) = self._v7_transition_fixture(
                paths.database,
                suffix="integrity",
                root_identity=paths.root_identity,
                populated=False,
            )
            try:
                _apply_offline_migration_plan(
                    connection,
                    pending_migrations=pending,
                    execution=execution,
                    capability=capability,
                )
                history_payload = [
                    {
                        "version": int(row["version"]),
                        "name": str(row["name"]),
                        "digest_sha256": str(row["digest_sha256"]),
                        "applied_at": str(row["applied_at"]),
                    }
                    for row in connection.execute(
                        "SELECT version, name, digest_sha256, applied_at "
                        "FROM schema_migration ORDER BY version"
                    )
                ]
                migration8 = load_migrations()[7]
                execution8 = _OfflineMigrationExecution(
                    execution_id="migration.schema-v8-integrity",
                    attempt_id="schema-v8-integrity.attempt.0001",
                    source_schema_version=7,
                    target_schema_version=8,
                    verified_backup_manifest_sha256=digest(b"schema-v8.backup"),
                    applied_history_sha256=digest(
                        canonical_json_bytes(history_payload)
                    ),
                    plan_sha256=digest(b"schema-v8.plan"),
                    started_at="2026-08-13T12:00:00Z",
                    completed_at="2026-08-13T12:01:00Z",
                )
                _apply_offline_migration_plan(
                    connection,
                    pending_migrations=((
                        migration8.version,
                        migration8.name,
                        migration8.digest_sha256,
                    ),),
                    execution=execution8,
                    capability=_offline_migration_capability(
                        paths.database,
                        root_identity=paths.root_identity,
                    ),
                )
            finally:
                connection.close()

            historical_connection = _connect_workspace(paths.database)
            try:
                historical_connection.execute("PRAGMA journal_mode = DELETE")
                self.assertEqual(
                    _verify_historical_schema8_snapshot(
                        historical_connection
                    ).schema_version,
                    8,
                )
                self.assertEqual(
                    int(
                        historical_connection.execute(
                            "SELECT current_project_commit FROM workspace_metadata "
                            "WHERE singleton = 1"
                        ).fetchone()[0]
                    ),
                    2,
                )
                baseline_transition = _validated_schema_v7_root_transition(
                    historical_connection
                )
                _validated_schema_v8_root_refresh(historical_connection)
            finally:
                historical_connection.close()
            self.assertEqual(baseline_transition.writer_epoch, 1)
            with self.assertRaises(WorkspaceIntegrityError):
                WorkspaceStore(paths, "riemann_hypothesis").verify_integrity()

    def test_v7_guards_reject_ambiguous_or_orphaned_v6_state(self) -> None:
        cases = (
            "metadata_source",
            "duplicate_intent",
            "duplicate_settlement",
            "retry_lineage",
            "duplicate_chain",
            "binding_intent_mismatch",
            "reference_binding_mismatch",
            "partial_result",
            "orphan_receipt",
            "orphan_evidence",
        )
        for case in cases:
            with self.subTest(case=case):
                connection = self._v6_connection()
                try:
                    fixture = self._insert_v6_prepared(
                        connection,
                        suffix=case,
                        with_result_reference=case
                        in {"reference_binding_mismatch", "partial_result"},
                    )
                    if case == "metadata_source":
                        connection.execute(
                            "UPDATE workspace_metadata SET root_digest_version = 2"
                        )
                    elif case == "duplicate_intent":
                        connection.execute(
                            "INSERT INTO outbox_intent("
                            "intent_id, session_id, session_revision, "
                            "envelope_digest, envelope_json, lifecycle, created_at"
                            ") VALUES ('intent.duplicate', ?, 1, ?, '{}', "
                            "'pending', '2026-08-10T12:03:00Z')",
                            (fixture["session_id"], digest(b"duplicate")),
                        )
                    elif case == "duplicate_settlement":
                        for ordinal in (1, 2):
                            connection.execute(
                                "INSERT INTO session_settlement("
                                "settlement_id, session_id, session_revision, "
                                "attempt_references_json, charge_basis_json, "
                                "release_conversion_json, evidence_binding_id, "
                                "command_id, created_at) VALUES (?, ?, 1, '[]', "
                                "'{}', '{}', NULL, ?, '2026-08-10T12:03:00Z')",
                                (
                                    f"settlement.duplicate.{ordinal}",
                                    fixture["session_id"],
                                    f"command.duplicate.{ordinal}",
                                ),
                            )
                    elif case == "retry_lineage":
                        connection.execute(
                            "UPDATE session_attempt_binding "
                            "SET attempt_sequence = 2 WHERE attempt_id = ?",
                            (fixture["attempt_id"],),
                        )
                    elif case == "duplicate_chain":
                        second = self._insert_v6_prepared(
                            connection,
                            suffix="duplicate-chain-second",
                            with_result_reference=False,
                        )
                        connection.execute(
                            "UPDATE session_attempt_binding SET attempt_chain_id = ? "
                            "WHERE attempt_id = ?",
                            (f"chain.{case}", second["attempt_id"]),
                        )
                    elif case == "binding_intent_mismatch":
                        connection.execute(
                            "UPDATE outbox_intent SET envelope_digest = ? "
                            "WHERE intent_id = ?",
                            (digest(b"mismatch"), fixture["intent_id"]),
                        )
                    elif case == "reference_binding_mismatch":
                        connection.execute(
                            "UPDATE attempt_reference SET intent_digest = ? "
                            "WHERE attempt_id = ?",
                            (digest(b"reference-mismatch"), fixture["attempt_id"]),
                        )
                    elif case == "partial_result":
                        connection.execute(
                            "UPDATE attempt_reference SET result_digest = NULL "
                            "WHERE attempt_id = ?",
                            (fixture["attempt_id"],),
                        )
                    elif case == "orphan_receipt":
                        connection.execute(
                            "INSERT INTO inbox_receipt("
                            "receipt_id, attempt_id, envelope_digest, envelope_json, "
                            "lifecycle, created_at"
                            ") VALUES ('receipt.orphan', 'attempt.orphan', ?, '{}', "
                            "'received', '2026-08-10T12:03:00Z')",
                            (digest(b"orphan"),),
                        )
                    else:
                        connection.execute(
                            "INSERT INTO attempt_evidence_binding("
                            "binding_id, attempt_id, result_receipt_id, "
                            "result_digest, evidence_id, evidence_revision, "
                            "evidence_payload_digest, closure_id, closure_digest, "
                            "created_at) VALUES ('binding.orphan', ?, "
                            "'receipt.orphan', ?, 'evidence.orphan', 1, ?, "
                            "'closure.orphan', ?, '2026-08-10T12:03:00Z')",
                            (
                                fixture["attempt_id"],
                                digest(b"result.orphan"),
                                digest(b"evidence.orphan"),
                                digest(b"closure.orphan"),
                            ),
                        )
                    with self.assertRaises(sqlite3.IntegrityError):
                        self._apply_v7(connection)
                finally:
                    connection.close()

    def test_v7_composite_foreign_keys_reject_cross_wired_authority(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys = OFF")
        self._apply_through(connection, 7)
        connection.execute("PRAGMA foreign_keys = ON")
        first = self._insert_v7_attempt(connection, suffix="first")
        second = self._insert_v7_attempt(connection, suffix="second")

        def insert_second_receipt(fixture: dict[str, str], suffix: str) -> str:
            receipt_id = f"receipt.v7.{suffix}.2"
            connection.execute(
                "INSERT INTO inbox_receipt("
                "receipt_id, receipt_contract_version, attempt_id, "
                "execution_intent_id, attempt_chain_id, attempt_sequence, "
                "observation_sequence, predecessor_receipt_id, "
                "predecessor_receipt_sha256, envelope_digest, envelope_json, "
                "termination_observation_sha256, lifecycle, created_at) "
                "VALUES (?, 7, ?, ?, ?, 1, 2, ?, ?, ?, '{}', ?, 'received', "
                "'2026-08-10T12:05:00Z')",
                (
                    receipt_id,
                    fixture["attempt_id"],
                    fixture["intent_id"],
                    fixture["chain_id"],
                    fixture["receipt_id"],
                    digest((suffix + ".receipt-1").encode("utf-8")),
                    digest((suffix + ".result-2").encode("utf-8")),
                    digest((suffix + ".observation-2").encode("utf-8")),
                ),
            )
            return receipt_id

        first_receipt_2 = insert_second_receipt(first, "first")
        second_receipt_2 = insert_second_receipt(second, "second")
        first_disposition_2 = "disposition.v7.first.2"
        connection.execute(
            "INSERT INTO session_attempt_disposition_transition("
            "disposition_id, disposition_contract_version, session_id, "
            "session_revision, attempt_id, transition_sequence, "
            "predecessor_disposition_id, predecessor_disposition_sha256, "
            "result_receipt_id, result_digest, termination_observation_sha256, "
            "action, normalized_reason, disposition_sha256, created_at, row_digest) "
            "VALUES (?, 1, ?, 1, ?, 2, ?, ?, ?, ?, ?, 'terminal_operational', "
            "'provider_failed', ?, '2026-08-10T12:05:00Z', ?)",
            (
                first_disposition_2,
                first["session_id"],
                first["attempt_id"],
                first["disposition_id"],
                first["disposition_sha256"],
                first_receipt_2,
                digest(b"first.result-2"),
                digest(b"first.observation-2"),
                digest(b"first.disposition-2"),
                digest(b"first.disposition-row-2"),
            ),
        )

        mutations = {
            "attempt_allocation_scope": (
                "UPDATE attempt_reference SET allocation_id = ?, "
                "allocation_sha256 = ? WHERE attempt_id = ?",
                (
                    second["allocation_id"],
                    second["allocation_sha256"],
                    first["attempt_id"],
                ),
            ),
            "receipt_attempt_identity": (
                "UPDATE inbox_receipt SET execution_intent_id = ?, "
                "attempt_chain_id = ? WHERE receipt_id = ?",
                (second["intent_id"], second["chain_id"], first["receipt_id"]),
            ),
            "receipt_predecessor_attempt": (
                "UPDATE inbox_receipt SET predecessor_receipt_id = ? "
                "WHERE receipt_id = ?",
                (second["receipt_id"], first_receipt_2),
            ),
            "disposition_attempt_scope": (
                "UPDATE session_attempt_disposition_transition SET attempt_id = ? "
                "WHERE disposition_id = ?",
                (second["attempt_id"], first["disposition_id"]),
            ),
            "disposition_result_attempt": (
                "UPDATE session_attempt_disposition_transition "
                "SET result_receipt_id = ? WHERE disposition_id = ?",
                (second_receipt_2, first["disposition_id"]),
            ),
            "disposition_predecessor_session": (
                "UPDATE session_attempt_disposition_transition "
                "SET predecessor_disposition_id = ? WHERE disposition_id = ?",
                (second["disposition_id"], first_disposition_2),
            ),
            "binding_allocation_scope": (
                "UPDATE session_attempt_binding SET allocation_id = ? "
                "WHERE binding_id = ?",
                (second["allocation_id"], first["binding_id"]),
            ),
        }
        for label, (statement, parameters) in mutations.items():
            with self.subTest(label=label):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement, parameters)

        connection.execute("SAVEPOINT prepared_receipt_cross_wire")
        try:
            connection.execute(
                "DELETE FROM prepared_attempt_receipt WHERE attempt_id = ?",
                (second["attempt_id"],),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE prepared_attempt_receipt SET attempt_id = ? "
                    "WHERE attempt_id = ?",
                    (second["attempt_id"], first["attempt_id"]),
                )
        finally:
            connection.execute("ROLLBACK TO prepared_receipt_cross_wire")
            connection.execute("RELEASE prepared_receipt_cross_wire")

        evidence_id = "evidence.v7.cross-wire"
        closure_id = "closure.v7.cross-wire"
        connection.execute(
            "INSERT INTO evidence_item_revision("
            "evidence_id, revision, subtype, subject_json, exact_scope_json, rigor, "
            "limitations_json, non_inferences_json, security_json, retention_json, "
            "availability_state, canonical_effect, payload_digest, created_at) "
            "VALUES (?, 1, 'research_attempt_result', '{}', '{}', 'bounded', "
            "'[]', '[]', '{}', '{}', 'verified_available', 'none', ?, "
            "'2026-08-10T12:05:00Z')",
            (evidence_id, digest(b"evidence.v7.cross-wire")),
        )
        connection.execute(
            "INSERT INTO closure_manifest("
            "closure_id, root_kind, root_digest, members_json, contract_json, "
            "created_at) VALUES (?, 'evidence_item', ?, '[]', '{}', "
            "'2026-08-10T12:05:00Z')",
            (closure_id, digest(b"closure.v7.cross-wire")),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO attempt_evidence_binding("
                "binding_id, binding_contract_version, attempt_id, "
                "result_receipt_id, result_digest, evidence_id, evidence_revision, "
                "evidence_payload_digest, closure_id, closure_digest, created_at) "
                "VALUES ('evidence-binding.v7.cross-wire', 7, ?, ?, ?, ?, 1, ?, ?, ?, "
                "'2026-08-10T12:05:00Z')",
                (
                    first["attempt_id"],
                    second["receipt_id"],
                    second["result_digest"],
                    evidence_id,
                    digest(b"evidence.v7.cross-wire"),
                    closure_id,
                    digest(b"closure.v7.cross-wire"),
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO session_settlement("
                "settlement_id, settlement_contract_version, session_id, "
                "session_revision, attempt_references_json, attempt_chain_sha256, "
                "final_attempt_id, allocation_id, disposition_head_id, "
                "disposition_head_sha256, charge_basis_json, charge_basis_sha256, "
                "release_conversion_json, release_conversion_sha256, "
                "evidence_binding_id, command_id, created_at) "
                "VALUES ('settlement.v7.cross-wire', 7, ?, 1, '[]', ?, ?, ?, ?, ?, "
                "'{}', ?, '{}', ?, NULL, 'command.v7.cross-wire', "
                "'2026-08-10T12:05:00Z')",
                (
                    first["session_id"],
                    digest(b"chain.cross-wire"),
                    first["attempt_id"],
                    second["allocation_id"],
                    first["disposition_id"],
                    first["disposition_sha256"],
                    digest(b"charge.cross-wire"),
                    digest(b"release.cross-wire"),
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO inbox_receipt("
                "receipt_id, receipt_contract_version, attempt_id, "
                "execution_intent_id, attempt_chain_id, attempt_sequence, "
                "observation_sequence, predecessor_receipt_id, "
                "predecessor_receipt_sha256, envelope_digest, envelope_json, "
                "termination_observation_sha256, lifecycle, created_at) "
                "VALUES ('receipt.v7.legacy-orphan', 1, 'attempt.missing', NULL, "
                "NULL, NULL, NULL, NULL, NULL, ?, '{}', NULL, 'received', "
                "'2026-08-10T12:05:00Z')",
                (digest(b"legacy-orphan-receipt"),),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO session_settlement("
                "settlement_id, settlement_contract_version, session_id, "
                "session_revision, attempt_references_json, attempt_chain_sha256, "
                "final_attempt_id, allocation_id, disposition_head_id, "
                "disposition_head_sha256, charge_basis_json, charge_basis_sha256, "
                "release_conversion_json, release_conversion_sha256, "
                "evidence_binding_id, command_id, created_at) "
                "VALUES ('settlement.v7.legacy-orphan-evidence', 1, ?, 1, '[]', "
                "NULL, NULL, NULL, NULL, NULL, '{}', NULL, '{}', NULL, "
                "'evidence-binding.missing', 'command.v7.legacy-orphan-evidence', "
                "'2026-08-10T12:05:00Z')",
                (first["session_id"],),
            )
        connection.execute(
            "INSERT INTO workspace_metadata("
            "singleton, project_id, application_version, schema_version, "
            "root_identity, lifecycle, canonical_authority_json, "
            "canonical_authority_digest, current_writer_epoch, "
            "current_project_commit, current_root_digest, transition_head_digest, "
            "created_at, updated_at, operating_mode, root_digest_version) "
            "VALUES (1, 'riemann_hypothesis', ?, 7, ?, 'quiesced', '{}', ?, "
            "NULL, 0, ?, NULL, '2026-08-10T12:05:00Z', "
            "'2026-08-10T12:05:00Z', 'mission_runtime', 4)",
            (APPLICATION_VERSION, "1" * 64, "2" * 64, "3" * 64),
        )
        connection.execute(
            "INSERT INTO writer_epoch("
            "epoch, project_id, owner, lifecycle, creation_basis, "
            "predecessor_epoch, takeover_evidence_json, created_at, closed_at, "
            "row_digest) VALUES (1, 'riemann_hypothesis', 'migration.v7', "
            "'quiesced', 'migration', NULL, NULL, '2026-08-10T12:05:00Z', "
            "'2026-08-10T12:05:00Z', ?)",
            (digest(b"writer.v7"),),
        )
        connection.execute(
            "INSERT INTO project_commit("
            "commit_no, project_id, root_digest, canonical_authority_digest, "
            "transition_head_digest, command_id, created_by, created_at) "
            "VALUES (0, 'riemann_hypothesis', ?, ?, NULL, NULL, 'migration.v7', "
            "'2026-08-10T12:05:00Z')",
            ("3" * 64, "2" * 64),
        )
        for ordinal in (1, 2):
            connection.execute(
                "INSERT INTO migration_execution("
                "execution_id, attempt_id, source_schema_version, "
                "target_schema_version, verified_backup_manifest_sha256, "
                "applied_history_sha256, plan_sha256, started_at, completed_at, "
                "status, canonical_effect) VALUES (?, ?, 6, 7, ?, ?, ?, "
                "'2026-08-10T12:05:00Z', '2026-08-10T12:05:00Z', "
                "'applied', 'none')",
                (
                    f"migration.execution.{ordinal}",
                    f"migration.attempt.{ordinal}",
                    digest(f"backup.{ordinal}".encode("utf-8")),
                    digest(f"history.{ordinal}".encode("utf-8")),
                    digest(f"plan.{ordinal}".encode("utf-8")),
                ),
            )
        root_transition_sql = (
            "INSERT INTO workspace_root_contract_transition("
            "transition_id, command_id, writer_epoch, migration_execution_id, "
            "migration_attempt_id, plan_sha256, "
            "verified_backup_manifest_sha256, source_project_commit, "
            "source_root_digest, source_transition_head_digest, "
            "canonical_authority_digest, source_schema_version, "
            "target_schema_version, source_root_digest_version, "
            "target_root_digest_version, target_migration_set_digest, "
            "target_schema_object_digest, canonical_effect, created_at, "
            "row_digest) VALUES (?, ?, 1, ?, ?, ?, ?, 0, ?, ?, ?, 6, 7, 3, 4, "
            "?, ?, 'none', '2026-08-10T12:05:00Z', ?)"
        )
        common_root_values = (
            digest(b"root-transition.plan"),
            digest(b"root-transition.backup"),
            "3" * 64,
            "2" * 64,
            digest(b"root-transition.migration-set"),
            digest(b"root-transition.schema"),
            digest(b"root-transition.row"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                root_transition_sql,
                (
                    "root-transition.cross-execution",
                    "command.root-transition.cross-execution",
                    "migration.execution.1",
                    "migration.attempt.2",
                    common_root_values[0],
                    common_root_values[1],
                    common_root_values[2],
                    None,
                    common_root_values[3],
                    common_root_values[4],
                    common_root_values[5],
                    common_root_values[6],
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                root_transition_sql,
                (
                    "root-transition.bad-head",
                    "command.root-transition.bad-head",
                    "migration.execution.1",
                    "migration.attempt.1",
                    common_root_values[0],
                    common_root_values[1],
                    common_root_values[2],
                    "not-a-digest",
                    common_root_values[3],
                    common_root_values[4],
                    common_root_values[5],
                    common_root_values[6],
                ),
            )
        self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())

    def test_v7_offline_apply_requires_exact_schema6_suffix(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-schema-v7-apply-") as temporary:
            database = Path(temporary) / "workspace.sqlite3"
            connection = sqlite3.connect(database, isolation_level=None)
            try:
                self._apply_through(connection, 1)
                migrations = load_migrations()
                pending = tuple(
                    (item.version, item.name, item.digest_sha256)
                    for item in migrations
                    if 1 < item.version <= 7
                )
                execution = _OfflineMigrationExecution(
                    execution_id="migration.invalid-v1-v7",
                    attempt_id="migration.invalid-v1-v7.attempt.1",
                    source_schema_version=1,
                    target_schema_version=7,
                    verified_backup_manifest_sha256=digest(b"backup"),
                    applied_history_sha256=digest(b"history"),
                    plan_sha256=digest(b"plan"),
                    started_at="2026-08-10T12:06:00Z",
                    completed_at="2026-08-10T12:06:01Z",
                )
                capability = _offline_migration_capability(
                    database,
                    root_identity=digest(b"root"),
                )
                with self.assertRaises(WorkspaceSchemaError):
                    _apply_offline_migration_plan(
                        connection,
                        pending_migrations=pending,
                        execution=execution,
                        capability=capability,
                    )
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 0
                )
                self.assertEqual(
                    tuple(
                        row[0]
                        for row in connection.execute(
                            "SELECT version FROM schema_migration ORDER BY version"
                        )
                    ),
                    (1,),
                )
            finally:
                connection.close()

    def test_public_plan_rejects_non_v6_source_targeting_schema7(self) -> None:
        migration = load_migrations()[0]
        manifest = SimpleNamespace(
            manifest_sha256=digest(b"manifest"),
            store=SimpleNamespace(
                schema_version=1,
                migration_history=(
                    SimpleNamespace(
                        version=1,
                        name=migration.name,
                        digest_sha256=migration.digest_sha256,
                    ),
                ),
                migration_history_sha256=digest(b"history"),
                operating_mode="inactive_foundation",
                root_digest_version=1,
                current_writer_epoch=None,
                writer_lifecycle=RecoveryLifecycle.OFFLINE.value,
            ),
        )
        backup = BackupSet(path=Path("synthetic-v1-backup"), manifest=manifest)
        with patch(
            "research_core.workspace_recovery.verify_backup_set",
            return_value=backup,
        ):
            with self.assertRaises(RecoveryError) as captured:
                prepare_offline_migration(
                    backup=backup,
                    target_schema_version=7,
                    lifecycle=RecoveryLifecycle.OFFLINE,
                )
        self.assertEqual(captured.exception.code, "migration_history_unsupported")

    def test_target7_public_preflight_rejects_nonmission_sources_before_artifacts(
        self,
    ) -> None:
        migrations = load_migrations()
        history = tuple(
            SimpleNamespace(
                version=item.version,
                name=item.name,
                digest_sha256=item.digest_sha256,
            )
            for item in migrations[:6]
        )
        pending = (
            (
                migrations[6].version,
                migrations[6].name,
                migrations[6].digest_sha256,
            ),
        )
        cases = {
            "inactive_root1": {
                "operating_mode": "inactive_foundation",
                "root_digest_version": 1,
                "current_writer_epoch": None,
                "writer_lifecycle": RecoveryLifecycle.OFFLINE.value,
            },
            "mission_wrong_root1": {
                "operating_mode": "mission_runtime",
                "root_digest_version": 1,
                "current_writer_epoch": None,
                "writer_lifecycle": RecoveryLifecycle.OFFLINE.value,
            },
            "non_mission_observer": {
                "operating_mode": "pre_cutover_observer",
                "root_digest_version": 2,
                "current_writer_epoch": None,
                "writer_lifecycle": RecoveryLifecycle.QUIESCED.value,
            },
            "current_writer": {
                "operating_mode": "mission_runtime",
                "root_digest_version": 3,
                "current_writer_epoch": 1,
                "writer_lifecycle": RecoveryLifecycle.ACTIVE.value,
            },
        }
        with tempfile.TemporaryDirectory(
            prefix="codex-schema-v7-preflight-"
        ) as temporary:
            owner = Path(temporary)
            for name, values in cases.items():
                with self.subTest(source=name):
                    history_sha256 = digest(f"history.{name}".encode("utf-8"))
                    manifest_sha256 = digest(f"manifest.{name}".encode("utf-8"))
                    store_binding = SimpleNamespace(
                        schema_version=6,
                        migration_history=history,
                        migration_history_sha256=history_sha256,
                        **values,
                    )
                    backup = BackupSet(
                        path=owner / name / "backups" / "backup-source",
                        manifest=SimpleNamespace(
                            manifest_sha256=manifest_sha256,
                            store=store_binding,
                        ),
                    )
                    plan = OfflineMigrationPlan(
                        current_schema_version=6,
                        target_schema_version=7,
                        verified_backup_manifest_sha256=manifest_sha256,
                        applied_history_sha256=history_sha256,
                        pending_migrations=pending,
                        lifecycle=RecoveryLifecycle.OFFLINE,
                    )
                    with patch(
                        "research_core.workspace_recovery.verify_backup_set",
                        return_value=backup,
                    ):
                        with self.assertRaises(RecoveryError):
                            prepare_offline_migration(
                                backup=backup,
                                target_schema_version=7,
                                lifecycle=RecoveryLifecycle.OFFLINE,
                            )
                    with patch(
                        "research_core.workspace_recovery."
                        "_verify_backup_for_trust_transition",
                        return_value=backup,
                    ):
                        with self.assertRaises(MigrationExecutionError):
                            execute_offline_migration(
                                plan=plan,
                                backup=backup,
                                migration_id=f"preflight-{name}",
                                actor="test.schema-v7-preflight",
                            )
                    self.assertFalse((owner / name / "recovery").exists())

    def test_v7_postinspection_rejects_a_stale_root_transition(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="codex-schema-v7-inspection-",
            ignore_cleanup_errors=True,
        ) as temporary:
            database = Path(temporary) / "workspace.sqlite3"
            (
                connection,
                execution,
                capability,
                pending,
                source_metadata_items,
            ) = self._v7_transition_fixture(
                database,
                suffix="inspection",
                populated=False,
            )
            source_metadata = dict(source_metadata_items)
            source_history = tuple(
                SimpleNamespace(
                    version=int(row["version"]),
                    name=str(row["name"]),
                    digest_sha256=str(row["digest_sha256"]),
                )
                for row in connection.execute(
                    "SELECT version, name, digest_sha256 FROM schema_migration "
                    "WHERE version <= 6 ORDER BY version"
                )
            )
            _apply_offline_migration_plan(
                connection,
                pending_migrations=pending,
                execution=execution,
                capability=capability,
            )
            plan = OfflineMigrationPlan(
                current_schema_version=6,
                target_schema_version=7,
                verified_backup_manifest_sha256=(
                    execution.verified_backup_manifest_sha256
                ),
                applied_history_sha256=execution.applied_history_sha256,
                pending_migrations=pending,
                lifecycle=RecoveryLifecycle.QUIESCED,
            )
            source_store = SimpleNamespace(
                project_id="riemann_hypothesis",
                root_identity=str(source_metadata["root_identity"]),
                application_version=str(source_metadata["application_version"]),
                schema_version=6,
                operating_mode="mission_runtime",
                root_digest_version=3,
                migration_history=source_history,
                migration_history_sha256=execution.applied_history_sha256,
                canonical_authority_digest=str(
                    source_metadata["canonical_authority_digest"]
                ),
                current_writer_epoch=None,
                writer_epoch_high_watermark=0,
                writer_lifecycle=str(source_metadata["lifecycle"]),
                project_commit_id=int(source_metadata["current_project_commit"]),
                project_root_digest=str(source_metadata["current_root_digest"]),
                transition_head=source_metadata["transition_head_digest"],
            )
            verified = BackupSet(
                path=Path(temporary) / "backup-source",
                manifest=SimpleNamespace(
                    manifest_sha256=execution.verified_backup_manifest_sha256,
                    store=source_store,
                ),
            )
            state = {
                "migration_id": execution.execution_id.removeprefix("migration."),
                "source_logical_sha256": digest(b"unused-source-logical"),
            }
            exact = _inspect_database(
                connection,
                state=state,
                plan=plan,
                verified=verified,
                plan_sha256=execution.plan_sha256,
            )
            self.assertEqual(exact.classification, "exact_commit")

            for field, wrong_value in (
                ("project_id", "foreign_project"),
                ("root_identity", "f" * 64),
                ("canonical_authority_digest", "e" * 64),
                ("project_commit_id", 1),
                ("project_root_digest", "d" * 64),
                ("transition_head", "c" * 64),
                ("writer_epoch_high_watermark", 1),
                ("writer_lifecycle", RecoveryLifecycle.OFFLINE.value),
            ):
                with self.subTest(stale_source_field=field):
                    stale_store = SimpleNamespace(
                        **{
                            **vars(source_store),
                            field: wrong_value,
                        }
                    )
                    stale_verified = BackupSet(
                        path=verified.path,
                        manifest=SimpleNamespace(
                            manifest_sha256=execution.verified_backup_manifest_sha256,
                            store=stale_store,
                        ),
                    )
                    stale_source = _inspect_database(
                        connection,
                        state=state,
                        plan=plan,
                        verified=stale_verified,
                        plan_sha256=execution.plan_sha256,
                    )
                    self.assertEqual(stale_source.classification, "ambiguous")

            connection.execute(
                "UPDATE workspace_metadata SET application_version = 'stale-version' "
                "WHERE singleton = 1"
            )
            stale_application = _inspect_database(
                connection,
                state=state,
                plan=plan,
                verified=verified,
                plan_sha256=execution.plan_sha256,
            )
            self.assertEqual(stale_application.classification, "ambiguous")
            connection.execute(
                "UPDATE workspace_metadata SET application_version = ? "
                "WHERE singleton = 1",
                (APPLICATION_VERSION,),
            )

            connection.execute(
                "UPDATE workspace_root_contract_transition SET plan_sha256 = ?",
                (digest(b"stale-transition-plan"),),
            )
            stale = _inspect_database(
                connection,
                state=state,
                plan=plan,
                verified=verified,
                plan_sha256=execution.plan_sha256,
            )
            self.assertEqual(stale.classification, "ambiguous")
            connection.close()


class LogicalDatabaseDigestTests(unittest.TestCase):
    """Small parity/resource oracles, not retained-corpus performance evidence."""

    @staticmethod
    def _reference_digest(connection: sqlite3.Connection) -> str:
        # Independent copy of the retired materializing algorithm, confined to
        # tiny fixtures. Do not derive the expected order/bytes from sort runs.
        tables = tuple(str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ))
        payload = []
        for table in tables:
            rows = [[repr(value) for value in tuple(row)]
                    for row in connection.execute(f'SELECT * FROM "{table}"')]
            rows.sort()
            payload.append({"table": table, "rows": rows})
        return digest(canonical_json_bytes({
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "schema_object_sha256": schema_object_digest(connection),
            "tables": payload,
        }))

    @staticmethod
    def _fixture(connection: sqlite3.Connection) -> list[tuple]:
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE "z empty" (value)')
        connection.execute('CREATE TABLE "a data" (first, second, third)')
        # No affinity: integer and real repr forms must remain distinguishable.
        rows = [
            (None, 1, b"\x00\xff\n"), (1, 1.0, -0.0), (10, 2, float("inf")),
            (2, -10, float("-inf")), ("Straße", "ß", "\U0001f642"),
            ("\x00\n\r\t", "'\"\\", "prefix"), ("prefix", "prefix-long", b"'\"\\"),
            ("é", "e\u0301", "\uffff"), ("\U00010000", "x", b""),
            ("'", "JSON-escaped ordering differs", 0), ("\"", "repr ordering", 0),
        ]
        rows.extend((str(value), value, "same prefix " + str(value)) for value in range(24))
        rows.extend(rows[:3])
        connection.executemany('INSERT INTO "a data" VALUES (?,?,?)', reversed(rows))
        connection.execute("PRAGMA user_version = 37")
        return rows

    def test_digest_matches_reference_for_repr_types_duplicates_and_row_order(self) -> None:
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(migration_module._logical_database_digest(connection),
                             self._reference_digest(connection))
            rows = self._fixture(connection)
            expected = self._reference_digest(connection)
            self.assertEqual(migration_module._logical_database_digest(connection), expected)
            connection.execute('DELETE FROM "a data"')
            connection.executemany('INSERT INTO "a data" VALUES (?,?,?)', rows)
            connection.execute("VACUUM")
            self.assertEqual(migration_module._logical_database_digest(connection), expected)
            connection.execute('INSERT INTO "a data" VALUES (?,?,?)', rows[0])
            changed = migration_module._logical_database_digest(connection)
            self.assertNotEqual(changed, expected)
            self.assertEqual(changed, self._reference_digest(connection))

    def test_digest_binds_schema_user_version_and_all_application_tables(self) -> None:
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            self._fixture(connection)
            previous = migration_module._logical_database_digest(connection)
            for statement in (
                'CREATE INDEX data_second ON "a data" (second)',
                'CREATE TRIGGER data_insert AFTER INSERT ON "a data" BEGIN SELECT 1; END',
                "PRAGMA user_version = 38",
                "CREATE TABLE generated_index_nodes (node_json)",
                "INSERT INTO generated_index_nodes VALUES ('derived but still included')",
                "CREATE TABLE sequence_owner (id INTEGER PRIMARY KEY AUTOINCREMENT)",
            ):
                connection.execute(statement)
                current = migration_module._logical_database_digest(connection)
                self.assertNotEqual(current, previous)
                self.assertEqual(current, self._reference_digest(connection))
                previous = current
            connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('sequence_owner',99)")
            self.assertEqual(migration_module._logical_database_digest(connection), previous)

    def test_digest_spills_multiple_passes_with_bounded_fan_in_and_oversized_row(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rh-digest-test-") as directory:
            parent = Path(directory)
            temporary_directory = tempfile.TemporaryDirectory
            made = []
            writes = []
            merges = []
            progress = []
            write_run = migration_module._write_logical_digest_run
            merge_runs = migration_module._merge_logical_digest_runs

            def scratch(**kwargs):
                result = temporary_directory(dir=parent, **kwargs)
                made.append(Path(result.name))
                return result

            def write(path, rows):
                if path.name.startswith("0-"):
                    self.assertIsInstance(rows, list)
                    sizes = [sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row)
                             for row in rows]
                    writes.append((len(rows), sum(sizes) + sys.getsizeof(rows), max(sizes)))
                return write_run(path, rows)

            def merge(paths, target):
                merges.append((len(paths), int(target.name.split("-")[0])))
                return merge_runs(paths, target)

            with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
                source_rows = self._fixture(connection)
                connection.execute('INSERT INTO "a data" VALUES (?,?,?)', ("x" * 65_536, 0, b"large"))
                expected = self._reference_digest(connection)
                with (
                    patch.object(migration_module, "_LOGICAL_DIGEST_SORT_BYTES", 512),
                    patch.object(migration_module, "_LOGICAL_DIGEST_MERGE_FAN_IN", 2),
                    patch.object(migration_module.tempfile, "TemporaryDirectory", side_effect=scratch),
                    patch.object(migration_module, "_write_logical_digest_run", side_effect=write),
                    patch.object(migration_module, "_merge_logical_digest_runs", side_effect=merge),
                    preparation_progress_sink(progress.append),
                ):
                    self.assertEqual(migration_module._logical_database_digest(connection), expected)
            row_count = len(source_rows) + 1
            self.assertEqual(progress[-1], {
                "phase": "logical_digest_rows", "completed": row_count, "total": row_count})
            sort_progress = [item for item in progress
                             if item["phase"] == "logical_digest_sort_rows"]
            self.assertTrue(any(0 < item["completed"] <= row_count and item["total"] is None
                                for item in sort_progress))
            self.assertIn({"phase": "logical_digest_sort_rows", "completed": row_count,
                           "total": row_count}, sort_progress)
            self.assertEqual(sort_progress[-1], {
                "phase": "logical_digest_sort_rows", "completed": 0, "total": 0})
            self.assertGreater(len(writes), 4)
            self.assertTrue(any(count == 1 and largest > 65_536 for count, _size, largest in writes))
            self.assertTrue(all(size <= 512 + largest for _count, size, largest in writes))
            self.assertTrue(merges)
            self.assertLessEqual(max(count for count, _level in merges), 2)
            self.assertGreaterEqual(max(level for _count, level in merges), 3)
            self.assertTrue(made)
            self.assertTrue(all(not path.exists() for path in made))
            self.assertEqual(list(parent.iterdir()), [])

    def test_digest_cleanup_on_sort_write_merge_and_final_hash_interruption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rh-digest-test-") as directory:
            parent = Path(directory)
            temporary_directory = tempfile.TemporaryDirectory
            write_run = migration_module._write_logical_digest_run
            row_encoder = migration_module.canonical_json_bytes
            for failure in ("write", "merge", "hash"):
                with self.subTest(failure=failure), closing(
                    sqlite3.connect(":memory:", isolation_level=None)
                ) as connection:
                    self._fixture(connection)
                    expected = self._reference_digest(connection)
                    made = []
                    started_output = False

                    def scratch(**kwargs):
                        result = temporary_directory(dir=parent, **kwargs)
                        made.append(Path(result.name))
                        return result

                    def write(path, rows):
                        if failure == "write" and path.name.startswith("0-"):
                            def broken():
                                yield next(iter(rows))
                                raise OSError("injected sort disk full")
                            return write_run(path, broken())
                        if failure == "merge" and not path.name.startswith("0-"):
                            def interrupted():
                                yield next(iter(rows))
                                raise KeyboardInterrupt("injected merge interruption")
                            return write_run(path, interrupted())
                        return write_run(path, rows)

                    original_rows = migration_module._sorted_logical_digest_rows

                    def rows(*args):
                        nonlocal started_output
                        with closing(original_rows(*args)) as ordered:
                            for row in ordered:
                                started_output = True
                                yield row

                    def encode(value):
                        if failure == "hash" and started_output:
                            raise KeyboardInterrupt("injected final hash interruption")
                        return row_encoder(value)

                    connection.execute("BEGIN")
                    with (
                        patch.object(migration_module, "_LOGICAL_DIGEST_SORT_BYTES", 1),
                        patch.object(migration_module, "_LOGICAL_DIGEST_MERGE_FAN_IN", 2),
                        patch.object(migration_module.tempfile, "TemporaryDirectory", side_effect=scratch),
                        patch.object(migration_module, "_write_logical_digest_run", side_effect=write),
                        patch.object(migration_module, "_sorted_logical_digest_rows", side_effect=rows),
                        patch.object(migration_module, "canonical_json_bytes", side_effect=encode),
                        self.assertRaisesRegex((OSError, KeyboardInterrupt), "injected"),
                    ):
                        migration_module._logical_database_digest(connection)
                    self.assertTrue(connection.in_transaction)
                    self.assertEqual(self._reference_digest(connection), expected)
                    connection.rollback()
                    self.assertTrue(made)
                    self.assertTrue(all(not path.exists() for path in made))
                    self.assertEqual(list(parent.iterdir()), [])

    def test_digest_preserves_read_only_source_and_caller_transaction(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rh-digest-test-") as directory:
            database = Path(directory) / "source.sqlite3"
            with closing(sqlite3.connect(database, isolation_level=None)) as connection:
                self._fixture(connection)
                expected = self._reference_digest(connection)
            before = database.read_bytes()
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                                         isolation_level=None)) as connection:
                connection.row_factory = sqlite3.Row
                for active in (False, True):
                    with self.subTest(active=active):
                        if active:
                            connection.execute("BEGIN")
                        changes = connection.total_changes
                        statements = []
                        connection.set_trace_callback(statements.append)
                        try:
                            self.assertEqual(migration_module._logical_database_digest(connection), expected)
                        finally:
                            connection.set_trace_callback(None)
                        self.assertEqual(connection.in_transaction, active)
                        self.assertEqual(connection.total_changes, changes)
                        self.assertTrue(all(statement.upper().startswith(("SELECT", "PRAGMA"))
                                            for statement in statements))
                        if active:
                            connection.rollback()
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual({path.name for path in Path(directory).iterdir()}, {"source.sqlite3"})


class GenericMigrationGuardTests(unittest.TestCase):
    def test_schema9_names_the_specialized_dot6_successor_upgrader(self) -> None:
        migration9 = load_migrations()[8]
        plan = OfflineMigrationPlan(
            current_schema_version=8,
            target_schema_version=9,
            verified_backup_manifest_sha256="a" * 64,
            applied_history_sha256="b" * 64,
            pending_migrations=((
                migration9.version,
                migration9.name,
                migration9.digest_sha256,
            ),),
            lifecycle=RecoveryLifecycle.OFFLINE,
        )
        backup = BackupSet(
            path=Path("unused-schema9-backup"),
            manifest=SimpleNamespace(),
        )
        with self.assertRaises(MigrationExecutionError) as raised:
            _validate_plan(plan, backup)
        self.assertEqual(
            raised.exception.code,
            "specialized_dot6_successor_upgrader_required",
        )
        self.assertIn("specialized local dot6 successor upgrader", str(raised.exception))


class OfflineMigrationExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-rh-migration-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.owner = Path(self.temporary.name)
        self.paths = WorkspacePaths.from_root(self.owner / "workspace")
        self.addCleanup(
            _remove_tree,
            self.paths.root,
            owned_parent=self.owner,
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
        self.authority = self.snapshot.authority_vector.to_mapping()
        self.store = WorkspaceStore.initialize_direct_mission_workspace(
            self.paths,
            project_id="riemann_hypothesis",
            canonical_snapshot=self.snapshot,
            actor="test.migration.fixture",
        )
        self.paths = self.store.paths
        self.cas = EvidenceCAS(self.paths)

    def _create_current_backup(self):
        project_id = self.store.project_id
        mission_id = "mission.migration"
        epoch_id = "epoch.migration.fixture"
        metadata = self.store.read_metadata()
        authority_digest = str(metadata["canonical_authority_digest"])
        lease = self.store.claim_writer(
            owner="migration-fixture-writer",
            creation_basis="persist direct migration fixture",
            expected_project_commit=int(metadata["current_project_commit"]),
            expected_root_digest=str(metadata["current_root_digest"]),
            expected_canonical_authority_digest=authority_digest,
        )
        self.store.initialize_direct_mission_genesis(
            _direct_genesis_fixture(
                project_id=project_id,
                mission_id=mission_id,
                branch_id="branch.migration",
                strategy_id="strategy.migration",
            ),
            mission_id=mission_id,
            canonical_snapshot=self.snapshot,
            lease=lease,
            command_id="migration.direct-genesis",
            actor="test.migration.fixture",
        )
        mission = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, mission_id)
        )
        self.assertIsNotNone(mission)
        assert mission is not None
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
            command_id="migration.epoch.authorize",
            actor="test.migration.fixture",
            expected_canonical_authority_digest=authority_digest,
            expected_mission_host_store_cut={
                "project_commit": int(
                    authorization_metadata["current_project_commit"]
                ),
                "current_root_digest": str(
                    authorization_metadata["current_root_digest"]
                ),
                "transition_head_digest": authorization_metadata[
                    "transition_head_digest"
                ],
                "canonical_authority_digest": str(
                    authorization_metadata["canonical_authority_digest"]
                ),
            },
        )
        bound = prepare_direct_executive_epoch_bound_event(
            executive_epoch_id=epoch_id,
            mission_id=mission_id,
            goal_thread_id="thread:migration-fixture",
            workspace_root=str(self.paths.root),
        )
        self.store.bind_executive_epoch(
            mission_id=mission_id,
            executive_epoch_id=epoch_id,
            event=bound,
            expected_event_ordinal=1,
            expected_event_digest=authorized.digest_sha256,
            lease=lease,
            command_id="migration.epoch.bind",
            actor="test.migration.fixture",
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
        candidate_id = "candidate.migration"
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
                "basis": "migration fixture preserving one exact A1 closure",
            },
            "provenance": {
                "kind": "native_executive_epoch",
                "executive_epoch_id": epoch_id,
                "executive_epoch_authority_sha256": authority.authority_sha256,
            },
            "authority_class": "candidate_only",
            "canonical_effect": "none",
        }
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
            command_id="migration.candidate.a1",
            actor="test.migration.fixture",
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
        self._legacy_relation_evidence = (
            binding.evidence_id,
            binding.evidence_revision,
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
        return create_bound_backup(
            store=self.store,
            cas=self.cas,
            backup_id="migration-source-current",
            authority_repo_root=REPO_ROOT,
            closure_id=closure.manifest.closure_id,
        )

    def _downgrade_current_backup(self, target_schema_version: int):
        if target_schema_version not in {1, 2}:
            raise ValueError("test downgrade target must be schema v1 or v2")
        backup = self._create_current_backup()
        if os.name == "nt":
            from research_core.workspace_recovery import _remove_windows_write_deny

            _remove_windows_write_deny(backup.path)
        for directory in (
            backup.path,
            *(item for item in backup.path.rglob("*") if item.is_dir()),
        ):
            os.chmod(
                directory,
                stat.S_IMODE(directory.stat().st_mode) | stat.S_IWUSR,
            )
        database = backup.path / "workspace.sqlite3"
        manifest_path = backup.path / "manifest.json"
        os.chmod(database, stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600)
        os.chmod(
            manifest_path, stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600
        )

        connection = sqlite3.connect(database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            metadata_values = dict(
                connection.execute(
                    "SELECT singleton, project_id, application_version, "
                    "schema_version, root_identity, lifecycle, "
                    "canonical_authority_json, canonical_authority_digest, "
                    "current_writer_epoch, current_project_commit, "
                    "current_root_digest, transition_head_digest, created_at, "
                    "updated_at FROM workspace_metadata WHERE singleton = 1"
                ).fetchone()
            )
            metadata_ddl = next(
                statement
                for statement in _iter_statements(load_migrations()[0].sql)
                if statement.startswith("CREATE TABLE workspace_metadata ")
            )
            evidence_id, evidence_revision = self._legacy_relation_evidence
            connection.execute(
                "INSERT INTO evidence_relation (relation_id, source_evidence_id, "
                "source_revision, target_evidence_id, target_revision, relation_kind, "
                "scope_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "relation.migration.legacy-review",
                    evidence_id,
                    evidence_revision,
                    evidence_id,
                    evidence_revision,
                    "review",
                    canonical_json_bytes({"scope": "existing v2 relation"}).decode(
                        "utf-8"
                    ),
                ),
            )
            relation_rows = tuple(
                dict(row)
                for row in connection.execute(
                    "SELECT relation_id, source_evidence_id, source_revision, "
                    "target_evidence_id, target_revision, relation_kind, scope_json "
                    "FROM evidence_relation ORDER BY relation_id"
                )
            )
            relation_ddl = next(
                statement
                for statement in _iter_statements(load_migrations()[0].sql)
                if statement.startswith("CREATE TABLE evidence_relation ")
            )
            blob_columns = (
                "sha256",
                "byte_length",
                "media_type",
                "encoding",
                "integrity_state",
                "availability_state",
                "logical_cas_path",
                "first_verified_at",
                "last_verified_at",
                "quarantine_state",
            )
            blob_rows = tuple(
                tuple(row[column] for column in blob_columns)
                for row in connection.execute(
                    "SELECT " + ", ".join(blob_columns) + " FROM blob ORDER BY sha256"
                )
            )
            blob_ddl = next(
                statement
                for statement in _iter_statements(load_migrations()[0].sql)
                if statement.startswith("CREATE TABLE blob ")
            )
            transition_rows = tuple(
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM transition_journal ORDER BY sequence_no"
                )
            )
            transition_ddl = next(
                statement
                for statement in _iter_statements(load_migrations()[0].sql)
                if statement.startswith("CREATE TABLE transition_journal ")
            )
            transition_columns = (
                "sequence_no",
                "project_id",
                "project_commit_no",
                "command_id",
                "command_kind",
                "request_digest",
                "actor",
                "writer_epoch",
                "changed_heads_json",
                "auxiliary_writes_json",
                "auxiliary_writes_digest",
                "evidence_head_advances_json",
                "evidence_head_advances_digest",
                "authorization_json",
                "canonical_effect",
                "predecessor_digest",
                "digest_sha256",
                "created_at",
            )
            baseline_table_ddl = {
                table_name: next(
                    statement
                    for statement in _iter_statements(load_migrations()[0].sql)
                    if statement.startswith(f"CREATE TABLE {table_name} ")
                )
                for table_name in (
                    "session_settlement",
                    "outbox_intent",
                    "inbox_receipt",
                    "attempt_reference",
                )
            }
            historical_admission_table_ddl = {
                table_name: next(
                    statement
                    for statement in _iter_statements(load_migrations()[0].sql)
                    if statement.startswith(f"CREATE TABLE {table_name} ")
                )
                for table_name in (
                    "admission_case_preparation",
                    "admission_objection",
                    "admission_review_binding",
                    "admission_decision_binding",
                )
            }
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("DROP TABLE blob")
            connection.execute(blob_ddl)
            connection.executemany(
                "INSERT INTO blob ("
                + ", ".join(blob_columns)
                + ") VALUES ("
                + ", ".join("?" for _ in blob_columns)
                + ")",
                blob_rows,
            )
            for table_name in (
                "continuation_checkpoint",
                "executive_epoch_event",
                "capture_scope_annotation_head",
                "evidence_capture_source",
                "capture_scope_annotation_revision",
                "raw_capture_artifact",
                "raw_capture",
            ):
                connection.execute(f"DROP TABLE {table_name}")
            connection.execute("DROP TABLE transition_journal")
            connection.execute(transition_ddl)
            # This synthetic fixture rebuilds the exact schema1 journal shape.
            # Root6's typed-row binding is absent from that shape, so recreate
            # the legacy chain and its commit/result links rather than copying
            # digest bodies which still authenticate the removed field.
            predecessor_digest = None
            for row in transition_rows:
                row["predecessor_digest"] = predecessor_digest
                legacy_body = {
                    "sequence_no": row["sequence_no"],
                    "project_id": row["project_id"],
                    "project_commit_no": row["project_commit_no"],
                    "command_id": row["command_id"],
                    "command_kind": row["command_kind"],
                    "request_digest": row["request_digest"],
                    "actor": row["actor"],
                    "writer_epoch": row["writer_epoch"],
                    "changed_heads": json.loads(row["changed_heads_json"]),
                    "auxiliary_writes_digest": row["auxiliary_writes_digest"],
                    "evidence_head_advances_digest": row["evidence_head_advances_digest"],
                    "authorization_digest": digest(
                        canonical_json_bytes(json.loads(row["authorization_json"]))
                    ),
                    "canonical_effect": row["canonical_effect"],
                    "predecessor_digest": predecessor_digest,
                    "created_at": row["created_at"],
                }
                row["digest_sha256"] = digest(canonical_json_bytes(legacy_body))
                predecessor_digest = row["digest_sha256"]
                connection.execute(
                    "UPDATE project_commit SET transition_head_digest=? WHERE commit_no=?",
                    (row["digest_sha256"], row["project_commit_no"]),
                )
                result_row = connection.execute(
                    "SELECT result_json FROM command_result WHERE command_id=?",
                    (row["command_id"],),
                ).fetchone()
                result = json.loads(result_row["result_json"])
                result["transition_digest"] = row["digest_sha256"]
                result_raw = canonical_json_bytes(result)
                connection.execute(
                    "UPDATE command_result SET result_json=?, result_digest=? WHERE command_id=?",
                    (result_raw.decode("utf-8"), digest(result_raw), row["command_id"]),
                )
            metadata_values["transition_head_digest"] = predecessor_digest
            connection.executemany(
                "INSERT INTO transition_journal ("
                + ", ".join(transition_columns)
                + ") VALUES ("
                + ", ".join("?" for _ in transition_columns)
                + ")",
                tuple(
                    tuple(row[column] for column in transition_columns)
                    for row in transition_rows
                ),
            )
            # Remove only migration0010's named indexes. Some disappeared with
            # rebuilt tables; surviving legacy owner tables retain their rows.
            schema10_indexes = tuple(
                (
                    line.split()[3]
                    if line.startswith("CREATE UNIQUE INDEX ")
                    else line.split()[2]
                )
                for line in load_migrations()[9].sql.splitlines()
                if line.startswith(("CREATE INDEX ", "CREATE UNIQUE INDEX "))
            )
            for index_name in schema10_indexes:
                connection.execute(f"DROP INDEX IF EXISTS {index_name}")
            connection.execute(
                "ALTER TABLE evidence_item_revision "
                "DROP COLUMN project_commit_no"
            )
            connection.execute("DROP TABLE admission_canonical_transaction")
            connection.execute("DROP TABLE admission_decision")
            connection.execute("DROP TABLE admission_review")
            connection.execute("DROP TABLE admission_case")
            for table_name in (
                "admission_case_preparation",
                "admission_objection",
                "admission_review_binding",
                "admission_decision_binding",
            ):
                connection.execute(historical_admission_table_ddl[table_name])
            for table_name in (
                "current_dependency_node",
                "current_dependency_projection",
                "capture_artifact_coverage_node",
                "root_hook_index_node",
                "workspace_root_transition_retained_head",
                "workspace_root_contract_transition",
                "prepared_attempt_receipt",
                "session_attempt_disposition_transition",
                "attempt_evidence_binding",
                "session_attempt_binding",
                "attempt_reference",
                "session_attempt_allocation",
            ):
                connection.execute(f"DROP TABLE {table_name}")
            connection.execute("DROP TABLE session_settlement")
            connection.execute("DROP TABLE outbox_intent")
            connection.execute("DROP TABLE inbox_receipt")
            for table_name in (
                "session_settlement",
                "outbox_intent",
                "inbox_receipt",
                "attempt_reference",
            ):
                connection.execute(baseline_table_ddl[table_name])
            connection.execute("DROP TABLE card11_campaign_observation")
            connection.execute("DROP TABLE card11_activation_binding")
            connection.execute("DROP TABLE card11_activation_record")
            connection.execute("DROP TABLE card11_capability_consumption")
            connection.execute("DROP TABLE workspace_metadata")
            connection.execute(metadata_ddl)
            metadata_columns = tuple(metadata_values)
            connection.execute(
                "INSERT INTO workspace_metadata ("
                + ", ".join(metadata_columns)
                + ") VALUES ("
                + ", ".join("?" for _ in metadata_columns)
                + ")",
                tuple(metadata_values[column] for column in metadata_columns),
            )
            connection.execute("DROP TABLE evidence_relation")
            connection.execute(relation_ddl)
            relation_columns = (
                "relation_id",
                "source_evidence_id",
                "source_revision",
                "target_evidence_id",
                "target_revision",
                "relation_kind",
                "scope_json",
            )
            connection.executemany(
                "INSERT INTO evidence_relation ("
                + ", ".join(relation_columns)
                + ") VALUES ("
                + ", ".join("?" for _ in relation_columns)
                + ")",
                tuple(
                    tuple(row[column] for column in relation_columns)
                    for row in relation_rows
                ),
            )
            if target_schema_version == 1:
                connection.execute("DROP TABLE alert_acknowledgement")
                connection.execute("DROP TABLE migration_execution")
            else:
                connection.execute("DROP INDEX migration_execution_exact_attempt")
            connection.execute(
                "DELETE FROM schema_migration WHERE version > ?",
                (target_schema_version,),
            )
            connection.execute(
                "UPDATE workspace_metadata "
                "SET schema_version = ?, application_version = ? WHERE singleton = 1",
                (target_schema_version, "research-workspace-inactive-v1"),
            )
            connection.execute(f"PRAGMA user_version = {target_schema_version}")
            connection.execute("COMMIT")
            connection.execute("PRAGMA foreign_keys = ON")
            object_sha256 = schema_object_digest(connection)
            self.assertEqual(
                object_sha256,
                _expected_schema_object_digest_for_version(target_schema_version),
            )
            metadata = connection.execute(
                "SELECT * FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            revision_count = sum(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {kind}_revision"
                    ).fetchone()[0]
                )
                for kind in (
                    "mission",
                    "branch",
                    "strategy",
                    "context",
                    "session",
                    "candidate",
                )
            )
            command_count = int(
                connection.execute("SELECT COUNT(*) FROM command_result").fetchone()[0]
            )
            journal_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transition_journal"
                ).fetchone()[0]
            )
        finally:
            connection.close()

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        history = payload["store"]["migration_history"][:target_schema_version]
        payload["store"]["application_version"] = "research-workspace-inactive-v1"
        payload["store"]["schema_version"] = target_schema_version
        payload["store"]["operating_mode"] = "inactive_foundation"
        payload["store"]["root_digest_version"] = 1
        payload["store"]["transition_head"] = metadata["transition_head_digest"]
        payload["store"]["migration_history"] = history
        payload["store"]["migration_history_sha256"] = digest(
            canonical_json_bytes(history)
        )
        payload["store"]["migration_digest"] = digest(
            canonical_json_bytes(
                [
                    {
                        "version": row["version"],
                        "name": row["name"],
                        "sha256": row["digest_sha256"],
                    }
                    for row in history
                ]
            )
        )
        payload["store"]["schema_object_digest"] = object_sha256
        integrity_payload = {
            "project_id": str(metadata["project_id"]),
            "schema_version": target_schema_version,
            "current_writer_epoch": metadata["current_writer_epoch"],
            "current_project_commit": int(metadata["current_project_commit"]),
            "current_root_digest": str(metadata["current_root_digest"]),
            "transition_head_digest": metadata["transition_head_digest"],
            "revision_count": revision_count,
            "command_count": command_count,
            "journal_count": journal_count,
        }
        payload["store"]["integrity_report_sha256"] = digest(
            canonical_json_bytes(integrity_payload)
        )

        persisted_evidence = _load_persisted_backup_evidence(
            database,
            closure_id=backup.manifest.closure.closure_id,
            cas=EvidenceCAS(WorkspacePaths.from_root(backup.path)),
            cas_inventory_contract=str(payload["cas_inventory_contract"]),
        )
        payload["evidence_metadata_sha256"] = persisted_evidence.metadata_sha256

        sqlite_raw = database.read_bytes()
        payload["sqlite_sha256"] = digest(sqlite_raw)
        payload["sqlite_length"] = len(sqlite_raw)
        fence_raw = canonical_json_bytes(
            {
                "deletion_directives": payload["deletion_directives"],
                "tombstones": payload["tombstones"],
                "effect": "retrieval_fence_only",
                "automatic_byte_deletion": False,
                "canonical_effect": "none",
            }
        )
        restore_entries = [
            {
                "path": "workspace.sqlite3",
                "sha256": payload["sqlite_sha256"],
                "length": payload["sqlite_length"],
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
        restored_root_sha256 = digest(
            canonical_json_bytes(sorted(restore_entries, key=lambda item: item["path"]))
        )
        restore = payload["restore_test"]
        restore["restored_root_sha256"] = restored_root_sha256
        restore["deletion_fence_sha256"] = digest(fence_raw)
        restore["read_only_seal_sha256"] = digest(
            canonical_json_bytes(
                {
                    "format": "research-workspace-read-only-v1",
                    "lifecycle": RecoveryLifecycle.VERIFIED_READ_ONLY.value,
                    "protected_files": sorted(
                        [
                            "workspace.sqlite3",
                            "deletion_fence.json",
                            *(
                                item["logical_path"]
                                for item in payload["evidence_inventory"]
                            ),
                        ]
                    ),
                    "protected_root_sha256": restored_root_sha256,
                    "writable": False,
                    "mission_auto_resume": False,
                    "canonical_effect": "none",
                }
            )
        )
        restore_body = {
            key: value for key, value in restore.items() if key != "receipt_sha256"
        }
        restore["receipt_sha256"] = digest(canonical_json_bytes(restore_body))
        manifest_body = {
            key: value for key, value in payload.items() if key != "manifest_sha256"
        }
        payload["manifest_sha256"] = digest(canonical_json_bytes(manifest_body))
        manifest_path.write_bytes(canonical_json_bytes(payload))
        _protect_closed_tree(backup.path)
        return verify_backup_set(backup.path)

    def _create_v2_backup(self):
        return self._downgrade_current_backup(2)

    def _downgrade_backup_to_v1(self):
        return self._downgrade_current_backup(1)

    def _plan(self, backup):
        return prepare_offline_migration(
            backup=backup,
            target_schema_version=3,
            lifecycle=RecoveryLifecycle.OFFLINE,
        )

    def _crash(
        self,
        *,
        backup,
        migration_id: str,
        crash_point: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment[_CRASH_WORKER_ENV] = json.dumps(
            {
                "backup_path": str(backup.path),
                "source_commit": backup.manifest.canonical.source_commit,
                "migration_id": migration_id,
                "crash_point": crash_point,
            }
        )
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            cwd=PACKAGE_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            86,
            f"crash worker did not stop at {crash_point}: "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        return completed

    def _artifact(self, migration_id: str) -> Path:
        return self.paths.recovery / f"migration-{migration_id}"

    def _assert_no_effect_state(self, artifact: Path) -> dict:
        state = json.loads(
            (artifact / "migration_state.json").read_text(encoding="utf-8")
        )
        self.assertFalse(state["selected_live_root"])
        self.assertFalse(state["mission_auto_resume"])
        self.assertEqual(state["canonical_effect"], "none")
        return state

    def test_v6_transition_journal_upgrade_is_atomic_and_closed(self) -> None:
        with closing(sqlite3.connect(self.paths.database)) as fresh:
            fresh.execute("PRAGMA foreign_keys = ON")
            fresh_ddl = str(
                fresh.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='transition_journal'"
                ).fetchone()[0]
            )
            self.assertIn("'none', 'exact_authorized_delta'", fresh_ddl)
            self.assertEqual(tuple(fresh.execute("PRAGMA foreign_key_check")), ())

        database = self.owner / "transition-journal-v5.sqlite3"
        migrations = load_migrations()
        self.assertEqual(
            tuple(item.version for item in migrations),
            (1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        )
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN EXCLUSIVE")
            try:
                for migration in migrations[:5]:
                    for statement in _iter_statements(migration.sql):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migration("
                        "version, name, digest_sha256, applied_at"
                        ") VALUES (?, ?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            migration.digest_sha256,
                            "2026-08-09T12:00:00Z",
                        ),
                    )
                connection.execute(
                    "INSERT INTO workspace_metadata("
                    "singleton, project_id, application_version, schema_version, "
                    "root_identity, lifecycle, canonical_authority_json, "
                    "canonical_authority_digest, current_writer_epoch, "
                    "current_project_commit, current_root_digest, "
                    "transition_head_digest, created_at, updated_at, "
                    "operating_mode, root_digest_version"
                    ") VALUES (1, ?, ?, 5, ?, 'offline', '{}', ?, 1, 1, ?, ?, "
                    "?, ?, 'inactive_foundation', 2)",
                    (
                        "riemann_hypothesis",
                        "research-workspace-inactive-v1",
                        "1" * 64,
                        "2" * 64,
                        "3" * 64,
                        "4" * 64,
                        "2026-08-09T12:00:00Z",
                        "2026-08-09T12:00:00Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO writer_epoch("
                    "epoch, project_id, owner, lifecycle, creation_basis, "
                    "predecessor_epoch, takeover_evidence_json, created_at, "
                    "closed_at, row_digest"
                    ") VALUES (1, ?, 'test.migration', 'quiesced', "
                    "'v5 transition journal fixture', NULL, NULL, ?, ?, ?)",
                    (
                        "riemann_hypothesis",
                        "2026-08-09T12:00:00Z",
                        "2026-08-09T12:00:00Z",
                        "5" * 64,
                    ),
                )
                connection.execute(
                    "INSERT INTO transition_journal("
                    "sequence_no, project_id, project_commit_no, command_id, "
                    "command_kind, request_digest, actor, writer_epoch, "
                    "changed_heads_json, auxiliary_writes_json, "
                    "auxiliary_writes_digest, evidence_head_advances_json, "
                    "evidence_head_advances_digest, authorization_json, "
                    "canonical_effect, predecessor_digest, digest_sha256, created_at"
                    ") VALUES (1, ?, 1, 'command.v5', 'fixture', ?, "
                    "'test.migration', 1, '[]', '[]', ?, '[]', ?, '{}', "
                    "'none', NULL, ?, ?)",
                    (
                        "riemann_hypothesis",
                        "6" * 64,
                        "7" * 64,
                        "8" * 64,
                        "9" * 64,
                        "2026-08-09T12:00:00Z",
                    ),
                )
                connection.execute("PRAGMA user_version = 5")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")

            journal_columns = tuple(
                str(row[1]) for row in connection.execute("PRAGMA table_info(transition_journal)")
            )
            source_row = tuple(
                connection.execute(
                    "SELECT " + ", ".join(journal_columns) + " FROM transition_journal"
                ).fetchone()
            )
            source_ddl = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='transition_journal'"
                ).fetchone()[0]
            )
            self.assertNotIn("exact_authorized_delta", source_ddl)
            self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())

            migration = migrations[5]
            pending = ((migration.version, migration.name, migration.digest_sha256),)
            execution = _OfflineMigrationExecution(
                execution_id="migration.transition-journal-v6",
                attempt_id="migration.transition-journal-v6.attempt.0001",
                source_schema_version=5,
                target_schema_version=6,
                verified_backup_manifest_sha256="a" * 64,
                applied_history_sha256="b" * 64,
                plan_sha256="c" * 64,
                started_at="2026-08-09T12:01:00Z",
                completed_at="2026-08-09T12:02:00Z",
            )
            capability = _offline_migration_capability(
                database,
                root_identity="1" * 64,
            )
            statements = tuple(_iter_statements(migration.sql))
            fault_index = next(
                index
                for index, statement in enumerate(statements, start=1)
                if statement == "DROP TABLE transition_journal;"
            )

            def fail_after_old_journal_drop(point, details):
                if (
                    point == "after_migration_statement"
                    and details["version"] == 6
                    and details["statement_index"] == fault_index
                ):
                    raise RuntimeError("injected after incumbent journal drop")

            with self.assertRaisesRegex(
                RuntimeError,
                "injected after incumbent journal drop",
            ):
                _apply_offline_migration_plan(
                    connection,
                    pending_migrations=pending,
                    execution=execution,
                    capability=capability,
                    fault_hook=fail_after_old_journal_drop,
                )

            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migration ORDER BY version"
                    )
                ),
                (1, 2, 3, 4, 5),
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT " + ", ".join(journal_columns) + " FROM transition_journal"
                    ).fetchone()
                ),
                source_row,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='transition_journal_v6'"
                ).fetchone()
            )
            self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())

            _apply_offline_migration_plan(
                connection,
                pending_migrations=pending,
                execution=execution,
                capability=capability,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(
                tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migration ORDER BY version"
                    )
                ),
                (1, 2, 3, 4, 5, 6),
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT " + ", ".join(journal_columns) + " FROM transition_journal"
                    ).fetchone()
                ),
                source_row,
            )
            target_ddl = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='transition_journal'"
                ).fetchone()[0]
            )
            self.assertIn("'none', 'exact_authorized_delta'", target_ddl)
            self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())

            insert_sql = (
                "INSERT INTO transition_journal("
                + ", ".join(journal_columns)
                + ") VALUES ("
                + ", ".join("?" for _ in journal_columns)
                + ")"
            )
            exact_delta_row = dict(zip(journal_columns, source_row, strict=True))
            exact_delta_row.update(
                {
                    "sequence_no": 2,
                    "project_commit_no": 2,
                    "command_id": "command.v6.exact-delta",
                    "request_digest": "d" * 64,
                    "canonical_effect": "exact_authorized_delta",
                    "predecessor_digest": "9" * 64,
                    "digest_sha256": "e" * 64,
                }
            )
            connection.execute(
                insert_sql,
                tuple(exact_delta_row[column] for column in journal_columns),
            )
            invalid_row = dict(exact_delta_row)
            invalid_row.update(
                {
                    "sequence_no": 3,
                    "project_commit_no": 3,
                    "command_id": "command.v6.invalid-effect",
                    "request_digest": "f" * 64,
                    "canonical_effect": "arbitrary_effect",
                    "digest_sha256": "0" * 64,
                }
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    insert_sql,
                    tuple(invalid_row[column] for column in journal_columns),
                )
            self.assertEqual(
                tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT canonical_effect FROM transition_journal "
                        "ORDER BY sequence_no"
                    )
                ),
                ("none", "exact_authorized_delta"),
            )
            self.assertEqual(tuple(connection.execute("PRAGMA foreign_key_check")), ())

    def test_retained_v4_schema_applies_the_exact_historical_target_six_suffix(
        self,
    ) -> None:
        database = self.owner / "retained-v4.sqlite3"
        migrations = load_migrations()
        self.assertEqual(
            tuple(item.version for item in migrations),
            (1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        )
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN EXCLUSIVE")
            try:
                for migration in migrations[:4]:
                    for statement in _iter_statements(migration.sql):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migration("
                        "version, name, digest_sha256, applied_at"
                        ") VALUES (?, ?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            migration.digest_sha256,
                            "2026-08-09T12:00:00Z",
                        ),
                    )
                connection.execute(
                    "INSERT INTO workspace_metadata("
                    "singleton, project_id, application_version, schema_version, "
                    "root_identity, lifecycle, canonical_authority_json, "
                    "canonical_authority_digest, current_writer_epoch, "
                    "current_project_commit, current_root_digest, "
                    "transition_head_digest, created_at, updated_at, "
                    "operating_mode, root_digest_version"
                    ") VALUES (1, ?, ?, 4, ?, 'offline', ?, ?, NULL, 0, ?, NULL, "
                    "?, ?, 'inactive_foundation', 2)",
                    (
                        "riemann_hypothesis",
                        "research-workspace-inactive-v1",
                        "1" * 64,
                        canonical_json_bytes(
                            {
                                "source_class": "retained_v4_fixture",
                                "canonical_state_sha256": "2" * 64,
                                "canonical_schema_version": 2,
                            }
                        ).decode("utf-8"),
                        "3" * 64,
                        "4" * 64,
                        "2026-08-09T12:00:00Z",
                        "2026-08-09T12:00:00Z",
                    ),
                )
                connection.execute("PRAGMA user_version = 4")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

            retained_card11_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'card11_activation_record'"
                ).fetchone()[0]
            )
            connection.execute("BEGIN EXCLUSIVE")
            try:
                for migration in migrations[4:6]:
                    for statement in _iter_statements(migration.sql):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migration("
                        "version, name, digest_sha256, applied_at"
                        ") VALUES (?, ?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            migration.digest_sha256,
                            "2026-08-09T12:01:00Z",
                        ),
                    )
                connection.execute(
                    "UPDATE workspace_metadata SET schema_version = 6, "
                    "application_version = ? WHERE singleton = 1",
                    (APPLICATION_VERSION,),
                )
                connection.execute("PRAGMA user_version = 6")
                violations = tuple(connection.execute("PRAGMA foreign_key_check"))
                self.assertEqual(violations, ())
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")

            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(
                tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migration ORDER BY version"
                    )
                ),
                (1, 2, 3, 4, 5, 6),
            )
            metadata = connection.execute(
                "SELECT project_id, schema_version, application_version, "
                "operating_mode, root_digest_version FROM workspace_metadata "
                "WHERE singleton = 1"
            ).fetchone()
            self.assertEqual(
                tuple(metadata),
                (
                    "riemann_hypothesis",
                    6,
                    APPLICATION_VERSION,
                    "inactive_foundation",
                    2,
                ),
            )
            current_card11_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'card11_activation_record'"
                ).fetchone()[0]
            )
            self.assertEqual(current_card11_sql, retained_card11_sql)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {
                    "session_attempt_binding",
                    "attempt_evidence_binding",
                    "admission_case",
                    "admission_review",
                    "admission_decision",
                    "admission_canonical_transaction",
                }.issubset(tables)
            )
            self.assertTrue(
                {
                    "admission_case_preparation",
                    "admission_objection",
                    "admission_review_binding",
                    "admission_decision_binding",
                }.isdisjoint(tables)
            )

    def test_v2_to_v3_preserves_existing_relations_and_expands_only_the_closed_kind(self) -> None:
        backup = self._create_v2_backup()
        source_before = (backup.path / "workspace.sqlite3").read_bytes()
        self.assertEqual(backup.manifest.store.schema_version, 2)
        query = (
            "SELECT relation_id, source_evidence_id, source_revision, "
            "target_evidence_id, target_revision, relation_kind, scope_json "
            "FROM evidence_relation ORDER BY relation_id"
        )
        with closing(sqlite3.connect(backup.path / "workspace.sqlite3")) as connection:
            before_rows = tuple(connection.execute(query))
            before_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='evidence_relation'"
                ).fetchone()[0]
            )
        self.assertEqual(len(before_rows), 1)
        self.assertNotIn("resolution_requirement", before_sql)

        plan = prepare_offline_migration(
            backup=backup,
            target_schema_version=3,
            lifecycle=RecoveryLifecycle.OFFLINE,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=backup,
            migration_id="v2-to-v3-resolution-requirement",
            actor="test.migration",
        )

        self.assertEqual(result.target_schema_version, 3)
        self.assertEqual(result.migration_history_count, 3)
        self.assertFalse(result.selected_live_root)
        self.assertFalse(result.mission_auto_resume)
        self.assertEqual(result.canonical_effect, "none")
        with closing(sqlite3.connect(result.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            after_rows = tuple(connection.execute(query))
            after_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='evidence_relation'"
                ).fetchone()[0]
            )
            metadata = connection.execute(
                "SELECT schema_version, application_version "
                "FROM workspace_metadata WHERE singleton=1"
            ).fetchone()
        self.assertEqual(after_rows, before_rows)
        self.assertIn("resolution_requirement", after_sql)
        self.assertEqual(tuple(metadata), (3, APPLICATION_VERSION))
        self.assertEqual((backup.path / "workspace.sqlite3").read_bytes(), source_before)
        self._assert_no_effect_state(result.artifact)

    def test_v1_to_v2_remains_a_valid_explicit_historical_target(self) -> None:
        backup = self._downgrade_backup_to_v1()
        source_before = (backup.path / "workspace.sqlite3").read_bytes()
        plan = prepare_offline_migration(
            backup=backup,
            target_schema_version=2,
            lifecycle=RecoveryLifecycle.OFFLINE,
        )

        with (
            patch.object(
                migration_module, "_verify_target_schema",
                wraps=migration_module._verify_target_schema,
            ) as verify_target,
            patch(
                "research_core.workspace_recovery._verify_backup_for_trust_transition",
                side_effect=AssertionError("executor rebuilt unchanged source semantics"),
            ),
        ):
            result = execute_offline_migration(
                plan=plan,
                backup=backup,
                migration_id="v1-to-v2-historical-target",
                actor="test.migration",
            )
        self.assertEqual(verify_target.call_count, 1)

        self.assertEqual(result.target_schema_version, 2)
        self.assertEqual(result.migration_history_count, 2)
        with closing(sqlite3.connect(result.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0],
                2,
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='migration_execution'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='card11_activation_record'"
                ).fetchone()
            )
        self.assertEqual((backup.path / "workspace.sqlite3").read_bytes(), source_before)
        self._assert_no_effect_state(result.artifact)

    def test_fault_rolls_back_exact_v1_and_successful_retry_advances_once(self) -> None:
        backup = self._downgrade_backup_to_v1()
        source_before = (backup.path / "workspace.sqlite3").read_bytes()
        plan = prepare_offline_migration(
            backup=backup,
            target_schema_version=3,
            lifecycle=RecoveryLifecycle.OFFLINE,
        )
        fault_seen: list[tuple[str, dict]] = []
        observer = CaptureWorkspaceObserver()

        def fail_after_initial_ddl(point, details):
            fault_seen.append((point, dict(details)))
            if point == "after_migration_statement" and details["statement_index"] == 1:
                raise RuntimeError("injected after initial DDL")

        with self.assertRaises(MigrationExecutionError) as captured:
            execute_offline_migration(
                plan=plan,
                backup=backup,
                migration_id="v1-to-v3",
                actor="test.migration",
                fault_hook=fail_after_initial_ddl,
                observer=observer,
                observation_environment=ObservationEnvironment.CI,
            )
        self.assertEqual(captured.exception.code, "migration_execution_failed")
        self.assertIn(
            "after_migration_statement", tuple(item[0] for item in fault_seen)
        )
        artifact = captured.exception.artifact
        assert artifact is not None
        failed = json.loads(
            (artifact / "attempt-0001.outcome.json").read_text(encoding="utf-8")
        )
        self.assertTrue(failed["rollback_exact"])
        self.assertEqual(failed["pre_logical_sha256"], failed["post_logical_sha256"])
        self.assertEqual(failed["schema_version"], 1)
        with closing(sqlite3.connect(artifact / "workspace.sqlite3")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[
                    0
                ],
                1,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_execution'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM workspace_metadata WHERE singleton=1"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(
            (backup.path / "workspace.sqlite3").read_bytes(), source_before
        )

        result = execute_offline_migration(
            plan=plan,
            backup=backup,
            migration_id="v1-to-v3",
            actor="test.migration",
            observer=observer,
            observation_environment=ObservationEnvironment.CI,
        )
        self.assertEqual(result.attempt_id, "v1-to-v3.attempt.0002")
        self.assertEqual(result.lifecycle, RecoveryLifecycle.VERIFIED_READ_ONLY)
        self.assertFalse(result.selected_live_root)
        self.assertFalse(result.mission_auto_resume)
        self.assertEqual(result.canonical_effect, "none")
        self.assertNotEqual(result.pre_logical_sha256, result.post_logical_sha256)
        self.assertEqual(result.migration_history_count, 3)
        state = json.loads(
            (artifact / "migration_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["attempt_count"], 2)
        self.assertFalse(state["selected_live_root"])
        self.assertEqual(
            sorted(item.name for item in artifact.glob("attempt-*.json")),
            [
                "attempt-0001.outcome.json",
                "attempt-0001.started.json",
                "attempt-0002.outcome.json",
                "attempt-0002.started.json",
            ],
        )
        uri = f"{result.database.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[
                    0
                ],
                3,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM migration_execution"
                ).fetchone()[0],
                1,
            )
            marker = connection.execute("SELECT * FROM migration_execution").fetchone()
            self.assertEqual(marker[1], result.attempt_id)
            self.assertEqual(marker[10], "none")
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM workspace_metadata WHERE singleton=1"
                ).fetchone()[0],
                3,
            )
        replay = execute_offline_migration(
            plan=plan,
            backup=backup,
            migration_id="v1-to-v3",
            actor="test.migration",
        )
        self.assertEqual(replay.to_payload(), result.to_payload())
        replay.verify_issued()
        self.assertEqual(
            (backup.path / "workspace.sqlite3").read_bytes(), source_before
        )
        migration_events = tuple(
            event
            for event in observer.events
            if event.kind is EventKind.MIGRATION_CHANGED
        )
        self.assertEqual(
            tuple(event.attributes["status"] for event in migration_events),
            ("started", "failed", "started", "passed"),
        )
        self.assertTrue(
            all(
                event.environment is ObservationEnvironment.CI
                for event in migration_events
            )
        )

    def test_process_exit_releases_external_lease_for_exact_retry(self) -> None:
        backup = self._downgrade_backup_to_v1()
        source_before = (backup.path / "workspace.sqlite3").read_bytes()
        migration_id = "crash-after-lease"
        self._crash(
            backup=backup,
            migration_id=migration_id,
            crash_point="after_lock_acquired",
        )
        artifact = self._artifact(migration_id)
        self.assertFalse(artifact.exists())
        lock_path = self.paths.recovery / f".migration-{migration_id}.executor.lock"
        self.assertTrue(lock_path.is_file())

        result = execute_offline_migration(
            plan=self._plan(backup),
            backup=backup,
            migration_id=migration_id,
            actor="test.restart",
        )
        self.assertEqual(result.attempt_id, f"{migration_id}.attempt.0001")
        self.assertEqual(self._assert_no_effect_state(artifact)["status"], "passed")
        self.assertEqual(
            (backup.path / "workspace.sqlite3").read_bytes(), source_before
        )

    def test_process_exit_before_atomic_publication_discards_staged_initialization(
        self,
    ) -> None:
        backup = self._downgrade_backup_to_v1()
        migration_id = "crash-before-publication"
        self._crash(
            backup=backup,
            migration_id=migration_id,
            crash_point="after_initial_state_fsynced",
        )
        artifact = self._artifact(migration_id)
        self.assertFalse(artifact.exists())
        self.assertEqual(
            len(tuple(self.paths.recovery.glob(f".stage-migration-{migration_id}-*"))),
            1,
        )

        result = execute_offline_migration(
            plan=self._plan(backup),
            backup=backup,
            migration_id=migration_id,
            actor="test.restart",
        )
        self.assertEqual(result.attempt_id, f"{migration_id}.attempt.0001")
        self.assertFalse(
            tuple(self.paths.recovery.glob(f".stage-migration-{migration_id}-*"))
        )
        self.assertEqual(self._assert_no_effect_state(artifact)["status"], "passed")

    def test_process_exit_before_commit_retries_only_after_exact_source_reconciliation(
        self,
    ) -> None:
        backup = self._downgrade_backup_to_v1()
        migration_id = "crash-before-commit"
        self._crash(
            backup=backup,
            migration_id=migration_id,
            crash_point="before_migration_commit",
        )
        artifact = self._artifact(migration_id)
        self.assertTrue((artifact / "attempt-0001.started.json").is_file())
        self.assertFalse((artifact / "attempt-0001.outcome.json").exists())

        result = execute_offline_migration(
            plan=self._plan(backup),
            backup=backup,
            migration_id=migration_id,
            actor="test.restart",
        )
        self.assertEqual(result.attempt_id, f"{migration_id}.attempt.0002")
        interrupted = json.loads(
            (artifact / "attempt-0001.outcome.json").read_text(encoding="utf-8")
        )
        self.assertEqual(interrupted["status"], "interrupted_before_commit")
        self.assertTrue(interrupted["rollback_exact"])
        self.assertEqual(
            interrupted["pre_logical_sha256"], interrupted["post_logical_sha256"]
        )
        state = self._assert_no_effect_state(artifact)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["attempt_count"], 2)

    def test_process_exit_after_commit_forward_finalizes_without_rerunning_sql(
        self,
    ) -> None:
        backup = self._downgrade_backup_to_v1()
        migration_id = "crash-after-commit"
        self._crash(
            backup=backup,
            migration_id=migration_id,
            crash_point="after_migration_commit",
        )
        artifact = self._artifact(migration_id)
        self.assertFalse((artifact / "attempt-0001.outcome.json").exists())

        result = execute_offline_migration(
            plan=self._plan(backup),
            backup=backup,
            migration_id=migration_id,
            actor="test.restart",
        )
        self.assertEqual(result.attempt_id, f"{migration_id}.attempt.0001")
        self.assertEqual(self._assert_no_effect_state(artifact)["attempt_count"], 1)
        with closing(sqlite3.connect(result.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[
                    0
                ],
                3,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM migration_execution"
                ).fetchone()[0],
                1,
            )

    def test_passed_state_before_tree_protection_reissues_same_attempt(self) -> None:
        backup = self._downgrade_backup_to_v1()
        plan = self._plan(backup)
        migration_id = "passed-before-protect"

        def fail_after_passed_state(point, details):
            if point == "after_migration_passed_state":
                raise RuntimeError(f"injected after {details['attempt_id']}")

        with self.assertRaises(MigrationExecutionError) as captured:
            execute_offline_migration(
                plan=plan,
                backup=backup,
                migration_id=migration_id,
                actor="test.pass-before-protect",
                fault_hook=fail_after_passed_state,
            )
        self.assertEqual(
            captured.exception.code,
            "migration_result_finalization_interrupted",
        )
        artifact = self._artifact(migration_id)
        state_before = json.loads(
            (artifact / "migration_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state_before["status"], "passed")
        self.assertEqual(state_before["attempt_count"], 1)
        self.assertEqual(
            state_before["successful_attempt_id"],
            f"{migration_id}.attempt.0001",
        )

        with patch.object(
            migration_module, "_verify_target_schema",
            wraps=migration_module._verify_target_schema,
        ) as verify_target:
            result = execute_offline_migration(
                plan=plan,
                backup=backup,
                migration_id=migration_id,
                actor="test.pass-before-protect.reissue",
            )
        self.assertEqual(verify_target.call_count, 1)

        self.assertEqual(result.attempt_id, f"{migration_id}.attempt.0001")
        self.assertEqual(
            tuple(path.name for path in artifact.glob("attempt-*.started.json")),
            ("attempt-0001.started.json",),
        )
        self.assertEqual(
            json.loads(
                (artifact / "migration_state.json").read_text(encoding="utf-8")
            )["attempt_count"],
            1,
        )
        for path in (artifact, *artifact.rglob("*")):
            self.assertFalse(stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR)

    def test_process_exit_after_outcome_repairs_state_from_commit_witness(self) -> None:
        backup = self._downgrade_backup_to_v1()
        migration_id = "crash-after-outcome"
        self._crash(
            backup=backup,
            migration_id=migration_id,
            crash_point="after_migration_outcome",
        )
        artifact = self._artifact(migration_id)
        self.assertEqual(
            json.loads(
                (artifact / "attempt-0001.outcome.json").read_text(encoding="utf-8")
            )["status"],
            "passed",
        )
        self.assertEqual(self._assert_no_effect_state(artifact)["status"], "running")

        result = execute_offline_migration(
            plan=self._plan(backup),
            backup=backup,
            migration_id=migration_id,
            actor="test.restart",
        )
        self.assertEqual(result.attempt_id, f"{migration_id}.attempt.0001")
        state = self._assert_no_effect_state(artifact)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["attempt_count"], 1)

    def test_missing_commit_witness_refuses_ambiguous_target_without_retry(
        self,
    ) -> None:
        backup = self._downgrade_backup_to_v1()
        migration_id = "ambiguous-missing-commit-witness"
        self._crash(
            backup=backup,
            migration_id=migration_id,
            crash_point="after_migration_commit",
        )
        artifact = self._artifact(migration_id)
        database = artifact / "workspace.sqlite3"
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.execute(
                "DELETE FROM migration_execution WHERE execution_id = ?",
                (f"migration.{migration_id}",),
            )

        with self.assertRaises(MigrationExecutionError) as captured:
            execute_offline_migration(
                plan=self._plan(backup),
                backup=backup,
                migration_id=migration_id,
                actor="test.restart",
            )
        self.assertEqual(captured.exception.code, "migration_reconciliation_required")
        state = self._assert_no_effect_state(artifact)
        self.assertEqual(state["status"], "reconciliation_required")
        self.assertEqual(state["attempt_count"], 1)
        self.assertFalse((artifact / "attempt-0002.started.json").exists())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[
                    0
                ],
                3,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM migration_execution"
                ).fetchone()[0],
                0,
            )

    def test_failing_observer_cannot_change_offline_migration_outcome(self) -> None:
        class FailingObserver:
            def emit_event(self, event):
                raise RuntimeError("observer unavailable")

            def observe_metric(self, sample):
                raise RuntimeError("observer unavailable")

        backup = self._downgrade_backup_to_v1()
        plan = prepare_offline_migration(
            backup=backup,
            target_schema_version=3,
            lifecycle=RecoveryLifecycle.OFFLINE,
        )
        result = execute_offline_migration(
            plan=plan,
            backup=backup,
            migration_id="observer-failure-v1-to-v3",
            actor="test.migration",
            observer=FailingObserver(),
        )
        self.assertEqual(result.target_schema_version, 3)
        self.assertEqual(result.lifecycle, RecoveryLifecycle.VERIFIED_READ_ONLY)
        self.assertTrue(result.database.is_file())


if __name__ == "__main__":
    if _CRASH_WORKER_ENV in os.environ:
        _run_crash_worker()
    unittest.main()
