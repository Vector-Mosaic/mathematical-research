from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.migration_executor import (  # noqa: E402
    MigrationExecutionError,
    execute_offline_migration,
)
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_recovery import (  # noqa: E402
    RecoveryError,
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
    open_snapshot_connection,
    registered_schema_contract,
    verify_schema,
    verify_schema_contract,
)
from research_core import workspace_store as store_module  # noqa: E402
from research_core import migration_executor as migration_executor_module  # noqa: E402
from research_core import owner_content_access as content_access_module  # noqa: E402
from research_core.workspace_store import WorkspaceStore  # noqa: E402
import test_workspace_recovery as recovery_fixture  # noqa: E402
from test_workspace_schema10_migration import initialize_schema9_fixture  # noqa: E402


def initialize_schema10_fixture(paths, *, project_id, canonical_snapshot, actor, **kwargs):
    """Frozen native root6 genesis; later fixture writes use the real Store."""

    paths.root.mkdir(parents=True)
    paths = WorkspacePaths.from_root(paths.root)
    for directory in (paths.cas_sha256, paths.staging, paths.backups, paths.recovery):
        directory.mkdir(parents=True, exist_ok=True)
    authority = canonical_payload(
        store_module._verified_live_canonical_authority(canonical_snapshot)
    )
    now = "2026-09-14T00:00:00Z"
    with closing(_connect_workspace(paths.database)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for migration in load_migrations()[:10]:
            for statement in _iter_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migration VALUES(?,?,?,?)",
                (migration.version, migration.name, migration.digest_sha256, now),
            )
        connection.execute("PRAGMA user_version=10")
        connection.execute("PRAGMA foreign_keys=ON")
        root = store_module._root6_commitment(
            project_id=project_id,
            schema_contract=registered_schema_contract(10),
            project_commit_no=0,
            canonical_authority_digest=authority.sha256,
            predecessor_project_commit_no=None,
            predecessor_root_digest=None,
            predecessor_transition_head_digest=None,
            transition_head_digest=None,
        )
        connection.execute(
            "INSERT INTO workspace_metadata(singleton,project_id,application_version,"
            "schema_version,root_identity,lifecycle,canonical_authority_json,"
            "canonical_authority_digest,current_writer_epoch,current_project_commit,"
            "current_root_digest,transition_head_digest,created_at,updated_at,"
            "operating_mode,root_digest_version) "
            "VALUES(1,?,?,10,?,'offline',?,?,NULL,0,?,NULL,?,?,'mission_runtime',6)",
            (project_id, APPLICATION_VERSION, paths.root_identity, authority.text,
             authority.sha256, root, now, now),
        )
        store_module._initialize_current_dependency_projection(
            connection, project_id=project_id, current_project_commit=0,
        )
        connection.execute(
            "INSERT INTO project_commit(commit_no,project_id,root_digest,"
            "canonical_authority_digest,transition_head_digest,command_id,"
            "created_by,created_at) VALUES(0,?,?,?,NULL,NULL,?,?)",
            (project_id, root, authority.sha256, actor, now),
        )
        verify_schema(connection, schema_version=10)
    return WorkspaceStore.open(paths, expected_project_id=project_id)


class Schema12ContractTests(unittest.TestCase):
    def test_generation_selection_keeps_exact_retained_contracts(self):
        self.assertTrue(is_current_store_generation(12, 6, "mission_runtime"))
        for schema, root in ((9, 5), (10, 6)):
            self.assertFalse(is_current_store_generation(schema, root, "mission_runtime"))
            self.assertTrue(is_direct_mission_generation(schema, root, "mission_runtime"))
        for schema, root in ((9, 6), (10, 5), (11, 6), (12, 5), (13, 6)):
            self.assertFalse(is_direct_mission_generation(schema, root, "mission_runtime"))

    def test_schema10_migration_bytes_and_contract_remain_distinct(self):
        self.assertEqual(
            load_migrations()[9].digest_sha256,
            "f4289b8fd70dbc2f23d45329876c2d7e4a2b1e05e9557d6147fdabca957b30e1",
        )
        old = registered_schema_contract(10)
        current = registered_schema_contract(12)
        self.assertNotEqual(old["migration_set_digest"], current["migration_set_digest"])
        self.assertNotEqual(old["schema_object_digest"], current["schema_object_digest"])
        self.assertEqual(registered_schema_contract(10), old)

    def test_physical_contract_cannot_be_verified_as_an_older_cut(self):
        with tempfile.TemporaryDirectory(prefix="rh-schema12-contract-") as temporary:
            with closing(_connect_workspace(Path(temporary) / "workspace.sqlite3")) as connection:
                connection.execute("PRAGMA foreign_keys=OFF")
                for migration in load_migrations():
                    for statement in _iter_statements(migration.sql):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migration VALUES(?,?,?,?)",
                        (migration.version, migration.name, migration.digest_sha256, "fixture"),
                    )
                connection.execute("PRAGMA user_version=12")
                connection.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(verify_schema_contract(connection).schema_version, 12)
                with self.assertRaises(WorkspaceSchemaError):
                    verify_schema_contract(connection, schema_version=10)
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(transition_journal)")
                }
                self.assertIn("owner_content_index_json", columns)
                capture_seek = tuple(connection.execute(
                    "EXPLAIN QUERY PLAN SELECT "
                    "json_extract(auxiliary_writes_json, '$[#-1].primary_key.capture_id') "
                    "FROM transition_journal INDEXED BY transition_journal_capture_commit "
                    "WHERE command_kind='commit_raw_capture' "
                    "AND project_commit_no>? AND project_commit_no<=? ORDER BY project_commit_no",
                    (10, 20),
                ))
                self.assertTrue(any(
                    "transition_journal_capture_commit" in str(row[3])
                    and "SEARCH" in str(row[3])
                    for row in capture_seek
                ), capture_seek)
                for rejected_kind in ("directory", "posting", "unregistered_kind"):
                    with self.subTest(rejected_kind=rejected_kind), self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "INSERT INTO owner_content_index_node VALUES(?,?,?)",
                            ("a" * 64, rejected_kind, "{}"),
                        )


