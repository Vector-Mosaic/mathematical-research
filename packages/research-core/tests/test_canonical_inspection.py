from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for location in (PACKAGE_ROOT, TEST_ROOT):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

import test_mission_interface_direct as fixtures
from research_core import executive_orientation as orientation
from research_core.canonical_snapshot import CANONICAL_STATE_REPO_PATH, load_canonical_snapshot
from research_core.json_support import canonical_json_bytes
from research_core.mission_interface import MissionInterface
from research_core.research_model import deep_thaw
from research_core.workspace_store import WorkspaceIntegrityError


class CanonicalInspectionTests(unittest.TestCase):
    """Stopped inspection binds selected bytes to persisted, not runtime, authority."""

    selected_sha = "b" * 40

    @classmethod
    def setUpClass(cls):
        fixtures.DirectMissionInterfaceTests.setUpClass.__func__(cls)

    def setUp(self):
        fixtures.DirectMissionInterfaceTests.setUp(self)
        self.byte_root = (Path(self.temporary.name) / "releases" / self.selected_sha).resolve()
        self.byte_path = self.byte_root / CANONICAL_STATE_REPO_PATH
        self.byte_path.parent.mkdir(parents=True)
        self.byte_path.write_bytes(self.canonical.authorized_path.read_bytes())
        self.persisted = json.loads(self.store.read_metadata()["canonical_authority_json"])
        self.assertEqual(self.persisted["source_commit"], "a" * 40)
        self.assertNotEqual(self.selected_sha, self.persisted["source_commit"])

    def _open(self):
        return MissionInterface.open(
            self.store.paths.root, self.store.project_id, self.interface.mission_id,
            canonical_repo_root=self.byte_root,
        )

    def _state(self):
        return (
            deep_thaw(self.store.read_metadata()),
            self.store.paths.database.read_bytes(),
            self.canonical.authorized_path.read_bytes(),
            tuple(sorted(path.relative_to(self.store.paths.root).as_posix()
                         for path in self.store.paths.root.rglob("*"))),
        )

    def test_distinct_selected_bytes_and_persisted_authority_succeed_in_one_read_cut(self):
        reader = self._open()
        before = self._state()
        reads = []

        def load(root, **kwargs):
            self.assertIsNotNone(reader._store._direct_recovery_read_connection.get())
            reads.append((Path(root), kwargs["source_commit"]))
            return load_canonical_snapshot(root, **kwargs)

        with (
            mock.patch.object(orientation, "load_canonical_snapshot", side_effect=load),
            mock.patch.object(reader._store, "direct_recovery_read_scope", wraps=reader._store.direct_recovery_read_scope) as scope,
            mock.patch.object(reader, "_writer_lease", side_effect=AssertionError("inspection acquired a writer")),
            mock.patch.object(reader, "authorize_executive_epoch_from_owner", side_effect=AssertionError("inspection authorized an epoch")),
        ):
            result = reader.host_snapshot()
        self.assertEqual(scope.call_count, 1)
        self.assertTrue(reads)
        self.assertEqual(set(reads), {(self.byte_root, self.persisted["source_commit"])})
        self.assertEqual(
            set(result),
            {"schema_version", "authorization_cut", "current_state", "executive_orientation"},
        )
        self.assertEqual(
            result["schema_version"],
            "mathematical_research.mission_host_snapshot.v4",
        )
        self.assertEqual(
            result["authorization_cut"]["project_commit"],
            result["current_state"]["observed_project_commit"],
        )
        self.assertIsNone(result["current_state"]["latest_executive_epoch"])
        self.assertEqual(result["current_state"]["canonical_authority"], {
            "source_commit": self.persisted["source_commit"],
            "canonical_state_sha256": self.persisted["canonical_state_sha256"],
            "canonical_authority_digest": before[0]["canonical_authority_digest"],
        })
        self.assertNotIn("reconstruction", result)
        self.assertNotIn("host_recovery_facts", result)
        self.assertNotIn("canonical_authority", json.dumps(result["executive_orientation"]))
        self.assertEqual(self._state(), before)

    def test_selected_bytes_mismatch_fails_without_owner_effect(self):
        # Whitespace changes the exact bytes while retaining valid mathematics.
        self.byte_path.write_bytes(self.byte_path.read_bytes() + b"\n")
        reader = self._open()
        before = self._state()
        with self.assertRaises(WorkspaceIntegrityError):
            reader.host_snapshot()
        self.assertEqual(self._state(), before)

    def test_selected_sha_cannot_substitute_for_persisted_authority_commit(self):
        manufactured = load_canonical_snapshot(self.byte_root, source_commit=self.selected_sha)
        self.assertTrue(manufactured.ok)
        reader = MissionInterface.open(
            self.store.paths.root, self.store.project_id, self.interface.mission_id,
            canonical_snapshot=manufactured.value,
        )
        before = self._state()
        with self.assertRaises(WorkspaceIntegrityError):
            reader.host_snapshot()
        self.assertEqual(self._state(), before)

    def test_malformed_persisted_vector_is_rejected_without_owner_effect(self):
        reader = self._open()
        before = self._state()
        malformed = (
            {key: value for key, value in self.persisted.items() if key != "source_commit"},
            {**self.persisted, "release_sha": self.selected_sha},
            {**self.persisted, "binding_version": 2.0},
            {**self.persisted, "source_class": "historical_fixture"},
            {**self.persisted, "canonical_state_path": "elsewhere/research_state.json"},
            {**self.persisted, "canonical_state_sha256": "not-a-hash"},
            {**self.persisted, "source_commit": self.selected_sha.upper()},
        )
        for vector in malformed:
            raw = canonical_json_bytes(vector)
            metadata = {**before[0], "canonical_authority_json": raw.decode("utf-8"),
                        "canonical_authority_digest": hashlib.sha256(raw).hexdigest()}
            with self.subTest(vector=vector), mock.patch.object(reader._store, "read_metadata", return_value=metadata):
                with self.assertRaises(WorkspaceIntegrityError):
                    reader.host_snapshot()
            self.assertEqual(self._state(), before)

    def test_persisted_authority_digest_mismatch_fails_before_loading_bytes(self):
        reader = self._open()
        before = self._state()
        metadata = {**before[0], "canonical_authority_digest": "0" * 64}
        with mock.patch.object(reader._store, "read_metadata", return_value=metadata), mock.patch.object(orientation, "load_canonical_snapshot") as load:
            with self.assertRaises(WorkspaceIntegrityError):
                reader.host_snapshot()
            load.assert_not_called()
        self.assertEqual(self._state(), before)

    def test_canonical_file_symlink_cannot_redirect_selected_bytes(self):
        outside = Path(self.temporary.name) / "outside-canonical.json"
        outside.write_bytes(self.byte_path.read_bytes())
        self.byte_path.unlink()
        try:
            self.byte_path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"local platform cannot create this disposable symlink: {exc.winerror if hasattr(exc, 'winerror') else exc.errno}")
        reader = self._open()
        before = self._state()
        with self.assertRaises(WorkspaceIntegrityError):
            reader.host_snapshot()
        self.assertEqual(self._state(), before)


if __name__ == "__main__":
    unittest.main()
