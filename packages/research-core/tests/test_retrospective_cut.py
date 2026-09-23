from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
for candidate in (PACKAGE_ROOT, TEST_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from test_mission_interface_direct import _direct_genesis_fixture  # noqa: E402
from research_core.canonical_snapshot import (  # noqa: E402
    CANONICAL_STATE_REPO_PATH,
    load_canonical_snapshot,
)
from research_core.evidence_store import EvidenceCAS  # noqa: E402
from research_core.mission_evidence import (  # noqa: E402
    RawCaptureArtifactFileInput,
    RawCaptureArtifactInput,
    commit_raw_capture,
    prepare_raw_capture,
)
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core.mission_operation_contract import (  # noqa: E402
    CHECKPOINT,
    MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
    RECORD_STRATEGY,
)
from research_core.retrospective_cut import (  # noqa: E402
    FrozenRetrospectiveCut,
    RetrospectiveCutError,
    material_projection,
)
from research_core.workspace_paths import attest_current_principal  # noqa: E402


class FrozenRetrospectiveCutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.interface = MissionInterface.initialize_from_owner(
            self.root / "live",
            project_id="project.rh",
            mission_id="mission.1",
            canonical_snapshot=self.canonical,
            genesis_seed=_direct_genesis_fixture(),
            owner_command_id="fixture.retrospective.genesis",
        )
        self.store = self.interface._store
        self.cas = EvidenceCAS(self.store.paths)
        goal = self.root / "goal"
        goal.mkdir()
        self.epoch = self.interface.authorize_executive_epoch_from_owner(
            {"expected_cut": self.interface.host_snapshot()["authorization_cut"]}
        )["executive_epoch_id"]
        self.interface.bind_executive_epoch_from_owner({
            "executiveEpochId": self.epoch,
            "rootThreadId": "thread:retrospective-fixture",
            "workspaceRoot": str(goal.resolve()),
        })
        self.clear_bytes = b"An intermediate fixture theorem, not an RH claim.\n"
        self.quarantined_bytes = b"Restricted fixture material must never be projected.\n"
        restricted = self.root / "restricted.txt"
        restricted.write_bytes(self.quarantined_bytes)
        record = prepare_raw_capture(
            self.store,
            cas=self.cas,
            mission_id="mission.1",
            executive_epoch_id=self.epoch,
            capture_kind="output",
            observation_id="observation.retrospective.fixture",
            assignment_id="child.retrospective.fixture",
            provenance={"kind": "test_fixture"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactInput(
                    role="result", logical_name="result.txt", content_bytes=self.clear_bytes,
                    media_type="text/plain", encoding="utf-8",
                ),
                RawCaptureArtifactFileInput(
                    role="evidence_only", logical_name="restricted.txt",
                    source_path=restricted.resolve(),
                    sha256=hashlib.sha256(self.quarantined_bytes).hexdigest(),
                    byte_length=len(self.quarantined_bytes), media_type="text/plain",
                    encoding="utf-8", quarantine_reason="opaque_restricted",
                ),
            ),
        )
        commit_raw_capture(
            self.store, cas=self.cas, record=record,
            lease=self.store.reissue_writer_lease(attest_current_principal()), actor="fixture",
        )
        self.capture_id = record.capture_id
        self._execute(RECORD_STRATEGY, {
            "mission_continuation": "continue",
            "integrated_comparison": "A second fixture Strategy retains the first revision.",
            "reconsideration_conditions": [{"condition": "A consequential mathematical change."}],
        })
        self._execute(CHECKPOINT, {})
        checkpoint = self.store.read_continuation_checkpoint(executive_epoch_id=self.epoch)
        assert checkpoint is not None
        self.cut = self.root / "frozen"
        self.cut.mkdir()
        report = self.store.export_verified_backup(self.cut / "workspace.sqlite3")
        shutil.copytree(self.store.paths.root / "cas", self.cut / "cas")
        self.envelope = {
            "mission_id": "mission.1", "selected_release_sha": "b" * 40,
            "checkpoint_ref": {"checkpoint_id": checkpoint["checkpoint_id"],
                               "payload_sha256": checkpoint["payload_digest"]},
            "store_backup": {**asdict(report), "target": str(report.target)},
        }
        self.expected = {
            "mission_id": "mission.1", "selected_release_sha": "b" * 40,
            "checkpoint_id": checkpoint["checkpoint_id"], "project_commit": report.project_commit,
        }
        self._write_envelope()

    def _execute(self, operation: str, semantic_input: dict) -> dict:
        result = self.interface.execute_semantic_operation({
            "schema_version": MISSION_SEMANTIC_REQUEST_SCHEMA_VERSION,
            "operation": operation, "input": semantic_input,
        }, executive_epoch_id=self.epoch)
        self.assertEqual(result["status"], "completed", result)
        return dict(result)

    def _write_envelope(self) -> None:
        (self.cut / "cut.json").write_text(json.dumps(self.envelope), encoding="utf-8")

    def _reader(self) -> FrozenRetrospectiveCut:
        return FrozenRetrospectiveCut(self.cut, expected_binding=self.expected)

    @staticmethod
    def _tree_bytes(root: Path) -> dict[str, bytes]:
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def test_frozen_history_is_readable_without_live_owner_or_any_state_change(self) -> None:
        # An informational export target must never redirect the frozen reader.
        self.envelope["store_backup"]["target"] = str(self.store.paths.root / "workspace.sqlite3")
        self._write_envelope()
        frozen_before = self._tree_bytes(self.cut)
        live_before = self._tree_bytes(self.store.paths.root)
        canonical_path = REPO_ROOT / CANONICAL_STATE_REPO_PATH
        canonical_before = canonical_path.read_bytes()
        with mock.patch("research_core.workspace_store.WorkspaceStore.open",
                        side_effect=AssertionError("cannot open a live Workspace")):
            with self._reader() as reader:
                inventory = reader.inventory()
                handles = {item["handle"] for item in inventory["retained_history"]}
                self.assertIn("strategy:strategy.theta.1@1", handles)
                self.assertIn("strategy:strategy.theta.1@2", handles)
                historical = reader.read("strategy:strategy.theta.1@1")
                self.assertEqual(historical["row"]["revision"], 1)
                clear = reader.read(f"capture-artifact:{self.capture_id}#0")
                self.assertEqual(clear["artifact"]["content"], self.clear_bytes.decode())
                blocked = reader.read(f"capture-artifact:{self.capture_id}#1")
                self.assertEqual(blocked["artifact"]["status"], "unavailable")
                self.assertEqual(blocked["artifact"]["reason"], "quarantined")
                self.assertNotIn("content", blocked["artifact"])
                tables = {item["handle"] for item in inventory["tables"]}
                self.assertIn("table:mission_revision", tables)
                self.assertIn("table:continuation_checkpoint", tables)
                projection = material_projection(reader)
                serialized = json.dumps(projection)
                self.assertNotIn(self.quarantined_bytes.decode().strip(), serialized)
                self.assertEqual(projection["coverage"]["canonical_effect"], "none")
                self.assertEqual(projection["coverage"]["readable_blob_count"], 1)
                metadata = reader.read("table:workspace_metadata")["rows"][0]
                self.assertEqual(metadata["root_identity"], self.envelope["store_backup"]["root_identity"])
                with self.assertRaises(sqlite3.OperationalError):
                    reader._connection.execute("DELETE FROM workspace_metadata")
        self.assertEqual(self._tree_bytes(self.cut), frozen_before)
        self.assertEqual(self._tree_bytes(self.store.paths.root), live_before)
        self.assertEqual(canonical_path.read_bytes(), canonical_before)

    def test_later_live_work_is_outside_frozen_population(self) -> None:
        with self._reader() as reader:
            before = material_projection(reader)
        successor = self.interface.authorize_executive_epoch_from_owner(
            {"expected_cut": self.interface.host_snapshot()["authorization_cut"]}
        )
        self.assertNotEqual(successor["executive_epoch_id"], self.epoch)
        self.assertGreater(self.store.read_metadata()["current_project_commit"], self.expected["project_commit"])
        with self._reader() as reader:
            self.assertEqual(material_projection(reader), before)

    def test_missing_frozen_blob_is_gap_never_live_source_fallback(self) -> None:
        digest = hashlib.sha256(self.clear_bytes).hexdigest()
        original = self.cas.path_for_digest(digest)
        frozen = self.cut / original.relative_to(self.store.paths.root)
        frozen.chmod(stat.S_IWRITE | stat.S_IREAD)
        frozen.unlink()
        self.assertTrue(original.is_file())
        with self._reader() as reader:
            material = reader.read(f"blob:{digest}")
            self.assertEqual(material["status"], "unavailable")
            self.assertNotIn("content", material)
            self.assertEqual(reader.coverage()["readable_blob_count"], 0)

    def test_binding_snapshot_and_exact_handle_boundaries(self) -> None:
        with self.assertRaises(RetrospectiveCutError):
            FrozenRetrospectiveCut(self.cut)
        with self._reader() as reader:
            for handle in ("table:../live", "blob:../live", "capture:absent"):
                with self.subTest(handle=handle), self.assertRaises(RetrospectiveCutError):
                    reader.read(handle)
        self.envelope["checkpoint_ref"]["payload_sha256"] = "0" * 64
        self._write_envelope()
        with self.assertRaisesRegex(RetrospectiveCutError, "checkpoint"):
            self._reader()
        self.envelope["store_backup"]["backup_sha256"] = "0" * 64
        self._write_envelope()
        with self.assertRaisesRegex(RetrospectiveCutError, "SQLite bytes"):
            self._reader()

    def test_linked_frozen_cas_cannot_escape_to_live_bytes(self) -> None:
        digest = hashlib.sha256(self.clear_bytes).hexdigest()
        original = self.cas.path_for_digest(digest)
        frozen = self.cut / original.relative_to(self.store.paths.root)
        frozen.chmod(stat.S_IWRITE | stat.S_IREAD)
        frozen.unlink()
        try:
            frozen.symlink_to(original)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self._reader() as reader:
            result = reader.read(f"blob:{digest}")
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("content", result)


if __name__ == "__main__":
    unittest.main()
