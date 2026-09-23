"""Stopped, Context-only authoring through the existing sole writer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

import test_mission_scientific_binding as binding
from research_core.research_model import deep_thaw
from research_core.workspace_paths import attest_current_principal
from research_core.workspace_store import (
    CommandConflictError, CommittedCommandAcknowledgementError, StaleCommandError,
    StaleWriterError, WorkspaceIntegrityError,
)


class StoppedScientificContextRevisionTests(unittest.TestCase):
    setUpClass = classmethod(binding.MissionScientificInitializationTests.setUpClass.__func__)
    _tables = binding.MissionScientificInitializationTests._tables
    _cut = binding.MissionScientificInitializationTests._cut
    _quiesce = binding.MissionScientificInitializationTests._quiesce
    _initialize = binding.MissionScientificInitializationTests._initialize
    _authorize = binding.MissionScientificInitializationTests._authorize

    def setUp(self):
        binding.MissionScientificInitializationTests.setUp(self)
        self._initialize()
        self.bound_mission = self.store.get_head(self.mission_id)
        self.before_context = self.store.get_head(self.context_id)
        treatment = deep_thaw(self.before_context.payload["treatments"]["surviving-contour"])
        treatment["account"] += " The bounded endpoint qualification is now explicitly retained."
        self.revision_arguments = {
            "mission_id": self.mission_id.value,
            "expected_head_revision": self.bound_mission.reference.revision,
            "expected_head_payload_digest": self.bound_mission.payload_digest,
            "update": {
                "context_id": self.context_id.key,
                "expected_head": {"kind": "context", "identity": self.context_id.value,
                                  "revision": self.before_context.reference.revision,
                                  "payload_sha256": self.before_context.payload_digest},
                "create": None,
                "patch": {"insert": {}, "replace": {"surviving-contour": treatment},
                          "remove": [], "exposure_add": [], "exposure_remove": [],
                          "metadata_replace": {}},
            },
            "expected_store_cut": self._cut(),
            "expected_canonical_authority_digest": self.store.read_metadata()["canonical_authority_digest"],
            "principal_attestation": attest_current_principal(),
            "command_id": "owner-scientific-context-revision:test", "actor": "test-stopped-context-owner",
        }

    def _revise(self, **overrides):
        return self.store.revise_mission_scientific_context(**{**self.revision_arguments, **overrides})

    def test_context_only_success_and_exact_replay_after_a_later_revision(self):
        before = self._tables()
        first = self._revise()
        after = self._tables()
        context = self.store.get_head(self.context_id)
        self.assertEqual(first.changed_heads, (context.reference,))
        self.assertEqual(context.reference.revision, self.before_context.reference.revision + 1)
        self.assertEqual(self.store.get_head(self.mission_id), self.bound_mission)
        self.assertEqual(self.store.get_revision(self.before_context.reference), self.before_context)
        self.assertEqual(len(after["writer_epoch"]), len(before["writer_epoch"]) + 1)
        for table, rows in before.items():
            if table.startswith(("mission_", "strategy_", "branch_", "candidate_", "evidence_",
                                 "raw_capture", "capture_", "session_", "executive_epoch")):
                self.assertEqual(after[table], rows, table)
        self.assertEqual(self.store.read_metadata()["lifecycle"], "quiesced")
        self.assertIsNone(self.store.read_metadata()["current_writer_epoch"])
        self.assertTrue(self._revise().replayed)
        self.assertEqual(self._tables(), after)
        later = deep_thaw(self.revision_arguments["update"])
        later["expected_head"].update(revision=context.reference.revision, payload_sha256=context.payload_digest)
        later["patch"]["replace"]["surviving-contour"]["account"] += " A later clarification remains distinct."
        self._revise(update=later, expected_store_cut=self._cut(), command_id="owner-scientific-context-revision:later")
        current = self._tables()
        replay = self._revise()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.project_commit, first.project_commit)
        self.assertEqual(replay.changed_heads, first.changed_heads)
        self.assertEqual(self._tables(), current)
        with self.assertRaises(CommandConflictError):
            self._revise(actor="different-command-actor")
        self.assertEqual(self._tables(), current)
        self.store.verify_integrity()

    def test_stale_scope_source_and_noop_inputs_leave_no_writer_or_revision(self):
        stale_context = deep_thaw(self.revision_arguments["update"])
        stale_context["expected_head"]["payload_sha256"] = "f" * 64
        missing_source = deep_thaw(self.revision_arguments["update"])
        missing_source["patch"]["replace"]["surviving-contour"]["sources"][0]["reference"]["payload_sha256"] = "f" * 64
        noop = deep_thaw(self.revision_arguments["update"])
        noop["patch"]["replace"] = {}
        for overrides in (
            {"expected_head_payload_digest": "f" * 64}, {"update": stale_context},
            {"update": missing_source}, {"update": noop},
            {"expected_store_cut": {**self._cut(), "project_commit": self._cut()["project_commit"] + 1}},
            {"expected_canonical_authority_digest": "f" * 64},
        ):
            with self.subTest(overrides=tuple(overrides)):
                before = self._tables()
                with self.assertRaises((ValueError, StaleCommandError, StaleWriterError, WorkspaceIntegrityError)):
                    self._revise(**overrides)
                self.assertEqual(self._tables(), before)

    def test_foreign_principal_and_live_epoch_cannot_claim_stopped_authority(self):
        before = self._tables()
        with mock.patch("research_core.workspace_store.stable_principal_owner_binding", return_value="os-principal:other"):
            with self.assertRaises(StaleWriterError):
                self._revise()
        self.assertEqual(self._tables(), before)
        self._authorize()
        self._quiesce()
        before = self._tables()
        with self.assertRaisesRegex(StaleCommandError, "authorized or bound"):
            self._revise(expected_store_cut=self._cut())
        self.assertEqual(self._tables(), before)

    def test_release_failure_rolls_back_and_lost_ack_replays_one_closed_claim(self):
        before = self._tables()
        with mock.patch.object(self.store, "_release_writer_in_transaction", side_effect=RuntimeError("release failed")):
            with self.assertRaisesRegex(RuntimeError, "release failed"):
                self._revise()
        self.assertEqual(self._tables(), before)
        with mock.patch.object(self.store, "_after_commit", side_effect=RuntimeError("lost acknowledgement")):
            with self.assertRaises(CommittedCommandAcknowledgementError):
                self._revise()
        after = self._tables()
        self.assertTrue(self._revise().replayed)
        self.assertEqual(self._tables(), after)
        self.assertEqual(self.store.read_metadata()["lifecycle"], "quiesced")
        self.assertIsNone(self.store.read_metadata()["current_writer_epoch"])


if __name__ == "__main__":
    unittest.main()
