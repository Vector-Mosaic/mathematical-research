from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.workspace_paths import (  # noqa: E402
    WorkspacePaths,
    attest_current_principal,
    stable_principal_owner_binding,
)
from research_core.workspace_schema import (  # noqa: E402
    APPLICATION_VERSION,
    LEGACY_MISSION_RUNTIME_APPLICATION_VERSION,
    REQUIRED_TABLES,
    WorkspaceSchemaError,
    _OfflineMigrationExecution,
    _apply_offline_migration_plan,
    _connect_workspace,
    _iter_statements,
    _offline_migration_capability,
    load_migrations,
)
from research_core.workspace_store import (  # noqa: E402
    AuxiliaryTable,
    RevisionCommandFamily,
    WorkspaceIntegrityError,
    WorkspaceStore,
    _AUXILIARY_SPECS,
    _FAMILY_COMMAND_KINDS,
    _ROOT4_AUXILIARY_SPECS,
    _ROOT5_EXECUTION_CONTRACT,
    _prepare_auxiliary_write,
    _prepare_continuation_checkpoint_auxiliary,
    _prepare_executive_epoch_event_auxiliary,
    _read_continuation_checkpoint_from_connection,
    _read_executive_epoch_events_from_connection,
    _row_digest,
    _verify_historical_schema8_snapshot,
    _workspace_root_digest_for_connection,
)


_LEGACY_MIGRATION_DIGESTS = {
    "0001_workspace.sql": "cf4f855aefb956c49b136d80d51ff4ad5b9bae1cf7a4eae04b36dfc4ab18a4b7",
    "0002_migration_execution.sql": "1d01f35fb0e7d4ea19ed4e9a8d06fa3cd17ee2d42496c70838e20658c5995558",
    "0003_resolution_requirement.sql": "24b59b9e82076b50ef8950ea2ce31de46e8de0403b4f991d3ac08ccbc26ab572",
    "0004_card11_activation.sql": "82b0a3d64e004ffc8797926fd8604eb3587973bf9141f8ca31433f53f5a29c69",
    "0005_mission_attempt_runtime.sql": "dcc911051f148645eb533314065e1f0b25ca0323aa5300538a069a08e8cd7a13",
    "0006_autonomous_mission.sql": "1986e197826a1fc0b6dfa6ab03d0d6ae0221ff13290519e47fd2361f3b3a9767",
    "0007_session_attempt_chain.sql": "6083884cf5ed700400dade29c1c932945af4084932b52116ac394b21b21d5b77",
    "0008_sol_ultra_attempt_contract.sql": "2246d7420b65c413b9a804faa33bc1e9073e05732dee1ec92af93c3885882c96",
}

