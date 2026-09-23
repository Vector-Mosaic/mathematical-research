from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core.workspace_schema import canonical_payload  # noqa: E402
from research_core.workspace_store import (  # noqa: E402
    WorkspaceIntegrityError,
    _REVISION_DIGEST_COLUMNS,
    _row_digest,
)
from test_mission_interface_direct import _direct_genesis_fixture  # noqa: E402


class DirectRecoveryStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.interface = MissionInterface.initialize_from_owner(
            Path(self.temporary.name) / "mission",
            project_id="project.rh",
            mission_id="mission.1",
            canonical_snapshot=self.canonical,
            genesis_seed=_direct_genesis_fixture(),
            owner_command_id="direct-recovery.genesis",
        )
        self.store = self.interface._store

    def _read(self, cut_project_commit: int | None):
        with self.store.direct_recovery_read_scope():
            return self.store.read_direct_recovery_facts(
                cut_project_commit=cut_project_commit
            )

    def test_no_cut_genesis_preserves_historical_and_current_direct_heads(self) -> None:
        facts = self._read(None)

        self.assertEqual(facts["observed_project_commit"], 1)
        self.assertIsNone(facts["cut_project_commit"])
        self.assertEqual(facts["raw_capture_origins"], ())
        self.assertEqual(facts["terminal_epochs"], ())
        self.assertEqual(
            tuple(item["command_kind"] for item in facts["post_cut_transitions"]),
            ("initialize_direct_mission_genesis",),
        )
        histories = {
            (item["kind"], item["identity"]): item
            for item in facts["head_history"]
        }
        self.assertEqual(
            histories[("mission", "mission.1")]["revisions"],
            ({"revision": 1, "project_commit": 1},),
        )
        for key in (
            ("branch", "branch.theta"),
            ("strategy", "strategy.theta.1"),
        ):
            self.assertEqual(histories[key]["at_cut_revision"], None)
            self.assertEqual(histories[key]["current_revision"], 1)
            self.assertEqual(
                histories[key]["revisions"],
                ({"revision": 1, "project_commit": 1},),
            )

    def test_cut_selects_at_cut_heads_and_only_later_transition_facts(self) -> None:
        goal_workspace = Path(self.temporary.name) / "cut-goal"
        goal_workspace.mkdir()
        authorized = self.interface.authorize_executive_epoch_from_owner(
            {"expected_cut": self.interface.host_snapshot()["authorization_cut"]}
        )
        self.interface.bind_executive_epoch_from_owner(
            {
                "executiveEpochId": authorized["executive_epoch_id"],
                "rootThreadId": "goal-thread:direct-recovery-cut",
                "workspaceRoot": str(goal_workspace.resolve()),
            }
        )
        facts = self._read(1)
        histories = {
            (item["kind"], item["identity"]): item
            for item in facts["head_history"]
        }
        self.assertEqual(
            histories[("branch", "branch.theta")]["at_cut_revision"], 1
        )
        self.assertEqual(
            histories[("strategy", "strategy.theta.1")]["at_cut_revision"], 1
        )
        self.assertEqual(
            tuple(item["project_commit"] for item in facts["post_cut_transitions"]),
            (2, 3),
        )

    def test_scope_reuses_one_snapshot_and_rejects_nesting_and_writes(self) -> None:
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "require direct_recovery_read_scope",
        ):
            self.store.read_direct_recovery_facts(cut_project_commit=None)

        with self.store.direct_recovery_read_scope():
            with self.store._connection(read_only=True) as first:
                with self.store._connection(read_only=True) as second:
                    self.assertIs(first, second)
            with self.assertRaisesRegex(WorkspaceIntegrityError, "rejects Store writes"):
                with self.store._connection():
                    pass
            with self.assertRaisesRegex(WorkspaceIntegrityError, "cannot be nested"):
                with self.store.direct_recovery_read_scope():
                    pass
            with self.assertRaisesRegex(WorkspaceIntegrityError, "overlap recovery"):
                with self.store.direct_checkpoint_write_scope():
                    pass

        with self.store.direct_checkpoint_write_scope():
            pass

    def test_reports_raw_capture_and_terminal_epoch_origin_commits(self) -> None:
        goal_workspace = Path(self.temporary.name) / "goal"
        goal_workspace.mkdir()
        goal_thread_id = "goal-thread:direct-recovery"
        authorized = self.interface.authorize_executive_epoch_from_owner(
            {"expected_cut": self.interface.host_snapshot()["authorization_cut"]}
        )
        epoch_id = str(authorized["executive_epoch_id"])
        self.interface.bind_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "rootThreadId": goal_thread_id,
                "workspaceRoot": str(goal_workspace.resolve()),
            }
        )
        captured = self.interface.capture_native_material_observation(
            {
                "observationId": "observation.direct-recovery",
                "materialKind": "output",
                "content": "Readable research output retained for recovery.",
                "rootThreadId": goal_thread_id,
                "parentThreadId": goal_thread_id,
                "childThreadId": "child-thread:direct-recovery",
            },
            executive_epoch_id=epoch_id,
        )
        self.assertEqual(captured["custody_status"], "store_cas_verified")
        failed = self.interface.record_direct_failed_executive_epoch_from_owner(
            {
                "executiveEpochId": epoch_id,
                "reconciliation": {
                    "stage": "goal_runtime",
                    "failure_reason": "Focused recovery fixture terminal.",
                },
            }
        )
        self.assertEqual(failed["state"], "failed_before_checkpoint")

        facts = self._read(1)

        self.assertEqual(len(facts["raw_capture_origins"]), 1)
        capture = facts["raw_capture_origins"][0]
        self.assertEqual(capture["mission_id"], "mission.1")
        self.assertEqual(capture["executive_epoch_id"], epoch_id)
        self.assertEqual(capture["capture_kind"], "output")
        self.assertEqual(
            facts["terminal_epochs"],
            (
                {
                    "executive_epoch_id": epoch_id,
                    "mission_id": "mission.1",
                    "terminal_kind": "failed_before_checkpoint",
                    "project_commit": facts["observed_project_commit"],
                },
            ),
        )
        self.assertLess(
            capture["project_commit"],
            facts["terminal_epochs"][0]["project_commit"],
        )
        post_cut_commits = tuple(
            item["project_commit"] for item in facts["post_cut_transitions"]
        )
        self.assertEqual(
            post_cut_commits,
            tuple(range(2, facts["observed_project_commit"] + 1)),
        )
        capture_transition = next(
            item
            for item in facts["post_cut_transitions"]
            if item["project_commit"] == capture["project_commit"]
        )
        self.assertEqual(capture_transition["command_kind"], "commit_raw_capture")
        self.assertIn(
            {
                "table": "raw_capture",
                "primary_key": {"capture_id": capture["capture_id"]},
            },
            capture_transition["auxiliary_writes"],
        )

    def test_journal_tamper_is_rejected(self) -> None:
        connection = sqlite3.connect(self.store.paths.database)
        try:
            connection.execute(
                "UPDATE transition_journal SET changed_heads_json = '[]' "
                "WHERE sequence_no = 1"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "transition journal",
        ):
            self._read(None)

    def test_unjournaled_imported_owner_is_rejected(self) -> None:
        payload = canonical_payload(
            {
                "schema_version": 1,
                "kind": "context_manifest",
                "project_id": "project.rh",
                "mission_id": "mission.1",
                "context_id": "context.imported",
            }
        )
        revision_values = {
            "object_id": "context.imported",
            "revision": 1,
            "payload_json": payload.text,
            "payload_digest": payload.sha256,
            "predecessor_revision": None,
            "created_actor": "imported-baseline",
            "created_session_id": None,
            "created_evidence_id": None,
            "authorization_json": "{}",
            "terminal_history_json": "{}",
            "created_at": "2026-08-23T00:00:00Z",
        }
        self.assertEqual(set(revision_values), set(_REVISION_DIGEST_COLUMNS))
        connection = sqlite3.connect(self.store.paths.database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO context_revision(object_id, revision, payload_json, "
                "payload_digest, predecessor_revision, created_actor, "
                "created_session_id, created_evidence_id, authorization_json, "
                "terminal_history_json, created_at, row_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*revision_values.values(), _row_digest("context_revision", revision_values)),
            )
            connection.execute(
                "INSERT INTO context_head(object_id, revision, payload_digest, "
                "project_commit_no) VALUES (?, ?, ?, ?)",
                ("context.imported", 1, payload.sha256, 1),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(
            WorkspaceIntegrityError,
            "absent from authoritative journal history",
        ):
            self._read(None)


if __name__ == "__main__":
    unittest.main()
