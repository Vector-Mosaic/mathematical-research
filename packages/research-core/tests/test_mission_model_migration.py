"""Stopped owner model migration through the real Mission Store transaction."""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

import test_mission_interface_direct as fixtures  # noqa: E402
from research_core.mission_owner import MissionOwnerError  # noqa: E402
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.workspace_paths import attest_current_principal  # noqa: E402
from research_core.workspace_schema import (  # noqa: E402
    IdentityKind,
    TypedWorkspaceId,
)
from research_core.workspace_store import (  # noqa: E402
    CommandConflictError,
    StaleCommandError,
    StaleWriterError,
    _issue_writer_lease,
)


class MissionModelMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        loaded = fixtures.load_canonical_snapshot(fixtures.REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def setUp(self) -> None:
        make_seed = fixtures._direct_genesis_fixture

        def historical_seed():
            seed = make_seed()
            seed["mission"]["execution_policy"]["model"] = "gpt-5.6-sol"
            return seed

        with mock.patch.object(fixtures, "_direct_genesis_fixture", historical_seed):
            fixtures.DirectMissionInterfaceTests.setUp(self)
        self.mission_id = TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        self.before_mission = self.store.get_head(self.mission_id)
        assert self.before_mission is not None
        self.arguments = {
            "mission_id": "mission.1",
            "expected_head_revision": self.before_mission.reference.revision,
            "expected_head_payload_digest": self.before_mission.payload_digest,
            "lease": self.store.reissue_writer_lease(attest_current_principal()),
            "command_id": "owner-model-migration:test-sol-to-astra",
            "actor": "owner-model-migration-test",
            "expected_canonical_authority_digest": self.store.read_metadata()[
                "canonical_authority_digest"
            ],
        }

    _authorize = fixtures.DirectMissionInterfaceTests._authorize
    _bind = fixtures.DirectMissionInterfaceTests._bind
    _capture_pending_material = fixtures.DirectMissionInterfaceTests._capture_pending_material
    _request = staticmethod(fixtures.DirectMissionInterfaceTests._request)

    def _tables(self):
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            return {
                name: tuple(connection.execute(f'SELECT * FROM "{name}"'))
                for name in tables
            }

    def _migrate(self, **overrides):
        return self.store.migrate_mission_model_policy(**{**self.arguments, **overrides})

    def _fail(self, epoch_id):
        result = self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "reconciliation": {
                    "stage": "goal_runtime",
                    "failure_reason": "provider-free terminal fixture",
                },
            }
        )
        self.assertEqual(result["state"], "failed_before_checkpoint")

    def test_one_mission_revision_and_no_mathematical_or_epoch_effect(self):
        before = self._tables()
        metadata = self.store.read_metadata()
        result = self._migrate()
        after = self._tables()
        changed = {name for name in before if before[name] != after[name]}
        self.assertEqual(
            changed,
            {
                "mission_revision", "mission_head", "project_commit",
                "transition_journal", "command_result", "workspace_metadata",
                "current_dependency_projection",
            },
        )
        current = self.store.get_head(self.mission_id)
        expected = deep_thaw(self.before_mission.payload)
        expected["execution_policy"]["model"] = "gpt-6-astra"
        expected["control_revision"] += 1
        self.assertEqual(deep_thaw(current.payload), expected)
        self.assertEqual(current.reference.revision, self.before_mission.reference.revision + 1)
        self.assertEqual(result.changed_heads, (current.reference,))
        self.assertEqual(result.project_commit, metadata["current_project_commit"] + 1)
        self.assertEqual(result.result["canonical_effect"], "none")
        self.assertEqual(result.result["auxiliary_write_count"], 0)
        self.assertEqual(
            self.store.read_metadata()["canonical_authority_digest"],
            metadata["canonical_authority_digest"],
        )
        self.assertEqual(
            self.store.get_revision(self.before_mission.reference), self.before_mission
        )
        historical_read = self.interface.reconstruct(
            selected_readback_handles=["mission:mission.1@1"]
        )["selected_readback"]
        self.assertEqual(
            historical_read[0]["document"], deep_thaw(self.before_mission.payload)
        )
        self.store.verify_integrity()

    def test_exact_replay_precedes_stale_head_and_new_active_epoch(self):
        first = self._migrate()
        successor_epoch = self._authorize()
        self._bind(successor_epoch)
        before = self._tables()
        replay = self._migrate()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.project_commit, first.project_commit)
        self.assertEqual(replay.changed_heads, first.changed_heads)
        self.assertEqual(self._tables(), before)
        with self.assertRaises(CommandConflictError):
            self._migrate(expected_canonical_authority_digest="f" * 64)
        self.assertEqual(self._tables(), before)

    def test_owner_facade_returns_exact_result_and_replays_after_new_epoch(self):
        request = {
            "expected_mission_revision": self.arguments["expected_head_revision"],
            "expected_mission_payload_sha256": self.arguments[
                "expected_head_payload_digest"
            ],
            "expected_canonical_authority_digest": self.arguments[
                "expected_canonical_authority_digest"
            ],
        }
        result = self.interface.migrate_model_policy_from_owner(**request)
        target = self.store.get_head(self.mission_id)
        self.assertEqual(result, {
            "schema_version": "mathematical_research.mission_model_policy_migration_result.v1",
            "project_id": self.store.project_id,
            "mission_id": "mission.1",
            "idempotent": False,
            "source_mission_ref": {
                "revision": self.before_mission.reference.revision,
                "payload_sha256": self.before_mission.payload_digest,
            },
            "target_mission_ref": {
                "revision": target.reference.revision,
                "payload_sha256": target.payload_digest,
            },
            "model": "gpt-6-astra",
            "reasoning_effort": "ultra",
            "project_commit": self.store.read_metadata()["current_project_commit"],
            "canonical_authority_digest": request["expected_canonical_authority_digest"],
            "canonical_effect": "none",
            "mathematical_effect": "none",
            "provider_effect": "none",
        })
        epoch_id = self._authorize()
        self._bind(epoch_id)
        before = self._tables()
        replay = self.interface.migrate_model_policy_from_owner(**request)
        self.assertEqual(replay, {**result, "idempotent": True})
        self.assertEqual(self._tables(), before)
        self._fail(epoch_id)
        self.interface.fence_mission_from_owner({
            "threadId": self.goal_thread_id,
            "reason": "unknown_effect",
            "containmentScope": "mission_fence",
        }, executive_epoch_id=epoch_id)
        self.assertGreater(self.store.get_head(self.mission_id).reference.revision,
                           target.reference.revision)
        before = self._tables()
        replay = self.interface.migrate_model_policy_from_owner(**request)
        self.assertEqual(replay, {**result, "idempotent": True})
        self.assertEqual(self._tables(), before)

    def test_authorized_and_bound_epochs_reject_without_writes(self):
        epoch_id = self._authorize()
        for bound in (False, True):
            with self.subTest(bound=bound):
                if bound:
                    self._bind(epoch_id)
                before = self._tables()
                with self.assertRaisesRegex(StaleCommandError, "authorized or bound"):
                    self._migrate()
                self.assertEqual(self._tables(), before)

    def test_epoch_admitted_after_predecessor_read_still_blocks_migration(self):
        apply_command = self.store._apply_successor_storage_command
        after_authorization = None

        def authorize_before_transaction(**kwargs):
            nonlocal after_authorization
            if kwargs["command_kind"] == "migrate_mission_model_policy":
                self._authorize()
                after_authorization = self._tables()
            return apply_command(**kwargs)

        with mock.patch.object(
            self.store,
            "_apply_successor_storage_command",
            side_effect=authorize_before_transaction,
        ):
            with self.assertRaisesRegex(StaleCommandError, "authorized or bound"):
                self._migrate()
        self.assertIsNotNone(after_authorization)
        self.assertEqual(self._tables(), after_authorization)

    def test_reconciled_failure_keeps_history_and_next_epoch_binds_astra(self):
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._fail(epoch_id)
        old_events = self.store.read_executive_epoch_events(executive_epoch_id=epoch_id)
        self._migrate()
        new_epoch = self._authorize()
        new_events = self.store.read_executive_epoch_events(executive_epoch_id=new_epoch)
        current = self.store.get_head(self.mission_id)
        self.assertNotEqual(new_epoch, epoch_id)
        self.assertEqual(new_events[0]["event"]["mission_root"]["revision"], current.reference.revision)
        self.assertEqual(self.store.read_executive_epoch_events(executive_epoch_id=epoch_id), old_events)
        self.assertEqual(old_events[-1]["event_kind"], "failed_before_checkpoint")
        self.store.verify_integrity()

    def test_checkpoint_survives_and_successor_carries_exact_predecessor(self):
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._capture_pending_material(epoch_id)
        result = self.interface.execute_semantic_operation(
            self._request(fixtures.CHECKPOINT, {}), executive_epoch_id=epoch_id
        )
        self.assertEqual(result["status"], "completed", result)
        checkpoint = self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id)
        self._migrate()
        new_epoch = self._authorize()
        self.assertEqual(self.store.read_continuation_checkpoint(executive_epoch_id=epoch_id), checkpoint)
        new_events = self.store.read_executive_epoch_events(executive_epoch_id=new_epoch)
        self.assertEqual(new_events[0]["event"]["predecessor_checkpoint"], {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "payload_sha256": checkpoint["payload_digest"],
        })
        self.store.verify_integrity()

    def test_historical_sol_orientation_and_fence_remain_supported(self):
        orientation = self.interface.executive_orientation()
        self.assertTrue(orientation)
        reconstruction = self.interface.reconstruct(selected_readback_handles=["mission:mission.1@1"])
        self.assertIn("gpt-5.6-sol", json.dumps(deep_thaw(reconstruction)))
        epoch_id = self._authorize()
        self._bind(epoch_id)
        self._fail(epoch_id)
        self.interface.fence_mission_from_owner({
            "threadId": self.goal_thread_id,
            "reason": "unknown_effect",
            "containmentScope": "mission_fence",
        }, executive_epoch_id=epoch_id)
        fenced = self.store.get_head(self.mission_id)
        self.assertEqual(fenced.payload["execution_policy"]["model"], "gpt-5.6-sol")
        before = self._tables()
        with self.assertRaises(StaleCommandError):
            self._migrate()
        with self.assertRaises(MissionOwnerError):
            self._migrate(expected_head_revision=fenced.reference.revision,
                          expected_head_payload_digest=fenced.payload_digest)
        self.assertEqual(self._tables(), before)
        self.store.verify_integrity()

    def test_stale_predecessor_authority_and_writer_reject_without_writes(self):
        before = self._tables()
        bad_lease = _issue_writer_lease(
            project_id=self.store.project_id,
            epoch=self.arguments["lease"].epoch + 1,
            owner=self.arguments["lease"].owner,
        )
        for overrides, error in (
            ({"expected_head_payload_digest": "0" * 64}, StaleCommandError),
            ({"expected_head_revision": 999}, StaleCommandError),
            ({"expected_canonical_authority_digest": "f" * 64}, StaleCommandError),
            ({"lease": bad_lease}, StaleWriterError),
        ):
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(error):
                    self._migrate(**overrides)
                self.assertEqual(self._tables(), before)
        self._migrate()
        migrated = self._tables()
        with self.assertRaises(StaleCommandError):
            self._migrate(command_id="different-command-for-stale-predecessor")
        self.assertEqual(self._tables(), migrated)


if __name__ == "__main__":
    unittest.main()