_SUCCESSOR_TABLES = frozenset(
    {
        "executive_epoch_event",
        "continuation_checkpoint",
        "raw_capture",
        "raw_capture_artifact",
        "evidence_capture_source",
        "capture_scope_annotation_revision",
        "capture_scope_annotation_head",
    }
)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class WorkspaceStoreV9Tests(unittest.TestCase):
    def _apply_through(
        self,
        target: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Connection:
        selected = connection or sqlite3.connect(":memory:", isolation_level=None)
        selected.row_factory = sqlite3.Row
        selected.execute("PRAGMA foreign_keys = OFF")
        for migration in load_migrations()[:target]:
            for statement in _iter_statements(migration.sql):
                selected.execute(statement)
            selected.execute(
                "INSERT INTO schema_migration("
                "version, name, digest_sha256, applied_at"
                ") VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.digest_sha256,
                    "2026-08-23T00:00:00Z",
                ),
            )
        selected.execute(f"PRAGMA user_version = {target}")
        return selected

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> frozenset[str]:
        return frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )

    @staticmethod
    def _foreign_key_groups(
        connection: sqlite3.Connection,
        table: str,
    ) -> frozenset[tuple[str, tuple[tuple[str, str], ...]]]:
        grouped: dict[int, tuple[str, list[tuple[int, str, str]]]] = {}
        for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
            key = int(row[0])
            target = str(row[2])
            if key not in grouped:
                grouped[key] = (target, [])
            grouped[key][1].append((int(row[1]), str(row[3]), str(row[4])))
        return frozenset(
            (
                target,
                tuple((source, destination) for _, source, destination in sorted(parts)),
            )
            for target, parts in grouped.values()
        )

    @staticmethod
    def _index_columns(
        connection: sqlite3.Connection,
        index: str,
    ) -> tuple[str, ...]:
        return tuple(
            str(row[2]) for row in connection.execute(f"PRAGMA index_info({index})")
        )

    @staticmethod
    def _insert_prepared_auxiliary(
        connection: sqlite3.Connection,
        prepared: object,
    ) -> None:
        spec = _AUXILIARY_SPECS[prepared.table]
        columns = spec.columns + (("row_digest",) if spec.stored_row_digest else ())
        values = tuple(prepared.values[column] for column in spec.columns) + (
            ((prepared.row_digest,) if spec.stored_row_digest else ())
        )
        connection.execute(
            f"INSERT INTO {prepared.table.value}({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )

    def test_schema9_adds_only_the_seven_factual_tables_and_exact_fks(self) -> None:
        schema8 = self._apply_through(8)
        self.addCleanup(schema8.close)
        schema9 = self._apply_through(9)
        self.addCleanup(schema9.close)

        self.assertEqual(schema9.execute("PRAGMA user_version").fetchone()[0], 9)
        self.assertTrue(REQUIRED_TABLES.issubset(self._tables(schema9)))
        self.assertEqual(self._tables(schema9) - self._tables(schema8), _SUCCESSOR_TABLES)

        expected_columns = {
            "executive_epoch_event": (
                "executive_epoch_id",
                "event_ordinal",
                "mission_id",
                "project_commit_no",
                "event_kind",
                "event_json",
                "event_digest",
                "predecessor_event_ordinal",
                "predecessor_event_digest",
                "created_actor",
                "created_at",
                "row_digest",
            ),
            "continuation_checkpoint": (
                "checkpoint_id",
                "mission_id",
                "executive_epoch_id",
                "project_commit_no",
                "document_json",
                "payload_digest",
                "created_actor",
                "created_at",
                "row_digest",
            ),
            "raw_capture": (
                "capture_id",
                "project_id",
                "mission_id",
                "executive_epoch_id",
                "capture_kind",
                "observation_id",
                "assignment_id",
                "provenance_json",
                "completion_json",
                "created_at",
                "row_digest",
            ),
            "raw_capture_artifact": (
                "capture_id",
                "ordinal",
                "role",
                "logical_name",
                "blob_sha256",
            ),
            "evidence_capture_source": (
                "evidence_id",
                "evidence_revision",
                "source_ordinal",
                "capture_id",
                "artifact_ordinal",
                "exact_scope_json",
            ),
            "capture_scope_annotation_revision": (
                "annotation_id",
                "revision",
                "capture_id",
                "annotation_kind",
                "exact_scope_json",
                "lifecycle",
                "predecessor_revision",
                "created_actor",
                "created_epoch_id",
                "created_at",
                "payload_digest",
                "row_digest",
            ),
            "capture_scope_annotation_head": (
                "annotation_id",
                "revision",
                "payload_digest",
                "project_commit_no",
            ),
        }
        for table, columns in expected_columns.items():
            with self.subTest(table=table):
                self.assertEqual(
                    tuple(str(row[1]) for row in schema9.execute(f"PRAGMA table_info({table})")),
                    columns,
                )

        expected_fks = {
            "executive_epoch_event": frozenset(
                {
                    (
                        "executive_epoch_event",
                        (
                            ("executive_epoch_id", "executive_epoch_id"),
                            ("predecessor_event_ordinal", "event_ordinal"),
                            ("predecessor_event_digest", "event_digest"),
                        ),
                    ),
                    ("project_commit", (("project_commit_no", "commit_no"),)),
                }
            ),
            "continuation_checkpoint": frozenset(
                {("project_commit", (("project_commit_no", "commit_no"),))}
            ),
            "raw_capture": frozenset(
                {("workspace_metadata", (("project_id", "project_id"),))}
            ),
            "raw_capture_artifact": frozenset(
                {
                    ("raw_capture", (("capture_id", "capture_id"),)),
                    ("blob", (("blob_sha256", "sha256"),)),
                }
            ),
            "evidence_capture_source": frozenset(
                {
                    (
                        "evidence_item_revision",
                        (("evidence_id", "evidence_id"), ("evidence_revision", "revision")),
                    ),
                    (
                        "raw_capture_artifact",
                        (("capture_id", "capture_id"), ("artifact_ordinal", "ordinal")),
                    ),
                }
            ),
            "capture_scope_annotation_revision": frozenset(
                {
                    ("raw_capture", (("capture_id", "capture_id"),)),
                    (
                        "capture_scope_annotation_revision",
                        (
                            ("annotation_id", "annotation_id"),
                            ("predecessor_revision", "revision"),
                            ("capture_id", "capture_id"),
                        ),
                    ),
                }
            ),
            "capture_scope_annotation_head": frozenset(
                {
                    (
                        "capture_scope_annotation_revision",
                        (
                            ("annotation_id", "annotation_id"),
                            ("revision", "revision"),
                            ("payload_digest", "payload_digest"),
                        ),
                    ),
                    ("project_commit", (("project_commit_no", "commit_no"),)),
                }
            ),
        }
        for table, foreign_keys in expected_fks.items():
            with self.subTest(table=table):
                self.assertEqual(self._foreign_key_groups(schema9, table), foreign_keys)

        self.assertEqual(
            self._index_columns(schema9, "executive_epoch_event_mission_order"),
            ("mission_id", "executive_epoch_id", "event_ordinal"),
        )
        self.assertEqual(
            self._index_columns(schema9, "executive_epoch_event_one_bound"),
            ("executive_epoch_id",),
        )
        self.assertEqual(
            self._index_columns(schema9, "executive_epoch_event_one_terminal"),
            ("executive_epoch_id",),
        )
        self.assertEqual(
            self._index_columns(schema9, "continuation_checkpoint_mission"),
            ("mission_id", "checkpoint_id"),
        )
        self.assertEqual(
            self._index_columns(schema9, "raw_capture_mission_epoch_order"),
            ("mission_id", "executive_epoch_id", "created_at", "capture_id"),
        )
        self.assertEqual(
            self._index_columns(schema9, "raw_capture_artifact_blob"),
            ("blob_sha256", "capture_id", "ordinal"),
        )
        self.assertEqual(
            self._index_columns(schema9, "evidence_capture_source_capture_scope"),
            (
                "capture_id",
                "artifact_ordinal",
                "evidence_id",
                "evidence_revision",
                "source_ordinal",
            ),
        )
        self.assertEqual(
            self._index_columns(schema9, "capture_scope_annotation_capture_revision"),
            ("capture_id", "annotation_id", "revision"),
        )
        schema9.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(tuple(schema9.execute("PRAGMA foreign_key_check")), ())

    def test_migrations_0001_through_0008_remain_byte_frozen(self) -> None:
        actual = {
            migration.path.name: migration.digest_sha256
            for migration in load_migrations()[:8]
        }
        self.assertEqual(actual, _LEGACY_MIGRATION_DIGESTS)
        self.assertEqual(APPLICATION_VERSION, "research-workspace-mission-runtime-v1")
        self.assertEqual(
            LEGACY_MISSION_RUNTIME_APPLICATION_VERSION,
            "research-workspace-mission-runtime-v1",
        )

    def test_root1_through_root4_bytes_are_frozen_and_root5_owns_new_facts(self) -> None:
        schema8 = self._apply_through(8)
        self.addCleanup(schema8.close)
        expected_legacy_roots = {
            1: "5cfcdb6477bcc70e591b8a2f25a4465e5647e305a0615d3ef2f95fda82b390dd",
            2: "f77f5b64918f89a753e82effca2a14dd3caf953098d69f63af1da11f7e3c28e7",
            3: "1573b4f504446000e36d79133357d92662ca01bec4c920d8d73b52e41aa01714",
            4: "aa3be6fb1d94cb4ce4b1bcf6322ea767947f296923468f13df89ca288c680e82",
        }
        for version, expected in expected_legacy_roots.items():
            mode = (
                "inactive_foundation"
                if version == 1
                else "pre_cutover_observer"
                if version == 2
                else "mission_runtime"
            )
            with self.subTest(root_digest_version=version):
                self.assertEqual(
                    _workspace_root_digest_for_connection(
                        schema8,
                        project_id="project.rh",
                        canonical_authority_digest="a" * 64,
                        root_digest_version=version,
                        operating_mode=mode,
                    ),
                    expected,
                )

        successor_specs = set(_AUXILIARY_SPECS) - set(_ROOT4_AUXILIARY_SPECS)
        self.assertEqual(
            successor_specs,
            {
                AuxiliaryTable.EXECUTIVE_EPOCH_EVENT,
                AuxiliaryTable.CONTINUATION_CHECKPOINT,
                AuxiliaryTable.RAW_CAPTURE,
                AuxiliaryTable.RAW_CAPTURE_ARTIFACT,
                AuxiliaryTable.EVIDENCE_CAPTURE_SOURCE,
                AuxiliaryTable.CAPTURE_SCOPE_ANNOTATION_REVISION,
                AuxiliaryTable.CAPTURE_SCOPE_ANNOTATION_HEAD,
            },
        )

        schema9 = self._apply_through(9)
        self.addCleanup(schema9.close)

        def root(version: int) -> str:
            return _workspace_root_digest_for_connection(
                schema9,
                project_id="project.rh",
                canonical_authority_digest="a" * 64,
                root_digest_version=version,
                operating_mode="mission_runtime",
            )

        root5_before_snapshot = root(5)
        self.assertEqual(_ROOT5_EXECUTION_CONTRACT, "mission_independent_owners.v1")
        self.assertEqual(
            root5_before_snapshot,
            "774225226785b150660862f2e6515bf6620221005e51ca19b5a5186dc7371525",
        )
        self.assertIn(
            "quarantine_reason",
            {
                str(row["name"])
                for row in schema9.execute("PRAGMA table_info(blob)")
            },
        )
        root4_before_snapshot = root(4)
        schema9.execute(
            "INSERT INTO mission_bundle_snapshot("
            "project_commit_no, project_id, bundle_digest, bundle_json, "
            "source_kind, created_at, row_digest"
            ") VALUES (1, 'project.rh', ?, '{}', 'historical', ?, ?)",
            ("b" * 64, "2026-08-23T00:00:00Z", "c" * 64),
        )
        self.assertEqual(root(5), root5_before_snapshot)
        self.assertNotEqual(root(4), root4_before_snapshot)

        capture_values = {
            "capture_id": "capture.assignment.1",
            "project_id": "project.rh",
            "mission_id": "mission.rh.public.1",
            "executive_epoch_id": "epoch.1",
            "capture_kind": "assignment",
            "observation_id": "observation.assignment.1",
            "assignment_id": "assignment.1",
            "provenance_json": '{"source":"native"}',
            "completion_json": None,
            "created_at": "2026-08-23T00:00:00Z",
        }
        schema9.execute(
            "INSERT INTO raw_capture VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*capture_values.values(), _row_digest("raw_capture", capture_values)),
        )
        previous = root5_before_snapshot
        current = root(5)
        self.assertNotEqual(current, previous)

        schema9.execute(
            "INSERT INTO raw_capture_artifact VALUES "
            "('capture.assignment.1', 0, 'primary', 'assignment.txt', ?)",
            ("d" * 64,),
        )
        previous, current = current, root(5)
        self.assertNotEqual(current, previous)

        schema9.execute(
            "INSERT INTO evidence_capture_source VALUES "
            "('evidence.1', 1, 0, 'capture.assignment.1', 0, '{}')"
        )
        previous, current = current, root(5)
        self.assertNotEqual(current, previous)

        annotation_payload = {
            "capture_id": "capture.assignment.1",
            "exact_scope": {"artifact_ordinal": 0},
            "lifecycle": "active",
        }
        annotation_values = {
            "annotation_id": "capture-annotation.1",
            "revision": 1,
            "capture_id": "capture.assignment.1",
            "annotation_kind": "reviewed-no-current-semantic-delta",
            "exact_scope_json": '{"artifact_ordinal":0}',
            "lifecycle": "active",
            "predecessor_revision": None,
            "created_actor": "executive.1",
            "created_epoch_id": "epoch.1",
            "created_at": "2026-08-23T00:00:00Z",
            "payload_digest": _digest(canonical_json_bytes(annotation_payload)),
        }
        schema9.execute(
            "INSERT INTO capture_scope_annotation_revision VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                *annotation_values.values(),
                _row_digest(
                    "capture_scope_annotation_revision",
                    annotation_values,
                ),
            ),
        )
        previous, current = current, root(5)
        self.assertNotEqual(current, previous)

        schema9.execute(
            "INSERT INTO capture_scope_annotation_head VALUES (?, 1, ?, 0)",
            ("capture-annotation.1", annotation_values["payload_digest"]),
        )
        self.assertNotEqual(root(5), current)

    def test_lifecycle_rows_prepare_read_constrain_and_detect_tamper(self) -> None:
        schema9 = self._apply_through(9)
        self.addCleanup(schema9.close)
        schema9.execute("PRAGMA foreign_keys = ON")
        schema9.execute(
            "INSERT INTO workspace_metadata("
            "singleton, project_id, application_version, schema_version, "
            "root_identity, lifecycle, canonical_authority_json, "
            "canonical_authority_digest, current_writer_epoch, "
            "current_project_commit, current_root_digest, "
            "transition_head_digest, created_at, updated_at, operating_mode, "
            "root_digest_version"
            ") VALUES (1, 'project.rh', ?, 9, ?, 'offline', '{}', ?, NULL, "
            "7, ?, NULL, ?, ?, 'mission_runtime', 5)",
            (
                APPLICATION_VERSION,
                "f" * 64,
                "a" * 64,
                "0" * 64,
                "2026-08-23T00:00:00Z",
                "2026-08-23T00:00:00Z",
            ),
        )
        schema9.execute(
            "INSERT INTO project_commit("
            "commit_no, project_id, root_digest, canonical_authority_digest, "
            "transition_head_digest, command_id, created_by, created_at"
            ") VALUES (7, 'project.rh', ?, ?, NULL, ?, ?, ?)",
            (
                "0" * 64,
                "a" * 64,
                "lifecycle.fixture.commit",
                "test.schema9-root5",
                "2026-08-23T00:00:00Z",
            ),
        )
        root5_before = _workspace_root_digest_for_connection(
            schema9,
            project_id="project.rh",
            canonical_authority_digest="a" * 64,
            root_digest_version=5,
            operating_mode="mission_runtime",
        )
        root4_before = _workspace_root_digest_for_connection(
            schema9,
            project_id="project.rh",
            canonical_authority_digest="a" * 64,
            root_digest_version=4,
            operating_mode="mission_runtime",
        )
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths.from_root(Path(temporary) / "workspace")

            def prepare(write: object, created_at: str):
                return _prepare_auxiliary_write(
                    write,
                    paths=paths,
                    created_at=created_at,
                    project_commit_no=7,
                )

            mission_id = "mission.rh.public.1"

            def event_document(
                event_kind: str,
                executive_epoch_id: str,
                *,
                checkpoint_ref: dict[str, str] | None = None,
            ) -> dict[str, object]:
                common: dict[str, object] = {
                    "schema_version": 1,
                    "kind": event_kind,
                    "executive_epoch_id": executive_epoch_id,
                    "mission_id": mission_id,
                }
                specific: dict[str, object]
                if event_kind == "authorized":
                    specific = {
                        "mission_root": {
                            "kind": "mission",
                            "identity": mission_id,
                            "revision": 1,
                            "payload_sha256": "b" * 64,
                        },
                        "predecessor_checkpoint": None,
                    }
                elif event_kind == "bound":
                    specific = {
                        "goal_thread_id": "goal-thread:physical-v9",
                        "workspace_root": str(paths.root),
                    }
                elif event_kind == "checkpointed":
                    specific = {
                        "checkpoint_ref": checkpoint_ref
                        or {
                            "checkpoint_id": "checkpoint.placeholder",
                            "payload_sha256": "c" * 64,
                        }
                    }
                else:
                    specific = {
                        "reconciliation": {
                            "stage": "goal_runtime",
                            "failure_reason": "goal ended without a checkpoint",
                        }
                    }
                return {**common, **specific}

            checkpoint_epoch_id = "epoch.direct.1"
            checkpoint_id = "checkpoint:" + hashlib.sha256(
                canonical_json_bytes(
                    {
                        "mission_id": mission_id,
                        "epoch_id": checkpoint_epoch_id,
                    }
                )
            ).hexdigest()
            checkpoint_document = {
                "schema_version": 1,
                "kind": "direct_continuation_checkpoint",
                "checkpoint_id": checkpoint_id,
                "mission_root": {
                    "kind": "mission",
                    "identity": mission_id,
                    "revision": 1,
                    "payload_sha256": "b" * 64,
                },
                "strategy_root": {
                    "kind": "strategy",
                    "identity": "strategy.theta.1",
                    "revision": 1,
                    "payload_sha256": "d" * 64,
                },
                "transitive_owner_refs": [],
                "causal_pointers": [],
                "unresolved_pointers": [],
                "pending_capture_locators": [],
                "predecessor_checkpoint": None,
                "authoring_epoch_id": checkpoint_epoch_id,
                "project_commit": 7,
            }
            checkpoint_ref = {
                "checkpoint_id": checkpoint_id,
                "payload_sha256": hashlib.sha256(
                    canonical_json_bytes(checkpoint_document)
                ).hexdigest(),
            }

            authorized = prepare(
                _prepare_executive_epoch_event_auxiliary(
                    executive_epoch_id="epoch.direct.1",
                    event_ordinal=1,
                    mission_id=mission_id,
                    event_kind="authorized",
                    event=event_document("authorized", "epoch.direct.1"),
                    predecessor_event_ordinal=None,
                    predecessor_event_digest=None,
                    created_actor="executive.owner",
                ),
                "2026-08-23T00:00:00Z",
            )
            self._insert_prepared_auxiliary(schema9, authorized)
            self.assertEqual(authorized.values["project_commit_no"], 7)
            root5_after_event = _workspace_root_digest_for_connection(
                schema9,
                project_id="project.rh",
                canonical_authority_digest="a" * 64,
                root_digest_version=5,
                operating_mode="mission_runtime",
            )
            self.assertNotEqual(root5_after_event, root5_before)
            self.assertEqual(
                _workspace_root_digest_for_connection(
                    schema9,
                    project_id="project.rh",
                    canonical_authority_digest="a" * 64,
                    root_digest_version=4,
                    operating_mode="mission_runtime",
                ),
                root4_before,
            )
            bound = prepare(
                _prepare_executive_epoch_event_auxiliary(
                    executive_epoch_id="epoch.direct.1",
                    event_ordinal=2,
                    mission_id=mission_id,
                    event_kind="bound",
                    event=event_document("bound", "epoch.direct.1"),
                    predecessor_event_ordinal=1,
                    predecessor_event_digest=authorized.values["event_digest"],
                    created_actor="executive.owner",
                ),
                "2026-08-23T00:00:01Z",
            )
            self._insert_prepared_auxiliary(schema9, bound)

            duplicate_bound = prepare(
                _prepare_executive_epoch_event_auxiliary(
                    executive_epoch_id="epoch.direct.1",
                    event_ordinal=3,
                    mission_id=mission_id,
                    event_kind="bound",
                    event=event_document("bound", "epoch.direct.1"),
                    predecessor_event_ordinal=2,
                    predecessor_event_digest=bound.values["event_digest"],
                    created_actor="executive.owner",
                ),
                "2026-08-23T00:00:02Z",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_prepared_auxiliary(schema9, duplicate_bound)

            checkpointed = prepare(
                _prepare_executive_epoch_event_auxiliary(
                    executive_epoch_id="epoch.direct.1",
                    event_ordinal=3,
                    mission_id=mission_id,
                    event_kind="checkpointed",
                    event=event_document(
                        "checkpointed",
                        "epoch.direct.1",
                        checkpoint_ref=checkpoint_ref,
                    ),
                    predecessor_event_ordinal=2,
                    predecessor_event_digest=bound.values["event_digest"],
                    created_actor="executive.owner",
                ),
                "2026-08-23T00:00:03Z",
            )
            self._insert_prepared_auxiliary(schema9, checkpointed)

            duplicate_terminal = prepare(
                _prepare_executive_epoch_event_auxiliary(
                    executive_epoch_id="epoch.direct.1",
                    event_ordinal=4,
                    mission_id=mission_id,
                    event_kind="failed_before_checkpoint",
                    event=event_document(
                        "failed_before_checkpoint", "epoch.direct.1"
                    ),
                    predecessor_event_ordinal=3,
                    predecessor_event_digest=checkpointed.values["event_digest"],
                    created_actor="executive.owner",
                ),
                "2026-08-23T00:00:04Z",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_prepared_auxiliary(schema9, duplicate_terminal)

            checkpoint = prepare(
                _prepare_continuation_checkpoint_auxiliary(
                    checkpoint_id=checkpoint_id,
                    mission_id=mission_id,
                    executive_epoch_id=checkpoint_epoch_id,
                    document=checkpoint_document,
                    created_actor="executive.owner",
                ),
                "2026-08-23T00:00:03Z",
            )
            self._insert_prepared_auxiliary(schema9, checkpoint)
            self.assertEqual(checkpoint.values["project_commit_no"], 7)
            root5_after_checkpoint = _workspace_root_digest_for_connection(
                schema9,
                project_id="project.rh",
                canonical_authority_digest="a" * 64,
                root_digest_version=5,
                operating_mode="mission_runtime",
            )
            self.assertNotEqual(root5_after_checkpoint, root5_after_event)
            self.assertEqual(
                _workspace_root_digest_for_connection(
                    schema9,
                    project_id="project.rh",
                    canonical_authority_digest="a" * 64,
                    root_digest_version=4,
                    operating_mode="mission_runtime",
                ),
                root4_before,
            )
            duplicate_checkpoint = prepare(
                _prepare_continuation_checkpoint_auxiliary(
                    checkpoint_id="checkpoint.direct.duplicate",
                    mission_id=mission_id,
                    executive_epoch_id="epoch.direct.1",
                    document={"frontier": {"strategy_id": "strategy.theta.2"}},
                    created_actor="executive.owner",
                ),
                "2026-08-23T00:00:05Z",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_prepared_auxiliary(schema9, duplicate_checkpoint)

            events = _read_executive_epoch_events_from_connection(
                schema9,
                executive_epoch_id="epoch.direct.1",
                mission_id=mission_id,
            )
            self.assertEqual(
                tuple(item["event_kind"] for item in events),
                ("authorized", "bound", "checkpointed"),
            )
            self.assertEqual(
                events[0]["event"],
                event_document("authorized", "epoch.direct.1"),
            )
            self.assertEqual(events[0]["project_commit_no"], 7)
            self.assertEqual(events[2]["predecessor_event_digest"], events[1]["event_digest"])
            from research_core import mission_executive

            with mock.patch.object(
                mission_executive,
                "_normalize_direct_continuation_checkpoint",
                wraps=mission_executive._normalize_direct_continuation_checkpoint,
            ) as normalize:
                by_id = _read_continuation_checkpoint_from_connection(
                    schema9,
                    checkpoint_id=checkpoint_id,
                    mission_id=mission_id,
                )
                self.assertEqual(normalize.call_count, 1)
                normalize.reset_mock()
                by_epoch = _read_continuation_checkpoint_from_connection(
                    schema9,
                    executive_epoch_id=checkpoint_epoch_id,
                )
                self.assertEqual(normalize.call_count, 1)
            self.assertEqual(by_id, by_epoch)
            assert by_id is not None
            self.assertEqual(
                by_id["document"],
                checkpoint_document,
            )
            self.assertEqual(by_id["project_commit_no"], 7)

            def insert_event_chain(
                executive_epoch_id: str,
                event_kinds: tuple[str, ...],
            ) -> None:
                predecessor_digest = None
                for ordinal, event_kind in enumerate(event_kinds, start=1):
                    item = prepare(
                        _prepare_executive_epoch_event_auxiliary(
                            executive_epoch_id=executive_epoch_id,
                            event_ordinal=ordinal,
                            mission_id=mission_id,
                            event_kind=event_kind,
                            event=event_document(event_kind, executive_epoch_id),
                            predecessor_event_ordinal=(
                                None if ordinal == 1 else ordinal - 1
                            ),
                            predecessor_event_digest=predecessor_digest,
                            created_actor="executive.owner",
                        ),
                        f"2026-08-23T00:01:{ordinal:02d}Z",
                    )
                    self._insert_prepared_auxiliary(schema9, item)
                    predecessor_digest = item.values["event_digest"]

            legal_prefixes = (
                ("authorized",),
                ("authorized", "bound"),
                ("authorized", "failed_before_checkpoint"),
                ("authorized", "bound", "checkpointed"),
                ("authorized", "bound", "failed_before_checkpoint"),
            )
            for index, event_kinds in enumerate(legal_prefixes, start=1):
                epoch_id = f"epoch.legal.{index}"
                insert_event_chain(epoch_id, event_kinds)
                self.assertEqual(
                    tuple(
                        item["event_kind"]
                        for item in _read_executive_epoch_events_from_connection(
                            schema9,
                            executive_epoch_id=epoch_id,
                        )
                    ),
                    event_kinds,
                )

            illegal_prefixes = (
                ("bound",),
                ("authorized", "authorized"),
                ("authorized", "checkpointed"),
                ("authorized", "failed_before_checkpoint", "bound"),
                ("authorized", "bound", "authorized"),
                ("authorized", "bound", "checkpointed", "authorized"),
            )
            for index, event_kinds in enumerate(illegal_prefixes, start=1):
                epoch_id = f"epoch.illegal.{index}"
                insert_event_chain(epoch_id, event_kinds)
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "legal factual lifecycle prefix",
                ):
                    _read_executive_epoch_events_from_connection(
                        schema9,
                        executive_epoch_id=epoch_id,
                    )

            schema9.execute(
                "UPDATE executive_epoch_event SET event_json = '{}' "
                "WHERE executive_epoch_id = 'epoch.direct.1' AND event_ordinal = 2"
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "strict JSON or has invalid universal facts",
            ):
                _read_executive_epoch_events_from_connection(
                    schema9,
                    executive_epoch_id="epoch.direct.1",
                )
            stale_checkpoint_document = {
                **checkpoint_document,
                "project_commit": 6,
            }
            schema9.execute(
                "UPDATE continuation_checkpoint SET document_json = ? "
                "WHERE checkpoint_id = ?",
                (
                    canonical_json_bytes(stale_checkpoint_document).decode(
                        "utf-8"
                    ),
                    checkpoint_id,
                ),
            )
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "checkpoint payload or row digest",
            ):
                _read_continuation_checkpoint_from_connection(
                    schema9,
                    checkpoint_id=checkpoint_id,
                )

            # Correct physical hashes must not make an unsupported document valid.
            malformed_document = {**checkpoint_document, "schema_version": 0}
            malformed_bytes = canonical_json_bytes(malformed_document)
            malformed_values = {
                **checkpoint.values,
                "document_json": malformed_bytes.decode("utf-8"),
                "payload_digest": _digest(malformed_bytes),
            }
            schema9.execute(
                "UPDATE continuation_checkpoint "
                "SET document_json = ?, payload_digest = ?, row_digest = ? "
                "WHERE checkpoint_id = ?",
                (
                    malformed_values["document_json"],
                    malformed_values["payload_digest"],
                    _row_digest(
                        AuxiliaryTable.CONTINUATION_CHECKPOINT.value,
                        malformed_values,
                    ),
                    checkpoint_id,
                ),
            )
            with mock.patch.object(
                mission_executive,
                "_normalize_direct_continuation_checkpoint",
                wraps=mission_executive._normalize_direct_continuation_checkpoint,
            ) as normalize:
                with self.assertRaisesRegex(
                    WorkspaceIntegrityError,
                    "checkpoint document is not a supported exact schema",
                ):
                    _read_continuation_checkpoint_from_connection(
                        schema9,
                        checkpoint_id=checkpoint_id,
                    )
                self.assertEqual(normalize.call_count, 1)


    def test_historical_schema8_snapshot_schema_remains_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "schema8.sqlite3"
            connection = _connect_workspace(database)
            try:
                self._apply_through(8, connection=connection)
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = DELETE")
                self.assertEqual(
                    _verify_historical_schema8_snapshot(connection).schema_version,
                    8,
                )
            finally:
                connection.close()

    def test_fresh_direct_mission_workspace_is_schema10_root6(self) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        self.assertTrue(loaded.ok, loaded.failure)
        assert loaded.value is not None
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths.from_root(Path(temporary) / "workspace")
            store = WorkspaceStore.initialize_direct_mission_workspace(
                paths,
                project_id="project.rh",
                canonical_snapshot=loaded.value,
                actor="test.schema10-root6",
            )
            metadata = store.read_metadata()
            self.assertEqual(
                (metadata["schema_version"], metadata["root_digest_version"]),
                (10, 6),
            )
            authority = json.loads(str(metadata["canonical_authority_json"]))
            self.assertEqual(
                set(authority),
                {
                    "binding_version",
                    "source_class",
                    "canonical_state_path",
                    "canonical_state_sha256",
                    "source_commit",
                },
            )
            self.assertEqual(store.verify_integrity().schema_version, 10)
            with store.snapshot_connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM mission_bundle_snapshot"
                    ).fetchone()[0],
                    0,
                )
            legacy_raw = canonical_json_bytes(
                {
                    "source_class": "live_canonical",
                    "canonical_state_path": authority["canonical_state_path"],
                    "canonical_state_sha256": authority["canonical_state_sha256"],
                    "source_commit": None,
                }
            )
            connection = sqlite3.connect(paths.database)
            try:
                connection.execute(
                    "UPDATE workspace_metadata SET canonical_authority_json = ?, "
                    "canonical_authority_digest = ? WHERE singleton = 1",
                    (
                        legacy_raw.decode("utf-8"),
                        hashlib.sha256(legacy_raw).hexdigest(),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(
                WorkspaceIntegrityError,
                "exact five-key v2 release binding",
            ):
                store.verify_integrity()

    def test_direct_genesis_is_one_atomic_revision_one_store_command(self) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        self.assertTrue(loaded.ok, loaded.failure)
        assert loaded.value is not None
        seed = json.loads(
            (
                REPO_ROOT
                / "contracts"
                / "rh_autonomous_mission_seed.v1.json"
            ).read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths.from_root(Path(temporary) / "workspace")
            store = WorkspaceStore.initialize_direct_mission_workspace(
                paths,
                project_id="project.riemann_hypothesis",
                canonical_snapshot=loaded.value,
                actor="test.direct-genesis",
            )
            metadata = store.read_metadata()
            lease = store.claim_writer(
                owner=stable_principal_owner_binding(attest_current_principal()),
                creation_basis="test.direct-genesis",
                expected_project_commit=int(metadata["current_project_commit"]),
                expected_root_digest=str(metadata["current_root_digest"]),
                expected_canonical_authority_digest=str(
                    metadata["canonical_authority_digest"]
                ),
            )
            outcome = store.initialize_direct_mission_genesis(
                seed,
                mission_id="mission.rh.public.1",
                canonical_snapshot=loaded.value,
                lease=lease,
                command_id="test.direct-genesis",
                actor="test.direct-genesis",
            )
            self.assertEqual(outcome.project_commit, 1)
            self.assertEqual(
                tuple(reference.revision for reference in outcome.changed_heads),
                (1, 1, 1),
            )
            integrity = store.verify_integrity()
            self.assertEqual(
                (integrity.current_project_commit, integrity.revision_count),
                (1, 3),
            )
            self.assertFalse(
                hasattr(WorkspaceStore, "materialize_successor_genesis_frontier")
            )
            for retired_name in (
                "initialize_card11",
                "issue_card11_store_capability",
                "claim_card11_writer",
                "release_card11_writer",
                "fail_card11_activation",
                "apply_card11_activation_command",
                "read_card11_workspace_snapshot",
            ):
                self.assertFalse(hasattr(WorkspaceStore, retired_name))
            self.assertNotIn(
                "campaign_compatibility",
                {family.value for family in RevisionCommandFamily},
            )
            self.assertNotIn(
                "card11_activation",
                {family.value for family in RevisionCommandFamily},
            )
            executable_command_kinds = {
                command_kind
                for command_kinds in _FAMILY_COMMAND_KINDS.values()
                for command_kind in command_kinds
            }
            self.assertTrue(
                {
                    "activate_campaign_observer",
                    "persist_campaign_observation",
                    "materialize_successor_genesis_frontier",
                    "persist_validated_inactive_topology",
                    "persist_inactive_attempt_result",
                }.isdisjoint(executable_command_kinds)
            )
            with store.snapshot_connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM mission_bundle_snapshot"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM executive_epoch_event"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM writer_epoch").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    tuple(
                        row["revision"]
                        for table in (
                            "mission_revision",
                            "branch_revision",
                            "strategy_revision",
                        )
                        for row in connection.execute(
                            f"SELECT revision FROM {table}"
                        )
                    ),
                    (1, 1, 1),
                )

            replay = store.initialize_direct_mission_genesis(
                seed,
                mission_id="mission.rh.public.1",
                canonical_snapshot=loaded.value,
                lease=lease,
                command_id="test.direct-genesis",
                actor="test.direct-genesis",
            )
            self.assertEqual(replay.project_commit, 1)
            malformed = json.loads(json.dumps(seed))
            malformed["strategy"]["mission_id"] = "mission.other"
            with self.assertRaises(ValueError):
                store.initialize_direct_mission_genesis(
                    malformed,
                    mission_id="mission.rh.public.1",
                    canonical_snapshot=loaded.value,
                    lease=lease,
                    command_id="test.direct-genesis.malformed",
                    actor="test.direct-genesis",
                )
            self.assertEqual(store.read_metadata()["current_project_commit"], 1)

    def test_generic_offline_writer_rejects_schema9(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "workspace.sqlite3"
            sqlite3.connect(database).close()
            capability = _offline_migration_capability(
                database,
                root_identity="a" * 64,
            )
            connection = _connect_workspace(database)
            try:
                migration9 = load_migrations()[8]
                execution = _OfflineMigrationExecution(
                    execution_id="migration.schema9-rejected",
                    attempt_id="schema9-rejected.attempt.0001",
                    source_schema_version=8,
                    target_schema_version=9,
                    verified_backup_manifest_sha256="a" * 64,
                    applied_history_sha256="b" * 64,
                    plan_sha256="c" * 64,
                    started_at="2026-08-23T00:00:00Z",
                    completed_at="2026-08-23T00:01:00Z",
                )
                with self.assertRaisesRegex(
                    WorkspaceSchemaError,
                    "specialized local dot6 successor upgrader",
                ):
                    _apply_offline_migration_plan(
                        connection,
                        pending_migrations=((
                            migration9.version,
                            migration9.name,
                            migration9.digest_sha256,
                        ),),
                        execution=execution,
                        capability=capability,
                    )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
