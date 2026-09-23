"""Explicit new Mission grants through the real immutable owner transaction."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from dataclasses import replace
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

import test_mission_model_migration as migration  # noqa: E402
from research_core.mission_owner import MissionOwnerError  # noqa: E402
from research_core.research_model import deep_thaw  # noqa: E402
from research_core.workspace_store import (  # noqa: E402
    CommandConflictError,
    StaleCommandError,
    StaleWriterError,
    WorkspaceIntegrityError,
    _issue_writer_lease,
)


class MissionReauthorizationTests(unittest.TestCase):
    setUpClass = classmethod(migration.MissionModelMigrationTests.setUpClass.__func__)
    _authorize = migration.MissionModelMigrationTests._authorize
    _bind = migration.MissionModelMigrationTests._bind
    _tables = migration.MissionModelMigrationTests._tables
    _fail = migration.MissionModelMigrationTests._fail

    def setUp(self):
        migration.MissionModelMigrationTests.setUp(self)
        self.epoch_id = self._authorize()
        self._bind(self.epoch_id)

    def _fence(self, epoch_id=None):
        self.interface.fence_mission_from_owner({
            "threadId": self.goal_thread_id,
            "reason": "mission_consistency_failure",
            "containmentScope": "mission_fence",
        }, executive_epoch_id=epoch_id or self.epoch_id)

    def _prepare(self):
        self._fence()
        self._fail(self.epoch_id)
        self.revoked = self.store.get_head(self.mission_id)
        self.events = self.store.read_executive_epoch_events(executive_epoch_id=self.epoch_id)
        self.arguments.update({
            "expected_head_revision": self.revoked.reference.revision,
            "expected_head_payload_digest": self.revoked.payload_digest,
            "executive_epoch_id": self.epoch_id,
            "root_thread_id": self.goal_thread_id,
            "expected_terminal_event_sha256": self.events[-1]["row_digest"],
            "command_id": "owner-mission-reauthorization:test",
            "actor": "owner-mission-reauthorization-test",
        })

    def _reauthorize(self, **overrides):
        return self.store.reauthorize_mission(**{**self.arguments, **overrides})

    def _facade_request(self):
        return {
            "expected_mission_revision": self.arguments["expected_head_revision"],
            "expected_mission_payload_sha256": self.arguments["expected_head_payload_digest"],
            "executive_epoch_id": self.epoch_id,
            "root_thread_id": self.arguments["root_thread_id"],
            "expected_terminal_event_sha256": self.arguments["expected_terminal_event_sha256"],
            "expected_canonical_authority_digest": self.arguments["expected_canonical_authority_digest"],
        }

    def test_only_exact_new_grant_is_written_and_old_history_survives(self):
        self._prepare()
        before = self._tables()
        outcome = self._reauthorize()
        changed = {name for name, rows in before.items() if self._tables()[name] != rows}
        self.assertEqual(changed, {
            "mission_revision", "mission_head", "project_commit", "transition_journal",
            "command_result", "workspace_metadata", "current_dependency_projection",
        })
        current = self.store.get_head(self.mission_id)
        expected = deep_thaw(self.revoked.payload)
        expected.update(lifecycle="active", effective=True, fence_reason=None,
                        fenced_at=None, control_revision=expected["control_revision"] + 1)
        self.assertEqual(deep_thaw(current.payload), expected)
        self.assertEqual(outcome.changed_heads, (current.reference,))
        self.assertEqual(current.reference.revision, self.revoked.reference.revision + 1)
        self.assertEqual(self.store.get_revision(self.revoked.reference), self.revoked)
        self.assertEqual(self.store.read_executive_epoch_events(executive_epoch_id=self.epoch_id), self.events)
        self.assertEqual(self.store.read_metadata()["canonical_authority_digest"],
                         self.arguments["expected_canonical_authority_digest"])
        self.store.verify_integrity()

    def test_facade_replay_after_astra_fresh_epoch_and_later_fence_never_regrants(self):
        self._prepare()
        request = self._facade_request()
        grant = self.interface.reauthorize_mission_from_owner(**request)
        self.assertFalse(grant["idempotent"])
        self.assertEqual(grant["source_mission_ref"], {
            "revision": self.revoked.reference.revision, "payload_sha256": self.revoked.payload_digest,
        })
        self.assertEqual(grant["terminal_event_sha256"], self.events[-1]["row_digest"])
        self.assertEqual(set(grant), {
            "schema_version", "project_id", "mission_id", "idempotent", "source_mission_ref",
            "target_mission_ref", "executive_epoch_id", "root_thread_id", "terminal_event_sha256",
            "project_commit", "canonical_authority_digest", "canonical_effect", "mathematical_effect", "provider_effect",
        })
        migrated = self.interface.migrate_model_policy_from_owner(
            expected_mission_revision=grant["target_mission_ref"]["revision"],
            expected_mission_payload_sha256=grant["target_mission_ref"]["payload_sha256"],
            expected_canonical_authority_digest=request["expected_canonical_authority_digest"],
        )
        new_epoch = self._authorize()
        self.goal_thread_id = "goal-thread:reauthorized-successor"
        self._bind(new_epoch)
        self.assertNotEqual(new_epoch, self.epoch_id)
        new_chain = self.store.read_executive_epoch_events(executive_epoch_id=new_epoch)
        self.assertEqual(new_chain[0]["event"]["mission_root"]["revision"],
                         migrated["target_mission_ref"]["revision"])
        self.assertEqual(self.store.get_head(self.mission_id).payload["execution_policy"]["model"], "gpt-6-astra")
        for later_fence in (False, True):
            if later_fence:
                self._fence(new_epoch)
                self._fail(new_epoch)
            before = self._tables()
            replay = self.interface.reauthorize_mission_from_owner(**request)
            self.assertEqual(replay, {**grant, "idempotent": True})
            self.assertEqual(self._tables(), before)
        self.assertFalse(self.store.get_head(self.mission_id).payload["effective"])
        self.assertEqual(self.store.read_executive_epoch_events(executive_epoch_id=self.epoch_id), self.events)
        self.assertEqual(self.store.get_revision(self.revoked.reference), self.revoked)
        self.store.verify_integrity()

    def test_fence_without_terminal_and_nonrevoked_terminal_reject(self):
        self._fence()
        fenced = self.store.get_head(self.mission_id)
        arguments = {**self.arguments, "expected_head_revision": fenced.reference.revision,
                     "expected_head_payload_digest": fenced.payload_digest,
                     "executive_epoch_id": self.epoch_id, "root_thread_id": self.goal_thread_id,
                     "expected_terminal_event_sha256": "0" * 64}
        before = self._tables()
        with self.assertRaisesRegex(StaleCommandError, "authorized or bound"):
            self.store.reauthorize_mission(**arguments)
        self.assertEqual(self._tables(), before)
        with self.assertRaises(MissionOwnerError):
            self.store.reauthorize_mission(**{**arguments,
                "expected_head_revision": self.before_mission.reference.revision,
                "expected_head_payload_digest": self.before_mission.payload_digest})
        self.assertEqual(self._tables(), before)

    def test_wrong_incident_stale_authority_and_writer_reject_without_writes(self):
        self._prepare()
        before = self._tables()
        bad_lease = _issue_writer_lease(project_id=self.store.project_id,
            epoch=self.arguments["lease"].epoch + 1, owner=self.arguments["lease"].owner)
        for overrides, error in (
            ({"executive_epoch_id": "epoch:absent"}, StaleCommandError),
            ({"root_thread_id": "root:wrong"}, StaleCommandError),
            ({"expected_terminal_event_sha256": "f" * 64}, StaleCommandError),
            ({"expected_head_payload_digest": "f" * 64}, StaleCommandError),
            ({"expected_head_revision": 999}, StaleCommandError),
            ({"expected_canonical_authority_digest": "f" * 64}, StaleCommandError),
            ({"lease": bad_lease}, StaleWriterError),
        ):
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(error):
                    self._reauthorize(**overrides)
                self.assertEqual(self._tables(), before)

    def test_same_command_id_with_changed_incident_conflicts_and_new_id_cannot_regrant(self):
        self._prepare()
        first = self._reauthorize()
        before = self._tables()
        self.assertTrue(self._reauthorize().replayed)
        with self.assertRaises(CommandConflictError):
            self._reauthorize(root_thread_id="root:changed")
        with self.assertRaises(StaleCommandError):
            self._reauthorize(command_id="different-grant")
        self.assertEqual(self._tables(), before)
        self.assertEqual(self.store.get_head(self.mission_id).reference, first.changed_heads[0])

    def test_transaction_rechecks_after_competing_grant_and_epoch_authorization(self):
        self._prepare()
        apply_command = self.store._apply_successor_storage_command
        after_competitor = None

        def competing_grant_then_epoch(**kwargs):
            nonlocal after_competitor
            with mock.patch.object(self.store, "_apply_successor_storage_command", apply_command):
                self._reauthorize(command_id="competing-explicit-grant")
                self._authorize()
                after_competitor = self._tables()
            return apply_command(**kwargs)

        with mock.patch.object(self.store, "_apply_successor_storage_command", side_effect=competing_grant_then_epoch):
            with self.assertRaises(StaleCommandError):
                self._reauthorize()
        self.assertIsNotNone(after_competitor)
        self.assertEqual(self._tables(), after_competitor)

    def test_broadened_payload_rejects_inside_transaction(self):
        self._prepare()
        apply_command = self.store._apply_successor_storage_command
        before = self._tables()

        def broaden_semantics(**kwargs):
            semantic = deep_thaw(kwargs["semantic_payload"])
            semantic["mission"]["execution_policy"]["model"] = "gpt-6-astra"
            kwargs["semantic_payload"] = semantic
            kwargs["writes"] = (
                replace(kwargs["writes"][0], payload=semantic["mission"]),
            )
            return apply_command(**kwargs)

        with mock.patch.object(self.store, "_apply_successor_storage_command", side_effect=broaden_semantics):
            with self.assertRaises((ValueError, WorkspaceIntegrityError)):
                self._reauthorize()
        self.assertEqual(self._tables(), before)

    def test_valid_old_terminal_cannot_authorize_a_later_fence(self):
        self._prepare()
        self._reauthorize()
        later_epoch = self._authorize()
        self.goal_thread_id = "goal-thread:later-incident"
        self._bind(later_epoch)
        self._fence(later_epoch)
        self._fail(later_epoch)
        later_fence = self.store.get_head(self.mission_id)
        before = self._tables()
        with self.assertRaisesRegex(StaleCommandError, "exact fence successor"):
            self._reauthorize(
                command_id="grant-using-wrong-incident",
                expected_head_revision=later_fence.reference.revision,
                expected_head_payload_digest=later_fence.payload_digest,
            )
        self.assertEqual(self._tables(), before)

    def test_nonfatal_epoch_on_same_mission_root_cannot_claim_later_fatal_fence(self):
        prior_root = self.goal_thread_id
        self._fail(self.epoch_id)
        prior_terminal = self.store.read_executive_epoch_events(
            executive_epoch_id=self.epoch_id
        )[-1]
        fatal_epoch = self._authorize()
        self.goal_thread_id = "goal-thread:fatal-on-unchanged-mission"
        self._bind(fatal_epoch)
        self._fence(fatal_epoch)
        self._fail(fatal_epoch)
        revoked = self.store.get_head(self.mission_id)
        before = self._tables()
        with self.assertRaisesRegex(StaleCommandError, "originating fence"):
            self.store.reauthorize_mission(**{
                **self.arguments,
                "expected_head_revision": revoked.reference.revision,
                "expected_head_payload_digest": revoked.payload_digest,
                "executive_epoch_id": self.epoch_id,
                "root_thread_id": prior_root,
                "expected_terminal_event_sha256": prior_terminal["row_digest"],
            })
        self.assertEqual(self._tables(), before)
        fatal_terminal = self.store.read_executive_epoch_events(
            executive_epoch_id=fatal_epoch
        )[-1]
        granted = self.store.reauthorize_mission(**{
            **self.arguments,
            "expected_head_revision": revoked.reference.revision,
            "expected_head_payload_digest": revoked.payload_digest,
            "executive_epoch_id": fatal_epoch,
            "root_thread_id": self.goal_thread_id,
            "expected_terminal_event_sha256": fatal_terminal["row_digest"],
        })
        self.assertFalse(granted.replayed)
        self.assertEqual(self.store.get_revision(revoked.reference), revoked)
        self.store.verify_integrity()

    def test_terminal_digest_is_only_in_exact_selected_readback(self):
        self._prepare()
        result = self.interface.reconstruct(selected_readback_handles=[self.epoch_id])
        self.assertNotIn("terminal_event_sha256", result["latest_executive_epoch"])
        selected = result["selected_readback"][0]["executive_epoch"]
        self.assertEqual(selected["terminal_event_sha256"], self.events[-1]["row_digest"])


if __name__ == "__main__":
    unittest.main()
