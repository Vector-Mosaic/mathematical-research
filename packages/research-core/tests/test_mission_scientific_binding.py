"""Exact inactive Mission-to-scientific-Context binding in disposable Stores."""

from __future__ import annotations

import sys
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

import test_mission_interface_direct as fixtures  # noqa: E402
import test_mission_model_migration as migration  # noqa: E402
from research_core.context_revision import (  # noqa: E402
    commit_context_revision,
    issue_context_revision,
    issue_context_revision_v3,
    prepare_scientific_context_update,
    read_context_revision,
)
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.workspace_paths import attest_current_principal  # noqa: E402
from research_core.workspace_schema import (  # noqa: E402
    IdentityKind,
    TypedWorkspaceId,
    canonical_payload,
    open_immutable_snapshot_connection,
)
from research_core.workspace_store import (  # noqa: E402
    CommandConflictError,
    CommittedCommandAcknowledgementError,
    StaleCommandError,
    StaleWriterError,
    WorkspaceIntegrityError,
    _issue_writer_lease,
)


class MissionScientificBindingTests(unittest.TestCase):
    setUpClass = classmethod(migration.MissionModelMigrationTests.setUpClass.__func__)
    _authorize = fixtures.DirectMissionInterfaceTests._authorize
    _tables = migration.MissionModelMigrationTests._tables
    _fail = migration.MissionModelMigrationTests._fail

    def setUp(self) -> None:
        fixtures.DirectMissionInterfaceTests.setUp(self)
        self.mission_id = TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        self.context_id = TypedWorkspaceId(IdentityKind.CONTEXT, "science.rh")
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self.before_context = self._publish_context(epoch_id)
        self._fail(epoch_id)
        self.before_mission = self.store.get_head(self.mission_id)
        assert self.before_mission is not None
        self.arguments = {
            "mission_id": "mission.1",
            "expected_head_revision": self.before_mission.reference.revision,
            "expected_head_payload_digest": self.before_mission.payload_digest,
            "context_id": self.context_id.value,
            "expected_context_revision": self.before_context.reference.revision,
            "expected_context_payload_digest": self.before_context.payload_digest,
            "expected_canonical_authority_digest": self.store.read_metadata()[
                "canonical_authority_digest"
            ],
            "lease": self.store.reissue_writer_lease(attest_current_principal()),
            "command_id": "owner-scientific-context-binding:test",
            "actor": "owner-scientific-context-binding-test",
        }

    def _bind(self, epoch_id: str) -> None:
        # Successor Epochs must not reuse a prior Epoch's Goal identity.
        self.goal_thread_id = f"goal-thread:scientific-binding:{epoch_id}"
        fixtures.DirectMissionInterfaceTests._bind(self, epoch_id)

    def _publish_context(self, epoch_id, *, replace_existing=False):
        authority = self.interface._direct_epoch_authority(epoch_id)
        treatment = {
            "question": "Can the surviving contour estimate support a wider construction?",
            "account": (
                "The compact-domain estimate survives the normalization correction."
                if not replace_existing else
                "The estimate survives, with an additional endpoint qualification."
            ),
            "qualifications": ["Compact domain only; the full Jensen implication is open."],
            "sources": [],
        }
        if replace_existing:
            previous = read_context_revision(self.store, context_id=self.context_id.value)
            update = {
                "context_id": self.context_id.key,
                "expected_head": {
                    "kind": "context", "identity": self.context_id.value,
                    "revision": previous.revision, "payload_sha256": previous.payload_digest,
                },
                "create": None,
                "patch": {
                    "insert": {}, "replace": {"surviving-contour": treatment},
                    "remove": [], "exposure_add": [], "exposure_remove": [],
                    "metadata_replace": {},
                },
            }
        else:
            previous = None
            record = issue_context_revision_v3(
                authority=authority,
                context_id=self.context_id.value,
                purpose="Keep the parent program and qualified surviving results visible.",
                question="Which available constructions can now advance the parent program?",
                treatments={"surviving-contour": treatment},
                exposed_treatments=(), known_omissions=(),
                restricted_uses=("Not canonical mathematical authority.",),
                independence_treatment={"method": "exact qualified source comparison"},
                restrictions=(),
            )
            update = {
                "context_id": self.context_id.key, "expected_head": None,
                "create": deep_thaw(record.document), "patch": None,
            }
        prepared = prepare_scientific_context_update(
            authority=authority, update=update, previous=previous,
        )
        request_digest = canonical_payload(update).sha256
        self.store.commit_scientific_context_publication(
            executive_epoch_id=epoch_id, mission_id="mission.1", updates=(prepared,),
            exposure_required=(), request_sha256=request_digest,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            command_id=f"binding-fixture-context:{request_digest}",
            actor="scientific-binding-fixture",
            expected_canonical_authority_digest=authority.canonical_authority_digest,
        )
        result = self.store.get_head(self.context_id)
        assert result is not None
        return result

    def _bind_science(self, **overrides):
        return self.store.bind_mission_scientific_context(**{**self.arguments, **overrides})

    def test_binding_appends_only_mission_relationship_and_preserves_all_science(self):
        before = self._tables()
        metadata = self.store.read_metadata()
        result = self._bind_science()
        after = self._tables()
        current = self.store.get_head(self.mission_id)
        expected = deep_thaw(self.before_mission.payload)
        expected.update(
            scientific_context_id="science.rh",
            control_revision=expected["control_revision"] + 1,
        )
        self.assertEqual(deep_thaw(current.payload), expected)
        self.assertEqual(current.reference.revision, self.before_mission.reference.revision + 1)
        self.assertEqual(result.changed_heads, (current.reference,))
        self.assertEqual(result.project_commit, metadata["current_project_commit"] + 1)
        self.assertEqual(result.result["canonical_effect"], "none")
        self.assertEqual(result.result["auxiliary_write_count"], 0)
        self.assertEqual(self.store.get_revision(self.before_mission.reference), self.before_mission)
        self.assertEqual(self.store.get_head(self.context_id), self.before_context)
        # Derived root/index tables can change; retained science and Epoch rows cannot.
        owner_prefixes = ("context_", "strategy_", "branch_", "candidate_", "evidence_",
                          "raw_capture", "capture_", "session_", "executive_epoch")
        for name, rows in before.items():
            if name.startswith(owner_prefixes):
                self.assertEqual(after[name], rows, name)
        self.assertEqual(self.store.read_metadata()["canonical_authority_digest"],
                         metadata["canonical_authority_digest"])
        self.store.verify_integrity()

    def test_authorized_and_bound_epochs_reject_without_writes(self):
        epoch_id = self._authorize()
        for bound in (False, True):
            with self.subTest(bound=bound):
                if bound:
                    self._bind(epoch_id)
                before = self._tables()
                with self.assertRaisesRegex(StaleCommandError, "authorized or bound"):
                    self._bind_science()
                self.assertEqual(self._tables(), before)

    def test_epoch_authorized_after_preparation_is_rejected_inside_transaction(self):
        apply_command = self.store._apply_successor_storage_command
        after_authorization = None

        def authorize_before_transaction(**kwargs):
            nonlocal after_authorization
            if kwargs["command_kind"] == "bind_mission_scientific_context":
                self._authorize()
                after_authorization = self._tables()
            return apply_command(**kwargs)

        with mock.patch.object(self.store, "_apply_successor_storage_command",
                               side_effect=authorize_before_transaction):
            with self.assertRaisesRegex(StaleCommandError, "authorized or bound"):
                self._bind_science()
        self.assertIsNotNone(after_authorization)
        self.assertEqual(self._tables(), after_authorization)

    def test_stale_mission_context_authority_and_writer_reject_without_writes(self):
        before = self._tables()
        bad_lease = _issue_writer_lease(
            project_id=self.store.project_id, epoch=self.arguments["lease"].epoch + 1,
            owner=self.arguments["lease"].owner,
        )
        for overrides, error in (
            ({"expected_head_payload_digest": "0" * 64}, StaleCommandError),
            ({"expected_head_revision": 999}, StaleCommandError),
            ({"expected_context_payload_digest": "0" * 64}, StaleCommandError),
            ({"expected_context_revision": 999}, StaleCommandError),
            ({"context_id": "science.absent"}, StaleCommandError),
            ({"expected_canonical_authority_digest": "f" * 64}, StaleCommandError),
            ({"lease": bad_lease}, StaleWriterError),
        ):
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(error):
                    self._bind_science(**overrides)
                self.assertEqual(self._tables(), before)

    def test_wrong_scope_exact_context_projection_rejects_before_transaction(self):
        before = self._tables()
        get_revision = self.store.get_revision
        # The normal writer cannot create these cross-scope targets. Exercise
        # the binding owner's independent rejection without modifying Store rows.
        for field, value in (
            ("project_id", "project.other"),
            ("mission_id", "mission.other"),
            ("context_id", "science.other"),
        ):
            with self.subTest(field=field):
                payload = deep_thaw(self.before_context.payload)
                payload[field] = value
                digest = canonical_payload(payload).sha256
                target = replace(self.before_context, payload=payload, payload_digest=digest)

                def selected_revision(reference):
                    if reference == self.before_context.reference:
                        return target
                    return get_revision(reference)

                with mock.patch.object(self.store, "get_revision", side_effect=selected_revision), \
                     mock.patch.object(
                         self.store, "_apply_successor_storage_command",
                         side_effect=AssertionError("wrong-scope target must not enter transaction"),
                     ) as apply_command:
                    with self.assertRaisesRegex(WorkspaceIntegrityError, "same-scope v3"):
                        self._bind_science(expected_context_payload_digest=digest)
                apply_command.assert_not_called()
                self.assertEqual(self._tables(), before)

    def test_exact_context_revision_must_still_be_current_for_a_new_command(self):
        epoch_id = self._authorize()
        self._bind(epoch_id)
        latest = self._publish_context(epoch_id, replace_existing=True)
        self._fail(epoch_id)
        before = self._tables()
        with self.assertRaises(StaleCommandError):
            self._bind_science()
        self.assertEqual(self._tables(), before)
        result = self._bind_science(
            expected_context_revision=latest.reference.revision,
            expected_context_payload_digest=latest.payload_digest,
        )
        self.assertFalse(result.replayed)
        self.assertEqual(self.store.get_head(self.context_id), latest)

    def test_legacy_context_is_readable_but_not_an_implicit_scientific_binding(self):
        epoch_id = self._authorize()
        self._bind(epoch_id)
        authority = self.interface._direct_epoch_authority(epoch_id)
        mission_ref = deep_thaw(authority.mission_root)
        record = issue_context_revision(
            authority=authority, context_id="context.legacy",
            purpose="Retain an ordinary historical Context.", question="What was known?",
            indispensable_ground=({
                "reference": mission_ref,
                "why": "The exact Mission root defines this historical Context's research purpose.",
            },),
            owner_source_references=({
                "reference": mission_ref,
                "retrieval": "Direct current Mission owner revision.",
                "provenance": "Active Executive Epoch Mission root.",
            },),
            known_omissions=(),
            restricted_uses=(), independence_treatment={"method": "ordinary authoring"},
            restrictions=(), invalidation_conditions=(),
        )
        commit_context_revision(
            self.store, authority=authority, record=record,
            lease=self.store.reissue_writer_lease(attest_current_principal()),
            actor="scientific-binding-fixture",
        )
        self._fail(epoch_id)
        legacy = self.store.get_head(TypedWorkspaceId(IdentityKind.CONTEXT, "context.legacy"))
        before = self._tables()
        with self.assertRaisesRegex(WorkspaceIntegrityError, "same-scope v3"):
            self._bind_science(
                context_id="context.legacy", expected_context_revision=legacy.reference.revision,
                expected_context_payload_digest=legacy.payload_digest,
            )
        self.assertEqual(self._tables(), before)
        self.assertEqual(read_context_revision(self.store, context_id="context.legacy").record, record)

    def test_replay_returns_original_effect_after_context_and_mission_heads_advance(self):
        first = self._bind_science()
        epoch_id = self._authorize()
        self._bind(epoch_id)
        latest_context = self._publish_context(epoch_id, replace_existing=True)
        self._fail(epoch_id)
        first_mission = self.store.get_head(self.mission_id)
        self._bind_science(
            command_id="owner-scientific-context-binding:later", context_id="science.rh",
            expected_head_revision=first_mission.reference.revision,
            expected_head_payload_digest=first_mission.payload_digest,
            expected_context_revision=latest_context.reference.revision,
            expected_context_payload_digest=latest_context.payload_digest,
        )
        latest_mission = self.store.get_head(self.mission_id)
        self.assertGreater(latest_mission.reference.revision, first.changed_heads[0].revision)
        next_epoch = self._authorize()
        self._bind(next_epoch)
        before = self._tables()
        replay = self._bind_science()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.changed_heads, first.changed_heads)
        self.assertEqual(replay.project_commit, first.project_commit)
        self.assertEqual(self._tables(), before)
        with self.assertRaises(CommandConflictError):
            self._bind_science(expected_canonical_authority_digest="f" * 64)
        self.assertEqual(self._tables(), before)
        self.assertEqual(self.store.get_head(self.mission_id), latest_mission)
        self.assertEqual(self.store.get_head(self.context_id), latest_context)
        self.store.verify_integrity()