class Schema12MigrationTests(unittest.TestCase):
    def _fixture(self, *, retained_root5=False):
        fixture = recovery_fixture.WorkspaceRecoveryTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        with patch.object(
            WorkspaceStore, "initialize_direct_mission_workspace",
            side_effect=(initialize_schema9_fixture if retained_root5 else initialize_schema10_fixture),
        ):
            fixture.setUp()
        return fixture

    def _migrate(self, source, *, target, label):
        plan = prepare_offline_migration(
            backup=source, target_schema_version=target,
            lifecycle=RecoveryLifecycle.QUIESCED,
        )
        if target == 12:
            self.assertEqual(plan.current_schema_version, 10)
            self.assertEqual(tuple(item[0] for item in plan.pending_migrations), (11, 12))
        try:
            with patch.object(
                content_access_module, "verify_owner_content_relation",
                wraps=content_access_module.verify_owner_content_relation,
            ) as audit_content:
                result = execute_offline_migration(
                    plan=plan, backup=source, migration_id=label, actor="test.schema12",
                )
            self.assertEqual(audit_content.call_count, 1 if target == 12 else 0)
            return rebackup_verified_offline_migration(
                source_backup=source, migration_result=result, backup_id=label + "-backup",
                authority_repo_root=recovery_fixture.REPO_ROOT,
            )
        except MigrationExecutionError as failure:
            # Preserve the failure while letting the existing safe staging
            # type/file/line projection identify its originating owner boundary.
            while failure.__cause__ is not None:
                failure = failure.__cause__
            raise failure from None

    def test_native_and_previously_migrated_root6_keep_their_old_cut_contracts(self):
        for retained_root5 in (False, True):
            with self.subTest(retained_root5=retained_root5):
                fixture = self._fixture(retained_root5=retained_root5)
                source = fixture.create_backup("schema12-source")
                if retained_root5:
                    source = self._migrate(source, target=10, label="retained-root6")
                source_database = source.path / "workspace.sqlite3"
                source_bytes = source_database.read_bytes()
                with closing(sqlite3.connect(source_database)) as connection:
                    old_commits = tuple(connection.execute("SELECT * FROM project_commit ORDER BY commit_no"))
                    old_journals = tuple(connection.execute(
                        "SELECT project_commit_no,digest_sha256 FROM transition_journal ORDER BY sequence_no"
                    ))
                    retained_heads = tuple(connection.execute(
                        "SELECT * FROM workspace_root_transition_retained_head ORDER BY ordinal"
                    ))
                    dependency_root = tuple(connection.execute(
                        "SELECT root_digest,entry_count FROM current_dependency_projection"
                    ))
                observed_scopes = []
                original_scope = store_module._schema_root_transition_source_scope

                @contextmanager
                def observed_source_scope(connection, *, transition):
                    index = transition.source_journal_index
                    self.assertIsNotNone(index)
                    self.assertIsNone(index.migration_source_transition)
                    self.assertEqual(
                        index.schema_v10_root_transition is not None, retained_root5,
                    )
                    prior_audit = store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get()
                    prior_schema = index.verified_schema
                    # The existing scope must restore its private authority on
                    # an exception as well as after the real bootstrap reads.
                    with self.assertRaisesRegex(RuntimeError, "source scope exit"):
                        with original_scope(connection, transition=transition):
                            self.assertIs(index.migration_source_transition, transition)
                            raise RuntimeError("source scope exit")
                    self.assertIsNone(index.migration_source_transition)
                    self.assertIs(index.verified_schema, prior_schema)
                    self.assertIs(store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(), prior_audit)
                    with original_scope(connection, transition=transition):
                        facts = store_module._direct_recovery_facts_from_connection(
                            connection, project_id=fixture.project_id,
                            cut_project_commit=None, journal_index=index,
                        )
                        self.assertIs(facts, index.complete_recovery_facts)
                        # A cached complete derivation alone cannot authorize
                        # reads while physical schema12 still has schema10
                        # metadata. Ordinary generation checks remain strict.
                        with patch.object(index, "migration_source_transition", None):
                            with self.assertRaisesRegex(
                                store_module.WorkspaceIntegrityError,
                                "supported direct Mission store",
                            ):
                                store_module._direct_recovery_facts_from_connection(
                                    connection, project_id=fixture.project_id,
                                    cut_project_commit=None, journal_index=index,
                                )
                        for project, cut in (("project.wrong", None), (fixture.project_id, 0)):
                            with self.assertRaisesRegex(
                                store_module.WorkspaceIntegrityError, "exact issued scope",
                            ):
                                store_module._direct_recovery_facts_from_connection(
                                    connection, project_id=project,
                                    cut_project_commit=cut, journal_index=index,
                                )
                        yield
                    self.assertIsNone(index.migration_source_transition)
                    self.assertIs(index.verified_schema, prior_schema)
                    self.assertIs(store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(), prior_audit)
                    with self.assertRaisesRegex(
                        store_module.WorkspaceIntegrityError, "supported direct Mission store",
                    ):
                        store_module._direct_recovery_facts_from_connection(
                            connection, project_id=fixture.project_id,
                            cut_project_commit=None, journal_index=index,
                        )
                    observed_scopes.append(True)

                with patch.object(
                    store_module, "_schema_root_transition_source_scope", observed_source_scope,
                ):
                    migrated = self._migrate(source, target=12, label="indexed-root6")
                self.assertEqual(len(observed_scopes), 2)
                self.assertEqual(migrated.manifest.store.schema_version, 12)
                self.assertEqual(migrated.manifest.store.root_digest_version, 6)
                self.assertEqual(migrated.manifest.canonical, source.manifest.canonical)
                self.assertEqual(migrated.manifest.evidence_inventory, source.manifest.evidence_inventory)
                self.assertEqual(source_database.read_bytes(), source_bytes)
                self.assertTrue(verify_portable_backup_set(migrated.path).semantic_verification_passed)
                inspection = fixture.root / "indexed-inspection.sqlite3"
                shutil.copyfile(migrated.path / "workspace.sqlite3", inspection)
                with closing(_connect_workspace(inspection)) as connection:
                    self.assertEqual(tuple(tuple(row) for row in connection.execute(
                        "SELECT * FROM project_commit ORDER BY commit_no"
                    ))[:len(old_commits)], old_commits)
                    # sqlite.Row compares by row type; compare scalar tuples for
                    # the two independently opened connections below.
                    after_journals = tuple(tuple(row) for row in connection.execute(
                        "SELECT project_commit_no,digest_sha256 FROM transition_journal ORDER BY sequence_no"
                    ))
                    self.assertEqual(after_journals[:-1], old_journals)
                    self.assertEqual(tuple(tuple(row) for row in connection.execute(
                        "SELECT * FROM workspace_root_transition_retained_head ORDER BY ordinal"
                    )), retained_heads)
                    self.assertEqual(tuple(tuple(row) for row in connection.execute(
                        "SELECT root_digest,entry_count FROM current_dependency_projection"
                    )), dependency_root)
                    descriptors = tuple(connection.execute(
                        "SELECT owner_content_index_json FROM transition_journal ORDER BY sequence_no"
                    ))
                    self.assertTrue(all(row[0] is None for row in descriptors[:-1]))
                    descriptor = json.loads(descriptors[-1][0])
                    self.assertEqual(set(descriptor), {
                        "contract_version", "projection_profile_digest", "root",
                    })
                    self.assertEqual(descriptor["contract_version"], 2)
                    self.assertEqual(set(descriptor["root"]), {
                        "digest", "kind", "count", "first", "last", "height", "used_bytes",
                    })
                    self.assertEqual(descriptor["root"]["count"], 5)
                    self.assertEqual(tuple(connection.execute(
                        "SELECT 1 FROM workspace_root_contract_transition WHERE target_schema_version=11"
                    )), ())
                    self.assertEqual(tuple(connection.execute(
                        "SELECT 1 FROM owner_content_index_node WHERE node_kind IN ('directory','posting')"
                    )), ())
                    for commit_no in (source.manifest.store.project_commit_id,
                                      migrated.manifest.store.project_commit_id):
                        bindings = store_module._root6_transition_bindings_at_cut(
                            connection, project_id=fixture.project_id,
                            project_commit_no=commit_no,
                        )
                        self.assertIsNotNone(bindings)
                        expected_schema = 10 if commit_no == source.manifest.store.project_commit_id else 12
                        expected_contract = registered_schema_contract(expected_schema)
                        self.assertEqual(
                            bindings.schema_contract_digest,
                            canonical_payload(expected_contract).sha256,
                        )

    def test_fault_before_commit_restores_exact_source_and_after_commit_replays(self):
        fixture = self._fixture()
        source = fixture.create_backup("schema12-fault-source")
        source_digest = hashlib.sha256((source.path / "workspace.sqlite3").read_bytes()).hexdigest()
        with self.assertRaisesRegex(RecoveryError, "schema11 is not"):
            prepare_offline_migration(
                backup=source, target_schema_version=11, lifecycle=RecoveryLifecycle.QUIESCED,
            )
        plan = prepare_offline_migration(
            backup=source, target_schema_version=12, lifecycle=RecoveryLifecycle.QUIESCED,
        )
        prior_audit = store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get()
        for point in (
            "after_migration_statement", "after_schema_v12_owner_content_index",
            "before_migration_commit",
        ):
            reached = []

            def fail(actual, details):
                reached.append(actual)
                if actual == point:
                    raise RuntimeError("injected schema12 fault")

            with self.assertRaises(MigrationExecutionError) as captured:
                execute_offline_migration(
                    plan=plan, backup=source, migration_id="schema12-" + point.replace("_", "-"),
                    actor="test.schema12", fault_hook=fail,
                )
            self.assertIn(point, reached)
            outcome = json.loads((captured.exception.artifact / "attempt-0001.outcome.json").read_text())
            self.assertTrue(outcome["rollback_exact"])
            self.assertEqual(outcome["schema_version"], 10)
            self.assertIs(store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(), prior_audit)

        def postcommit_fail(actual, details):
            if actual == "after_migration_commit_before_return":
                raise RuntimeError("injected schema12 acknowledgement loss")

        with self.assertRaises(MigrationExecutionError):
            execute_offline_migration(
                plan=plan, backup=source, migration_id="schema12-postcommit",
                actor="test.schema12", fault_hook=postcommit_fail,
            )
        self.assertIs(store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(), prior_audit)
        with patch.object(
            content_access_module, "verify_owner_content_relation",
            wraps=content_access_module.verify_owner_content_relation,
        ) as audit_content:
            result = execute_offline_migration(
                plan=plan, backup=source, migration_id="schema12-postcommit", actor="test.schema12",
            )
        self.assertEqual(audit_content.call_count, 1)
        self.assertIs(store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(), prior_audit)
        with closing(open_snapshot_connection(result.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            # Successful sealed reissue must not relax ordinary WAL admission.
            with self.assertRaisesRegex(WorkspaceSchemaError, "not in WAL mode"):
                verify_schema_contract(connection)
        migrated = rebackup_verified_offline_migration(
            source_backup=source, migration_result=result, backup_id="schema12-postcommit-backup",
            authority_repo_root=recovery_fixture.REPO_ROOT,
        )
        self.assertIs(store_module._ACTIVE_AUDIT_JOURNAL_INDEX.get(), prior_audit)
        self.assertEqual(migrated.manifest.store.project_commit_id,
                         source.manifest.store.project_commit_id + 1)
        self.assertEqual(hashlib.sha256((source.path / "workspace.sqlite3").read_bytes()).hexdigest(),
                         source_digest)

        # Reuse is tied to the actual transaction/connection, not a skip flag.
        # A same-connection row write after its full audit must roll back, even
        # when it writes the existing value and leaves the logical rows equal.
        actual_apply = migration_executor_module._apply_offline_migration_plan

        def apply_with_late_write(connection, **kwargs):
            original_hook = kwargs.get("fault_hook")

            def write_after_audit(point, details):
                if point == "before_migration_commit":
                    connection.execute(
                        "UPDATE workspace_metadata SET updated_at=updated_at WHERE singleton=1"
                    )
                if original_hook is not None:
                    original_hook(point, details)

            return actual_apply(connection, **{**kwargs, "fault_hook": write_after_audit})

        with patch.object(
            migration_executor_module, "_apply_offline_migration_plan",
            side_effect=apply_with_late_write,
        ):
            with self.assertRaises(MigrationExecutionError) as changed_after_audit:
                execute_offline_migration(
                    plan=plan, backup=source, migration_id="schema12-changed-after-audit",
                    actor="test.schema12",
                )
        outcome = json.loads((
            changed_after_audit.exception.artifact / "attempt-0001.outcome.json"
        ).read_text())
        self.assertTrue(outcome["rollback_exact"])
        self.assertEqual(outcome["schema_version"], 10)


if __name__ == "__main__":
    unittest.main()
