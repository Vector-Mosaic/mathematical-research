from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.canonical_snapshot import (  # noqa: E402
    CANONICAL_STATE_REPO_PATH,
    load_canonical_snapshot,
    verify_snapshot_current,
    verify_snapshot_integrity,
)
from research_core.research_model import SourceClass  # noqa: E402


SOURCE_COMMIT = "a" * 40


class CanonicalSnapshotTests(unittest.TestCase):
    def test_live_load_persists_only_exact_five_key_v2_binding(self) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit=SOURCE_COMMIT)
        self.assertTrue(loaded.ok, loaded.failure)
        snapshot = loaded.value
        assert snapshot is not None
        self.assertEqual(
            snapshot.authority_vector.to_mapping(),
            {
                "binding_version": 2,
                "source_class": "live_canonical",
                "canonical_state_path": CANONICAL_STATE_REPO_PATH,
                "canonical_state_sha256": snapshot.raw_sha256,
                "source_commit": SOURCE_COMMIT,
            },
        )
        self.assertTrue(verify_snapshot_integrity(snapshot))
        self.assertIsNone(verify_snapshot_current(snapshot))
        with self.assertRaises(TypeError):
            snapshot.state["kind"] = "changed"  # type: ignore[index]

    def test_live_load_requires_full_release_sha(self) -> None:
        for source_commit in (None, "", "a" * 39, "A" * 40, "a" * 64):
            with self.subTest(source_commit=source_commit):
                loaded = load_canonical_snapshot(
                    REPO_ROOT,
                    source_commit=source_commit,
                )
                self.assertFalse(loaded.ok)
                self.assertEqual(
                    loaded.failure.code,
                    "canonical_source_not_authoritative",
                )

    def test_live_load_rejects_alternate_path_but_fixture_load_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            alternate = Path(temporary) / "state.json"
            alternate.write_bytes((REPO_ROOT / CANONICAL_STATE_REPO_PATH).read_bytes())
            live = load_canonical_snapshot(
                REPO_ROOT,
                state_path=alternate,
                source_commit=SOURCE_COMMIT,
            )
            self.assertFalse(live.ok)
            fixture = load_canonical_snapshot(
                REPO_ROOT,
                state_path=alternate,
                source_class=SourceClass.SYNTHETIC_FIXTURE,
            )
            self.assertTrue(fixture.ok, fixture.failure)
            assert fixture.value is not None
            self.assertIsNone(fixture.value.authority_vector.source_commit)

    def test_current_verification_rereads_exact_state_bytes_and_meaning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / CANONICAL_STATE_REPO_PATH
            state_path.parent.mkdir(parents=True)
            original = (REPO_ROOT / CANONICAL_STATE_REPO_PATH).read_bytes()
            state_path.write_bytes(original)
            loaded = load_canonical_snapshot(root, source_commit=SOURCE_COMMIT)
            self.assertTrue(loaded.ok, loaded.failure)
            snapshot = loaded.value
            assert snapshot is not None

            value = json.loads(original)
            value["project"]["title"] += " changed"
            state_path.write_text(json.dumps(value), encoding="utf-8")
            failure = verify_snapshot_current(snapshot)
            self.assertIsNotNone(failure)
            self.assertEqual(failure.code, "authority_surface_changed")

    def test_integrity_detects_forged_in_memory_meaning_without_an_issuer_seal(
        self,
    ) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit=SOURCE_COMMIT)
        snapshot = loaded.value
        assert snapshot is not None
        forged = replace(snapshot, normalized_digest="0" * 64)
        self.assertFalse(verify_snapshot_integrity(forged))
        failure = verify_snapshot_current(forged)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, "canonical_snapshot_mutated")

    def test_unrelated_document_bytes_are_not_canonical_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / CANONICAL_STATE_REPO_PATH
            state_path.parent.mkdir(parents=True)
            state_path.write_bytes((REPO_ROOT / CANONICAL_STATE_REPO_PATH).read_bytes())
            loaded = load_canonical_snapshot(root, source_commit=SOURCE_COMMIT)
            self.assertTrue(loaded.ok, loaded.failure)
            snapshot = loaded.value
            assert snapshot is not None
            status = root / "docs/status.md"
            status.parent.mkdir(parents=True)
            status.write_text("not canonical mathematics", encoding="utf-8")
            self.assertIsNone(verify_snapshot_current(snapshot))


if __name__ == "__main__":
    unittest.main()