class MissionScientificInitializationTests(unittest.TestCase):
    """Stopped owner initialization has no model or fabricated Epoch authority."""

    setUpClass = classmethod(migration.MissionModelMigrationTests.setUpClass.__func__)
    _tables = migration.MissionModelMigrationTests._tables
    _authorize = fixtures.DirectMissionInterfaceTests._authorize
    _bind = MissionScientificBindingTests._bind
    _publish_context = MissionScientificBindingTests._publish_context
    _fail = migration.MissionModelMigrationTests._fail

    def setUp(self):
        fixtures.DirectMissionInterfaceTests.setUp(self)
        self.mission_id = TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        self.context_id = TypedWorkspaceId(IdentityKind.CONTEXT, "science.rh")
        self.before_mission = self.store.get_head(self.mission_id)
        self.document = {
            "schema_version": 3, "kind": "first_class_context",
            "project_id": "project.rh", "mission_id": "mission.1", "context_id": "science.rh",
            "purpose": "Keep the parent program and qualified survivors visible.",
            "question": "Which constructions can advance the parent question?",
            "treatments": {"surviving-contour": {
                "question": "Can the valid contour support a wider construction?",
                "account": "The compact-domain estimate survives the normalization correction.",
                "qualifications": ["Compact domain only; the full Jensen implication remains open."],
                "sources": [{
                    "reference": {"kind": "mission", "identity": "mission.1",
                                  "revision": self.before_mission.reference.revision,
                                  "payload_sha256": self.before_mission.payload_digest},
                    "selection": None, "roles": ["recognition"],
                    "why": "Preserve the exact parent Mission question.", "dependency": None,
                }],
            }},
            "exposed_treatments": [], "known_omissions": [],
            "restricted_uses": ["Not canonical mathematical authority."],
            "restrictions": [], "independence_treatment": {"method": "exact qualified source comparison"},
        }
        self._quiesce()
        self.arguments = {
            "expected_mission_revision": self.before_mission.reference.revision,
            "expected_mission_payload_sha256": self.before_mission.payload_digest,
            "expected_canonical_authority_digest": self.store.read_metadata()["canonical_authority_digest"],
            "expected_store_cut": self._cut(), "context_document": self.document,
        }

    def _cut(self):
        metadata = self.store.read_metadata()
        return {"project_commit": metadata["current_project_commit"],
                "current_root_digest": metadata["current_root_digest"],
                "transition_head_digest": metadata["transition_head_digest"],
                "canonical_authority_digest": metadata["canonical_authority_digest"]}

    def _quiesce(self):
        metadata = self.store.read_metadata()
        self.store.release_writer(
            self.store.reissue_writer_lease(attest_current_principal()),
            expected_project_commit=metadata["current_project_commit"],
            expected_root_digest=metadata["current_root_digest"],
            expected_canonical_authority_digest=metadata["canonical_authority_digest"],
            lifecycle="quiesced",
        )

    def _initialize(self, **overrides):
        return self.interface.initialize_scientific_context_from_owner(**{**self.arguments, **overrides})

    def _preflight(self, **overrides):
        values = {**self.arguments, **overrides}
        return self.store.validate_scientific_context_initialization(
            mission_id="mission.1", expected_head_revision=values["expected_mission_revision"],
            expected_head_payload_digest=values["expected_mission_payload_sha256"],
            expected_canonical_authority_digest=values["expected_canonical_authority_digest"],
            expected_store_cut=values["expected_store_cut"], context_document=values["context_document"],
            principal_attestation=attest_current_principal(),
            _connection_override=values.get("_connection_override"),
        )

    def test_initialization_is_one_joint_commit_and_releases_temporary_custody(self):
        before = self._tables()
        first = self._initialize()
        after = self._tables()
        mission = self.store.get_head(self.mission_id)
        context = self.store.get_head(self.context_id)
        expected = deep_thaw(self.before_mission.payload)
        expected.update(scientific_context_id="science.rh", control_revision=expected["control_revision"] + 1)
        self.assertEqual(deep_thaw(mission.payload), expected)
        self.assertEqual(deep_thaw(context.payload), self.document)
        self.assertEqual(context.reference.revision, 1)
        self.assertEqual(first["scientific_context_ref"]["payload_sha256"], canonical_payload(self.document).sha256)
        self.assertEqual(first["project_commit"], self.arguments["expected_store_cut"]["project_commit"] + 1)
        self.assertFalse(first["idempotent"])
        self.assertEqual(len(after["writer_epoch"]), len(before["writer_epoch"]) + 1)
        metadata = self.store.read_metadata()
        self.assertEqual(metadata["lifecycle"], "quiesced")
        self.assertIsNone(metadata["current_writer_epoch"])
        for name, rows in before.items():
            if name.startswith(("strategy_", "branch_", "candidate_", "evidence_", "raw_capture", "capture_", "session_", "executive_epoch")):
                self.assertEqual(after[name], rows, name)
        self.store.verify_integrity()
        replay = self._initialize()
        self.assertTrue(replay["idempotent"])
        self.assertEqual({**replay, "idempotent": False}, first)
        self.assertEqual(self._tables(), after)

    def test_preflight_reads_wal_and_sealed_delete_without_writes(self):
        before = self._tables()
        result = self._preflight()
        self.assertFalse(result["writes"])
        self.assertEqual(result["context_payload_sha256"], canonical_payload(self.document).sha256)
        self.assertEqual(self._tables(), before)
        with closing(sqlite3.connect(self.store.paths.database)) as writable:
            writable.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertEqual(writable.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        database_bytes = self.store.paths.database.read_bytes()
        connection = open_immutable_snapshot_connection(self.store.paths.database)
        try:
            connection.execute("BEGIN")
            sealed = self._preflight(_connection_override=connection)
            self.assertEqual(sealed, result)
            self.assertTrue(connection.in_transaction)
        finally:
            connection.close()
        self.assertEqual(self.store.paths.database.read_bytes(), database_bytes)
        self.assertEqual(self._tables(), before)

    def test_offline_migrated_target_keeps_original_initialization_principal(self):
        import test_workspace_recovery as recovery_fixture
        from test_workspace_schema12_migration import initialize_schema10_fixture
        from research_core import owner_content_access, workspace_store as store_module
        from research_core.migration_executor import execute_offline_migration
        from research_core.workspace_paths import stable_principal_owner_binding
        from research_core.workspace_recovery import RecoveryLifecycle, prepare_offline_migration
        from research_core.workspace_schema import _connect_workspace

        # Reuse the real BackupSet closure fixture, then finish its Epoch under
        # the original principal before running the actual offline migration.
        source = recovery_fixture.WorkspaceRecoveryTests(methodName="runTest")
        source.fixture_project_id = "project.rh"
        source.fixture_mission_id = "mission.1"
        self.addCleanup(source.doCleanups)
        with mock.patch.object(store_module.WorkspaceStore, "initialize_direct_mission_workspace",
                               side_effect=initialize_schema10_fixture):
            source.setUp()
        self.interface = fixtures.MissionInterface.open(
            source.paths.root, expected_project_id=source.project_id,
            expected_mission_id=source.mission_id, canonical_snapshot=source.snapshot,
        )
        self.store = self.interface._store
        metadata = self.store.read_metadata()
        owner = stable_principal_owner_binding(attest_current_principal())
        self.store.claim_writer(
            owner=owner, creation_basis="scientific initialization migration fixture",
            expected_project_commit=metadata["current_project_commit"],
            expected_root_digest=metadata["current_root_digest"],
            expected_canonical_authority_digest=metadata["canonical_authority_digest"],
        )
        self._fail(source.epoch_id)
        self._quiesce()
        backup = source.create_backup("scientific-initialization-source10")
        plan = prepare_offline_migration(
            backup=backup, target_schema_version=12, lifecycle=RecoveryLifecycle.QUIESCED,
        )
        migrated = execute_offline_migration(
            plan=plan, backup=backup, migration_id="scientific-initialization-10to12",
            actor="test.scientific-initialization",
        )

        # Select the real migrated image at this disposable original root.
        # A staging import would append a principal-rebinding writer and hide
        # the migration-tail admission defect this regression must exercise.
        source_uri = f"{migrated.database.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, isolation_level=None)) as source_connection, \
                closing(_connect_workspace(source.paths.database)) as target_connection:
            source_connection.backup(target_connection)
        self.interface = fixtures.MissionInterface.open(
            source.paths.root, expected_project_id=source.project_id,
            expected_mission_id=source.mission_id, canonical_snapshot=source.snapshot,
        )
        self.store = self.interface._store
        self.before_mission = self.store.get_head(self.mission_id)
        document = deep_thaw(self.document)
        document["treatments"]["surviving-contour"]["sources"][0]["reference"].update(
            revision=self.before_mission.reference.revision,
            payload_sha256=self.before_mission.payload_digest,
        )
        self.document = document
        self.arguments = {
            "expected_mission_revision": self.before_mission.reference.revision,
            "expected_mission_payload_sha256": self.before_mission.payload_digest,
            "expected_canonical_authority_digest": self.store.read_metadata()["canonical_authority_digest"],
            "expected_store_cut": self._cut(), "context_document": document,
        }
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            tail = dict(connection.execute("SELECT * FROM writer_epoch ORDER BY epoch DESC LIMIT 1").fetchone())
            predecessor = dict(connection.execute("SELECT * FROM writer_epoch WHERE epoch = ?",
                                                  (tail["predecessor_epoch"],)).fetchone())
        self.assertEqual(tail["owner"], "workspace_schema.offline_migration")
        self.assertEqual(tail["lifecycle"], "quiesced")
        self.assertEqual(predecessor["owner"], owner)
        self.assertEqual(predecessor["lifecycle"], "quiesced")
        before = self._tables()
        with mock.patch("research_core.workspace_store.stable_principal_owner_binding",
                        return_value="os-principal:someone-else"):
            for operation in (self._preflight, self._initialize):
                with self.assertRaises(StaleWriterError):
                    operation()
                self.assertEqual(self._tables(), before)

        # A digest-consistent actor label alone must not stand in for the
        # actual migration transition that links the original principal.
        changed_tail = {**tail, "creation_basis": "unrelated-migration"}
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute("UPDATE writer_epoch SET creation_basis = ?, row_digest = ? WHERE epoch = ?",
                               (changed_tail["creation_basis"], store_module._writer_digest(changed_tail), tail["epoch"]))
            connection.commit()
        try:
            tampered = self._tables()
            for operation in (self._preflight, self._initialize):
                with self.assertRaises((StaleWriterError, WorkspaceIntegrityError)):
                    operation()
                self.assertEqual(self._tables(), tampered)
        finally:
            with closing(sqlite3.connect(self.store.paths.database)) as connection:
                connection.execute("UPDATE writer_epoch SET creation_basis = ?, row_digest = ? WHERE epoch = ?",
                                   (tail["creation_basis"], tail["row_digest"], tail["epoch"]))
                connection.commit()

        with mock.patch.object(owner_content_access, "verify_owner_content_relation",
                               side_effect=AssertionError("initialization repeated the complete index audit")):
            self.assertFalse(self._preflight()["writes"])
            self.assertEqual(self._tables(), before)
            first = self._initialize()
            after = self._tables()
            replay = self._initialize()
        self.assertEqual({**replay, "idempotent": False}, first)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(len(after["writer_epoch"]), len(before["writer_epoch"]) + 1)
        self.assertEqual(self._tables(), after)
        self.assertEqual(deep_thaw(self.store.get_head(self.context_id).payload), document)
        self.assertEqual(self.store.get_head(self.mission_id).payload["scientific_context_id"], self.context_id.value)
        self.assertEqual(self.store.read_metadata()["lifecycle"], "quiesced")
        self.assertIsNone(self.store.read_metadata()["current_writer_epoch"])
        self.store.verify_integrity()

    def test_lost_ack_replays_the_joint_commit_without_a_second_claim(self):
        with mock.patch.object(self.store, "_after_commit", side_effect=RuntimeError("lost initialization acknowledgement")):
            with self.assertRaisesRegex(CommittedCommandAcknowledgementError, "lost initialization acknowledgement"):
                self._initialize()
        before = self._tables()
        replay = self._initialize()
        self.assertTrue(replay["idempotent"])
        self.assertEqual(self._tables(), before)
        self.store.verify_integrity()

    def test_release_failure_rolls_back_both_owners_and_writer_claim(self):
        before = self._tables()
        with mock.patch.object(self.store, "_release_writer_in_transaction", side_effect=RuntimeError("release failed")):
            with self.assertRaisesRegex(RuntimeError, "release failed"):
                self._initialize()
        self.assertEqual(self._tables(), before)
        self.assertIsNone(self.store.get_head(self.context_id))
        self.store.verify_integrity()

    def test_stale_preimages_and_malformed_scope_reject_without_writes(self):
        stale_cut = dict(self.arguments["expected_store_cut"], project_commit=self.arguments["expected_store_cut"]["project_commit"] + 1)
        wrong_context = deep_thaw(self.document)
        wrong_context["mission_id"] = "mission.other"
        unknown_source = deep_thaw(self.document)
        unknown_source["treatments"]["surviving-contour"]["sources"][0]["reference"]["payload_sha256"] = "f" * 64
        for override in (
            {"expected_store_cut": stale_cut}, {"expected_mission_payload_sha256": "f" * 64},
            {"expected_canonical_authority_digest": "f" * 64},
            {"context_document": wrong_context}, {"context_document": unknown_source},
        ):
            with self.subTest(override=override):
                before = self._tables()
                with self.assertRaises((ValueError, StaleCommandError, WorkspaceIntegrityError)):
                    self._initialize(**override)
                self.assertEqual(self._tables(), before)

    def test_foreign_principal_cannot_claim_initialization(self):
        before = self._tables()
        with mock.patch("research_core.workspace_store.stable_principal_owner_binding", return_value="os-principal:someone-else"):
            with self.assertRaises(StaleWriterError):
                self._initialize()
        self.assertEqual(self._tables(), before)

    def test_live_epoch_rejects_even_after_owner_writer_is_quiesced(self):
        self._authorize()
        self._quiesce()
        before = self._tables()
        with self.assertRaisesRegex(StaleCommandError, "authorized or bound"):
            self._initialize(expected_store_cut=self._cut())
        self.assertEqual(self._tables(), before)

    def test_existing_context_identity_is_not_rewritten_or_silently_bound(self):
        epoch = self._authorize()
        self._bind(epoch)
        old_context = self._publish_context(epoch)
        self._fail(epoch)
        self._quiesce()
        before = self._tables()
        for operation in (self._preflight, self._initialize):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(StaleCommandError):
                    operation(expected_store_cut=self._cut())
                self.assertEqual(self._tables(), before)
                self.assertEqual(self.store.get_head(self.context_id), old_context)

    def test_current_sensitive_source_guard_is_not_replaced_by_historical_existence(self):
        document = deep_thaw(self.document)
        source = document["treatments"]["surviving-contour"]["sources"][0]
        source["roles"] = ["reliance"]
        source["dependency"] = {
            "observed_current_reference": {**source["reference"], "payload_sha256": "f" * 64},
            "change_that_matters": "Parent question changed.",
            "dependent_judgment": "This treatment is useful to the parent question.",
        }
        before = self._tables()
        with self.assertRaises((ValueError, StaleCommandError, WorkspaceIntegrityError)):
            self._initialize(context_document=document)
        self.assertEqual(self._tables(), before)

    def test_bound_mission_cannot_be_initialized_again(self):
        self._initialize()
        mission = self.store.get_head(self.mission_id)
        before = self._tables()
        document = deep_thaw(self.document)
        document["context_id"] = "science.other"
        with self.assertRaises((ValueError, WorkspaceIntegrityError)):
            self._initialize(expected_mission_revision=mission.reference.revision,
                expected_mission_payload_sha256=mission.payload_digest,
                expected_store_cut=self._cut(), context_document=document)
        self.assertEqual(self._tables(), before)

    def test_replay_uses_original_owners_after_later_scientific_revisions(self):
        first = self._initialize()
        epoch = self._authorize()
        self._bind(epoch)
        self._publish_context(epoch, replace_existing=True)
        self._fail(epoch)
        before = self._tables()
        replay = self._initialize()
        self.assertEqual({**replay, "idempotent": False}, first)
        self.assertEqual(self._tables(), before)
        self.assertGreater(self.store.get_head(self.context_id).reference.revision, 1)
        self.store.verify_integrity()


if __name__ == "__main__":
    unittest.main()
