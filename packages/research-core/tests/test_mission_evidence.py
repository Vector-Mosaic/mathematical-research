from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for value in (PACKAGE_ROOT, Path(__file__).resolve().parent):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from research_core.canonical_snapshot import load_canonical_snapshot  # noqa: E402
from research_core import workspace_store as store_module  # noqa: E402
from research_core.evidence_store import (  # noqa: E402
    QUARANTINE_REASON_OPAQUE_RESTRICTED,
    EvidenceCAS,
    EvidenceStoreError,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_evidence import (  # noqa: E402
    CaptureScope,
    InterpretedOwnerReference,
    RawCaptureArtifactFileInput,
    RawCaptureArtifactInput,
    RawCaptureRecord,
    commit_capture_scope_annotation,
    commit_evidence_meaning,
    commit_raw_capture,
    prepare_capture_scope_annotation,
    prepare_evidence_meaning,
    prepare_raw_capture,
    read_capture_scope_annotation,
    read_evidence_meaning,
    read_raw_capture,
)
from research_core.mission_interface import MissionInterface  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    DirectExecutiveEpochAuthority,
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    prepare_direct_executive_epoch_failed_before_checkpoint_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.workspace_store import (  # noqa: E402
    IdentityKind,
    StaleCommandError,
    TypedWorkspaceId,
)


def _direct_genesis_fixture() -> dict[str, object]:
    project_id = "project.rh"
    mission_id = "mission.1"
    branch_id = "branch.theta"
    strategy_id = "strategy.theta.1"
    seed = json.loads(
        (
            REPO_ROOT
            / "contracts"
            / "rh_autonomous_mission_seed.v1.json"
        ).read_text(encoding="utf-8")
    )
    seed["project_id"] = project_id
    seed["authority"]["project_id"] = project_id
    mission = seed["mission"]
    mission["project_id"] = project_id
    mission["mission_id"] = mission_id
    mission["strategy_ids"] = [strategy_id]
    branch = seed["opening_branch"]
    branch["project_id"] = project_id
    branch["mission_id"] = mission_id
    branch["branch_id"] = branch_id
    strategy = seed["strategy"]
    strategy["project_id"] = project_id
    strategy["mission_id"] = mission_id
    strategy["strategy_id"] = strategy_id
    branch_digest = hashlib.sha256(canonical_json_bytes(branch)).hexdigest()

    def rebind_references(value: object) -> None:
        if isinstance(value, dict):
            if set(value) == {
                "kind",
                "identity",
                "revision",
                "payload_sha256",
            }:
                value.update(
                    {
                        "kind": "branch",
                        "identity": branch_id,
                        "revision": 1,
                        "payload_sha256": branch_digest,
                    }
                )
                return
            for child in value.values():
                rebind_references(child)
        elif isinstance(value, list):
            for child in value:
                rebind_references(child)

    rebind_references(strategy)
    return seed


class MissionNativeEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        loaded = load_canonical_snapshot(REPO_ROOT, source_commit="a" * 40)
        if not loaded.ok or loaded.value is None:
            raise AssertionError(loaded.failure)
        cls.canonical = loaded.value

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        interface = MissionInterface.initialize_from_owner(
            Path(self.temporary.name) / "workspace",
            project_id="project.rh",
            mission_id="mission.1",
            canonical_snapshot=self.canonical,
            genesis_seed=_direct_genesis_fixture(),
            owner_command_id="evidence-test.direct-genesis",
        )
        self.store = interface._store
        self.lease = interface._writer_lease()
        self.cas = EvidenceCAS(self.store.paths)

    def _open_direct_epoch(self) -> DirectExecutiveEpochAuthority:
        mission_head = self.store.get_head(
            TypedWorkspaceId(IdentityKind.MISSION, "mission.1")
        )
        self.assertIsNotNone(mission_head)
        assert mission_head is not None
        epoch_id = "epoch.direct-native-evidence.1"
        authorized = prepare_direct_executive_epoch_authorized_event(
            executive_epoch_id=epoch_id,
            mission_root=OwnerRevisionRef(
                "mission",
                "mission.1",
                mission_head.reference.revision,
                mission_head.payload_digest,
            ),
            predecessor_checkpoint=None,
        )
        bound = prepare_direct_executive_epoch_bound_event(
            executive_epoch_id=epoch_id,
            mission_id="mission.1",
            goal_thread_id="goal-thread:direct-native-evidence-test",
            workspace_root="C:/work/rh",
        )
        metadata = self.store.read_metadata()
        authority_digest = str(metadata["canonical_authority_digest"])
        expected_cut = {
            "project_commit": int(metadata["current_project_commit"]),
            "current_root_digest": str(metadata["current_root_digest"]),
            "transition_head_digest": metadata["transition_head_digest"],
            "canonical_authority_digest": authority_digest,
        }
        self.store.authorize_executive_epoch(
            mission_id="mission.1",
            executive_epoch_id=epoch_id,
            event=authorized,
            lease=self.lease,
            command_id="native-evidence.direct.authorize",
            actor="coordinating-codex",
            expected_canonical_authority_digest=authority_digest,
            expected_mission_host_store_cut=expected_cut,
        )
        self.store.bind_executive_epoch(
            mission_id="mission.1",
            executive_epoch_id=epoch_id,
            event=bound,
            expected_event_ordinal=1,
            expected_event_digest=authorized.digest_sha256,
            lease=self.lease,
            command_id="native-evidence.direct.bind",
            actor="coordinating-codex",
            expected_canonical_authority_digest=authority_digest,
        )

        event_readbacks = self.store.read_active_executive_epoch("mission.1")
        self.assertIsNotNone(event_readbacks)
        assert event_readbacks is not None
        return reissue_direct_executive_epoch_authority(
            event_readbacks=event_readbacks,
            project_id="project.rh",
            root_identity=self.store.paths.root_identity,
            canonical_authority_digest=authority_digest,
        )

    def test_successor_capture_preserves_empty_binary_multi_artifact_and_late_output(
        self,
    ) -> None:
        authority = self._open_direct_epoch()
        failed = prepare_direct_executive_epoch_failed_before_checkpoint_event(
            executive_epoch_id=authority.executive_epoch_id,
            mission_id=authority.mission_id,
            reconciliation={
                "stage": "goal_runtime",
                "failure_reason": "Goal ended before a continuation checkpoint.",
            },
        )
        self.store.fail_executive_epoch_before_checkpoint(
            mission_id=authority.mission_id,
            executive_epoch_id=authority.executive_epoch_id,
            event=failed,
            expected_event_ordinal=authority.bound_event_ordinal,
            expected_event_digest=authority.bound_event_digest,
            lease=self.lease,
            command_id="native-evidence.direct.fail-before-late-capture",
            actor="coordinating-codex",
            expected_canonical_authority_digest=(authority.canonical_authority_digest),
        )
        artifacts = (
            RawCaptureArtifactInput(
                role="stdout",
                logical_name="stdout.bin",
                content_bytes=b"",
            ),
            RawCaptureArtifactInput(
                role="result",
                logical_name="result.bin",
                content_bytes=b"\x00\xff\x10exact",
            ),
        )
        capture = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            capture_kind="output",
            observation_id="native-output-observation.17",
            assignment_id="native-assignment.17",
            provenance={
                "kind": "native_subagent_output",
                "root_thread_id": "goal-thread:native-evidence-test",
                "child_thread_id": "goal-child.17",
            },
            completion={"lifecycle": "completed", "exit_kind": "returned"},
            artifacts=artifacts,
        )
        # Custody deliberately takes no current Executive Epoch authority: an
        # authentic late result remains capturable after its producer closes.
        first = commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.lease,
            actor="native-capture-boundary",
        )
        replay = commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.lease,
            actor="native-capture-boundary",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(first.project_commit, replay.project_commit)

        readback = read_raw_capture(self.store, capture_id=capture.capture_id)
        self.assertEqual(readback.capture_id, capture.capture_id)
        self.assertEqual(
            readback.executive_epoch_id,
            authority.executive_epoch_id,
        )
        self.assertEqual(
            tuple(item.role for item in readback.artifacts), ("stdout", "result")
        )
        for artifact_input, descriptor in zip(
            artifacts, readback.artifacts, strict=True
        ):
            self.assertEqual(
                self.cas.path_for_digest(descriptor.blob_sha256).read_bytes(),
                artifact_input.content_bytes,
            )

        changed_facts = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            capture_kind="output",
            observation_id="native-output-observation.17",
            assignment_id="native-assignment.17",
            provenance={
                "kind": "native_subagent_output",
                "root_thread_id": "goal-thread:native-evidence-test",
                "child_thread_id": "goal-child.17",
            },
            completion={"lifecycle": "completed", "exit_kind": "returned"},
            artifacts=(
                artifacts[0],
                RawCaptureArtifactInput(
                    role="result",
                    logical_name="result.bin",
                    content_bytes=b"changed bytes",
                ),
            ),
        )
        self.assertEqual(changed_facts.capture_id, capture.capture_id)
        with self.assertRaises(StaleCommandError):
            commit_raw_capture(
                self.store,
                cas=self.cas,
                record=changed_facts,
                lease=self.lease,
                actor="native-capture-boundary",
            )

    def test_file_backed_raw_capture_streams_large_exact_artifact_without_evidence(
        self,
    ) -> None:
        authority = self._open_direct_epoch()
        sealed_root = Path(self.temporary.name) / "workstation-seals"
        sealed_root.mkdir()
        source = sealed_root / "formal-result.bin"
        digest = hashlib.sha256()
        byte_length = 0
        with source.open("wb") as stream:
            for ordinal in range(6):
                chunk = bytes([ordinal + 17]) * (1024 * 1024)
                stream.write(chunk)
                digest.update(chunk)
                byte_length += len(chunk)
            tail = b"file-backed-result-tail"
            stream.write(tail)
            digest.update(tail)
            byte_length += len(tail)
            stream.flush()
            os.fsync(stream.fileno())
        self.assertGreater(byte_length, 4 * 1024 * 1024)
        opaque_source = sealed_root / "opaque-output.zip"
        opaque_bytes = b"PK\x03\x04\napi_key=untrusted-formal-output-bytes"
        opaque_source.write_bytes(opaque_bytes)
        opaque_digest = hashlib.sha256(opaque_bytes).hexdigest()

        capture = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            capture_kind="output",
            observation_id="formal-output-observation.file-backed",
            assignment_id="formal-session.file-backed",
            provenance={"kind": "formal_attempt_output", "attempt_id": "attempt.44"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactFileInput(
                    role="result",
                    logical_name="formal-result.bin",
                    source_path=source,
                    sha256=digest.hexdigest(),
                    byte_length=byte_length,
                ),
                RawCaptureArtifactInput(
                    role="result-envelope",
                    logical_name="result-envelope.json",
                    content_bytes=b'{"status":"completed"}',
                    media_type="application/json",
                    encoding="utf-8",
                ),
                RawCaptureArtifactFileInput(
                    role="opaque-output",
                    logical_name="opaque-output.zip",
                    source_path=opaque_source,
                    sha256=opaque_digest,
                    byte_length=len(opaque_bytes),
                    media_type="application/zip",
                    quarantine_reason=QUARANTINE_REASON_OPAQUE_RESTRICTED,
                ),
            ),
            cas=self.cas,
        )
        committed = commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.lease,
            actor="formal-capture-boundary",
        )
        self.assertFalse(committed.replayed)
        readback = read_raw_capture(self.store, capture_id=capture.capture_id)
        self.assertEqual(len(readback.artifacts), 3)
        installed = self.cas.path_for_digest(readback.artifacts[0].blob_sha256)
        self.assertEqual(installed.stat().st_size, byte_length)
        installed_digest = hashlib.sha256()
        with installed.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                installed_digest.update(chunk)
        self.assertEqual(installed_digest.hexdigest(), digest.hexdigest())
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            automatic_meanings = connection.execute(
                "SELECT COUNT(*) FROM evidence_capture_source WHERE capture_id = ?",
                (capture.capture_id,),
            ).fetchone()[0]
            opaque_quarantine = connection.execute(
                "SELECT quarantine_state FROM blob WHERE sha256 = ?",
                (opaque_digest,),
            ).fetchone()[0]
        self.assertEqual(automatic_meanings, 0)
        self.assertEqual(opaque_quarantine, "quarantined")

    def test_file_backed_capture_preserves_distinct_aliases_of_one_exact_blob(
        self,
    ) -> None:
        authority = self._open_direct_epoch()
        source = Path(self.temporary.name) / "shared-sealed-artifact.txt"
        payload = b"one sealed payload with two logical origins\n"
        source.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()

        capture = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            capture_kind="output",
            observation_id="formal-output-observation.shared-blob-aliases",
            assignment_id="formal-session.shared-blob-aliases",
            provenance={"kind": "formal_attempt_output"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactFileInput(
                    role="accepted_output",
                    logical_name="output/result.txt",
                    source_path=source,
                    sha256=digest,
                    byte_length=len(payload),
                ),
                RawCaptureArtifactFileInput(
                    role="evidence_only",
                    logical_name="scratch/trace.txt",
                    source_path=source,
                    sha256=digest,
                    byte_length=len(payload),
                ),
            ),
            cas=self.cas,
        )
        committed = commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.lease,
            actor="formal-capture-boundary",
        )

        self.assertFalse(committed.replayed)
        readback = read_raw_capture(self.store, capture_id=capture.capture_id)
        self.assertEqual(
            [(item.role, item.logical_name) for item in readback.artifacts],
            [
                ("accepted_output", "output/result.txt"),
                ("evidence_only", "scratch/trace.txt"),
            ],
        )
        self.assertEqual(
            {item.blob_sha256 for item in readback.artifacts},
            {digest},
        )
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            blob_count = connection.execute(
                "SELECT COUNT(*) FROM blob WHERE sha256 = ?", (digest,)
            ).fetchone()[0]
        self.assertEqual(blob_count, 1)

    def test_same_digest_quarantine_conflict_is_rejected_in_either_order(
        self,
    ) -> None:
        authority = self._open_direct_epoch()
        source_root = Path(self.temporary.name) / "duplicate-quarantine"
        source_root.mkdir()
        payload = b"same exact ordinary output"
        digest = hashlib.sha256(payload).hexdigest()
        first_source = source_root / "first.bin"
        second_source = source_root / "second.bin"
        first_source.write_bytes(payload)
        second_source.write_bytes(payload)
        clear = RawCaptureArtifactFileInput(
            role="clear",
            logical_name="clear.bin",
            source_path=first_source,
            sha256=digest,
            byte_length=len(payload),
        )
        quarantined = RawCaptureArtifactFileInput(
            role="opaque",
            logical_name="opaque.bin",
            source_path=second_source,
            sha256=digest,
            byte_length=len(payload),
            quarantine_reason=QUARANTINE_REASON_OPAQUE_RESTRICTED,
        )

        for order, artifact_inputs in enumerate(
            ((clear, quarantined), (quarantined, clear)),
            start=1,
        ):
            with self.subTest(order=order), self.assertRaises(ValueError) as conflict:
                prepare_raw_capture(
                    self.store,
                    mission_id="mission.1",
                    executive_epoch_id=authority.executive_epoch_id,
                    capture_kind="output",
                    observation_id=f"formal-output-observation.duplicate-{order}",
                    assignment_id="formal-session.duplicate-quarantine",
                    provenance={"kind": "formal_attempt_output"},
                    completion={"lifecycle": "completed"},
                    artifacts=artifact_inputs,
                    cas=self.cas,
                )
            self.assertIn("conflicting physical Blob metadata", str(conflict.exception))

    def test_file_backed_raw_capture_replays_after_source_deletion_and_rejects_drift(
        self,
    ) -> None:
        authority = self._open_direct_epoch()
        source_root = Path(self.temporary.name) / "deleted-replay-source"
        source_root.mkdir()
        source = source_root / "result.bin"
        payload = b"sealed result that becomes disposable after custody"
        source.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()

        def prepare_one(
            *,
            sha256: str = digest,
            media_type: str = "application/octet-stream",
        ) -> RawCaptureRecord:
            return prepare_raw_capture(
                self.store,
                mission_id="mission.1",
                executive_epoch_id=authority.executive_epoch_id,
                capture_kind="output",
                observation_id="formal-output-observation.deleted-replay",
                assignment_id="formal-session.deleted-replay",
                provenance={
                    "kind": "formal_attempt_output",
                    "attempt_id": "attempt.45",
                },
                completion={"lifecycle": "completed"},
                artifacts=(
                    RawCaptureArtifactFileInput(
                        role="result",
                        logical_name="result.bin",
                        source_path=source,
                        sha256=sha256,
                        byte_length=len(payload),
                        media_type=media_type,
                    ),
                ),
                cas=self.cas,
            )

        first_record = prepare_one()
        first = commit_raw_capture(
            self.store,
            cas=self.cas,
            record=first_record,
            lease=self.lease,
            actor="formal-capture-boundary",
        )
        source.unlink()
        self.assertFalse(source.exists())

        replay_record = prepare_one()
        replay = commit_raw_capture(
            self.store,
            cas=self.cas,
            record=replay_record,
            lease=self.lease,
            actor="formal-capture-boundary",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(first.project_commit, replay.project_commit)
        self.assertEqual(self.cas.path_for_digest(digest).read_bytes(), payload)

        metadata_drift = prepare_one(media_type="text/plain")
        with self.assertRaises(StaleCommandError):
            commit_raw_capture(
                self.store,
                cas=self.cas,
                record=metadata_drift,
                lease=self.lease,
                actor="formal-capture-boundary",
            )
        digest_drift = prepare_one(sha256=hashlib.sha256(b"changed result").hexdigest())
        with self.assertRaises(StaleCommandError):
            commit_raw_capture(
                self.store,
                cas=self.cas,
                record=digest_drift,
                lease=self.lease,
                actor="formal-capture-boundary",
            )

    def test_file_backed_raw_capture_rejects_unsafe_source_identity_and_collision(
        self,
    ) -> None:
        authority = self._open_direct_epoch()
        source_root = Path(self.temporary.name) / "source-safety"
        source_root.mkdir()

        def prepare_one(
            source: Path,
            *,
            sha256: str,
            byte_length: int,
            observation: str,
        ) -> None:
            prepare_raw_capture(
                self.store,
                mission_id="mission.1",
                executive_epoch_id=authority.executive_epoch_id,
                capture_kind="output",
                observation_id=observation,
                assignment_id="formal-session.source-safety",
                provenance={"kind": "formal_attempt_output"},
                completion={"lifecycle": "completed"},
                artifacts=(
                    RawCaptureArtifactFileInput(
                        role="result",
                        logical_name="result.bin",
                        source_path=source,
                        sha256=sha256,
                        byte_length=byte_length,
                    ),
                ),
                cas=self.cas,
            )

        regular = source_root / "regular.bin"
        regular.write_bytes(b"exact-source")
        with self.assertRaises(EvidenceStoreError) as wrong_digest:
            prepare_one(
                regular,
                sha256=hashlib.sha256(b"different").hexdigest(),
                byte_length=regular.stat().st_size,
                observation="formal-output-observation.wrong-digest",
            )
        self.assertEqual(wrong_digest.exception.code, "exact_file_digest_mismatch")

        hard_link = source_root / "regular-hard-link.bin"
        os.link(regular, hard_link)
        with self.assertRaises(EvidenceStoreError) as linked:
            prepare_one(
                regular,
                sha256=hashlib.sha256(b"exact-source").hexdigest(),
                byte_length=regular.stat().st_size,
                observation="formal-output-observation.hard-link",
            )
        self.assertEqual(linked.exception.code, "exact_file_link_unsupported")
        hard_link.unlink()

        directory = source_root / "not-a-file"
        directory.mkdir()
        with self.assertRaises(EvidenceStoreError) as non_regular:
            prepare_one(
                directory,
                sha256=hashlib.sha256(b"").hexdigest(),
                byte_length=0,
                observation="formal-output-observation.non-regular",
            )
        self.assertEqual(non_regular.exception.code, "exact_file_not_regular")

        symlink = source_root / "regular-symlink.bin"
        try:
            symlink.symlink_to(regular)
        except OSError:
            pass
        else:
            with self.assertRaises(EvidenceStoreError) as linked:
                prepare_one(
                    symlink,
                    sha256=hashlib.sha256(b"exact-source").hexdigest(),
                    byte_length=regular.stat().st_size,
                    observation="formal-output-observation.symlink",
                )
            self.assertEqual(linked.exception.code, "exact_file_link_unsupported")

        existing = self.cas.ingest_bytes(
            b"existing-cas-object",
            original_name="existing.bin",
        )
        with self.assertRaises(EvidenceStoreError) as collision:
            prepare_one(
                existing.path,
                sha256=existing.record.sha256,
                byte_length=existing.record.length,
                observation="formal-output-observation.path-collision",
            )
        self.assertEqual(collision.exception.code, "exact_file_path_collision")

    def test_exact_file_stream_rejects_source_mutation_and_cleans_stage(self) -> None:
        source_root = Path(self.temporary.name) / "mutation-source"
        source_root.mkdir()
        source = source_root / "result.bin"
        original = b"a" * (2 * 1024 * 1024)
        source.write_bytes(original)

        def mutate_after_stream(phase: str, path: Path) -> None:
            if phase != "after_source_streamed":
                return
            with path.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"b")
                stream.flush()
                os.fsync(stream.fileno())

        with self.assertRaises(EvidenceStoreError) as changed:
            self.cas.ingest_exact_file(
                source,
                expected_sha256=hashlib.sha256(original).hexdigest(),
                expected_length=len(original),
                original_name="result.bin",
                fault_hook=mutate_after_stream,
            )
        self.assertEqual(changed.exception.code, "exact_file_changed")
        self.assertEqual(tuple(self.cas.staging_root.iterdir()), ())

    def test_successor_evidence_meanings_are_independent_and_scope_capture_bytes(
        self,
    ) -> None:
        fixed_time = "2026-09-07T00:00:00Z"
        self.store._clock = lambda: fixed_time
        authority = self._open_direct_epoch()
        capture = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            capture_kind="output",
            observation_id="native-output-observation.meanings",
            assignment_id="native-assignment.meanings",
            provenance={"kind": "native_subagent_output", "child": "worker.1"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactInput(
                    role="derivation",
                    logical_name="derivation.txt",
                    content_bytes=b"identity and objection in one mixed result",
                    media_type="text/plain",
                    encoding="utf-8",
                ),
                RawCaptureArtifactInput(
                    role="calculation",
                    logical_name="check.bin",
                    content_bytes=b"\x00\x01",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.lease,
            actor="native-capture-boundary",
        )
        scopes = (
            CaptureScope(capture.capture_id, 0, {"lines": [1, 1]}),
            CaptureScope(capture.capture_id, 1, "the complete exact calculation"),
        )
        first = prepare_evidence_meaning(
            authority=authority,
            evidence_id="evidence.meaning.sign-objection",
            statement="The returned normalization has an unresolved sign mismatch.",
            exact_scope="the stated transform normalization",
            strength="objection",
            semantic_role="objection",
            authority_basis="executive interpretation of exact captured material",
            sources=scopes,
            non_inferences=("does not establish or disprove RH",),
            limitations=("the cited source has not yet been independently fetched",),
            standing="active_objection",
            decision_consequence="retain a named sign obligation",
        )
        committed = commit_evidence_meaning(
            self.store,
            record=first,
            lease=self.lease,
            actor="coordinating-codex",
        )
        self.assertFalse(committed.replayed)
        read_first = read_evidence_meaning(self.store, evidence_id=first.evidence_id)
        self.assertEqual(read_first.revision, 1)
        self.assertEqual(read_first.sources, scopes)
        self.assertEqual(read_first.evidence.blob_roles, ())

        sibling = prepare_evidence_meaning(
            authority=authority,
            evidence_id="evidence.meaning.finite-identity",
            statement="The captured finite calculation is exact at its stated scope.",
            exact_scope="the two-byte finite calculation",
            strength="exact_finite_identity",
            semantic_role="result",
            authority_basis="direct finite verification",
            sources=(scopes[1],),
            non_inferences=("does not extend beyond the finite case",),
            standing="usable",
        )
        commit_evidence_meaning(
            self.store,
            record=sibling,
            lease=self.lease,
            actor="coordinating-codex",
        )
        read_sibling = read_evidence_meaning(
            self.store, evidence_id=sibling.evidence_id
        )
        correction = prepare_evidence_meaning(
            authority=authority,
            evidence_id=first.evidence_id,
            statement="The sign mismatch is confined to the first normalization.",
            exact_scope="only the first transform normalization",
            strength="objection",
            semantic_role="corrected_objection",
            authority_basis="executive correction against exact captured material",
            sources=(scopes[0],),
            non_inferences=("does not affect the sibling finite identity",),
            standing="scope_narrowed",
            dependencies=(
                {
                    "kind": "evidence",
                    "identity": sibling.evidence_id,
                    "revision": read_sibling.revision,
                    "payload_sha256": read_sibling.payload_digest,
                },
            ),
            expected_head_revision=read_first.revision,
            expected_head_payload_digest=read_first.payload_digest,
            expected_dependency_heads={
                f"evidence:{sibling.evidence_id}": (
                    read_sibling.revision,
                    read_sibling.payload_digest,
                )
            },
        )
        corrected = commit_evidence_meaning(
            self.store,
            record=correction,
            lease=self.lease,
            actor="coordinating-codex",
        )
        self.assertEqual(
            read_evidence_meaning(self.store, evidence_id=first.evidence_id).revision,
            2,
        )
        self.assertEqual(
            read_evidence_meaning(self.store, evidence_id=sibling.evidence_id).revision,
            1,
        )
        with (
            patch(
                "research_core.workspace_store._validated_journal_index",
                side_effect=AssertionError(
                    "exact historical Evidence read built the complete journal index"
                ),
            ),
            patch(
                "research_core.workspace_store._validated_transition_journal_chain",
                side_effect=AssertionError(
                    "exact historical Evidence read scanned the journal"
                ),
            ),
        ):
            historical_first = read_evidence_meaning(
                self.store,
                evidence_id=first.evidence_id,
                revision=1,
            )
        self.assertEqual(historical_first.revision, 1)
        self.assertEqual(historical_first.sources, scopes)
        self.assertEqual(historical_first.payload_digest, first.payload_digest)
        self.assertEqual(
            read_evidence_meaning(self.store, evidence_id=first.evidence_id).sources,
            (scopes[0],),
        )
        with self.store.snapshot_connection() as connection:
            origins = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT revision, created_at, project_commit_no "
                    "FROM evidence_item_revision WHERE evidence_id = ? "
                    "ORDER BY revision",
                    (first.evidence_id,),
                )
            )
            self.assertEqual(
                origins,
                (
                    (1, fixed_time, committed.project_commit),
                    (2, fixed_time, corrected.project_commit),
                ),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM evidence_blob WHERE evidence_id IN (?, ?)",
                    (first.evidence_id, sibling.evidence_id),
                ).fetchone()[0],
                0,
            )

        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "UPDATE evidence_item_revision "
                "SET project_commit_no = ? "
                "WHERE evidence_id = ? AND revision = 1",
                (
                    corrected.project_commit,
                    first.evidence_id,
                ),
            )
            connection.commit()
        with self.assertRaisesRegex(
            store_module.WorkspaceIntegrityError,
            "historical Evidence revision has no exact journaled origin",
        ):
            read_evidence_meaning(
                self.store,
                evidence_id=first.evidence_id,
                revision=1,
            )
        with self.assertRaisesRegex(
            store_module.WorkspaceIntegrityError,
            "direct recovery auxiliary row differs from its journal origin",
        ):
            self.store.verify_integrity()

    def test_capture_scope_annotation_is_revisable_and_never_evidence(self) -> None:
        authority = self._open_direct_epoch()
        capture = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            capture_kind="output",
            observation_id="native-output-observation.neutral",
            assignment_id="native-assignment.neutral",
            provenance={"kind": "native_subagent_output", "child": "worker.2"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactInput(
                    role="output",
                    logical_name="neutral.txt",
                    content_bytes=b"No presently useful semantic consequence.",
                    media_type="text/plain",
                    encoding="utf-8",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.lease,
            actor="native-capture-boundary",
        )
        with self.store.snapshot_connection() as connection:
            evidence_before = connection.execute(
                "SELECT COUNT(*) FROM evidence_item_revision"
            ).fetchone()[0]
        active = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id="capture-annotation.neutral.1",
            capture_id=capture.capture_id,
            exact_scope={"artifact_ordinal": 0, "coverage": "complete artifact"},
            lifecycle="active",
            dependency_heads={},
        )
        commit_capture_scope_annotation(
            self.store,
            record=active,
            lease=self.lease,
            actor="coordinating-codex",
        )
        read_active = read_capture_scope_annotation(
            self.store, annotation_id=active.annotation_id
        )
        self.assertEqual(
            read_active.annotation_kind,
            "reviewed-no-current-semantic-delta",
        )
        self.assertEqual(read_active.lifecycle, "active")

        removed = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id=active.annotation_id,
            capture_id=capture.capture_id,
            exact_scope={"artifact_ordinal": 0, "coverage": "complete artifact"},
            lifecycle="removed",
            expected_head_revision=read_active.revision,
            expected_head_payload_digest=read_active.payload_digest,
        )
        commit_capture_scope_annotation(
            self.store,
            record=removed,
            lease=self.lease,
            actor="coordinating-codex",
        )
        self.assertEqual(
            read_capture_scope_annotation(
                self.store, annotation_id=active.annotation_id
            ).lifecycle,
            "removed",
        )
        with (
            patch(
                "research_core.workspace_store._validated_journal_index",
                side_effect=AssertionError(
                    "exact historical annotation read built the complete journal index"
                ),
            ),
            patch(
                "research_core.workspace_store._validated_transition_journal_chain",
                side_effect=AssertionError(
                    "exact historical annotation read scanned the journal"
                ),
            ),
        ):
            historical_active = read_capture_scope_annotation(
                self.store, annotation_id=active.annotation_id, revision=1
            )
        self.assertEqual(historical_active.lifecycle, "active")
        with self.store.snapshot_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM evidence_item_revision"
                ).fetchone()[0],
                evidence_before,
            )

    def test_capture_coverage_projection_tracks_distinct_current_coverers(self) -> None:
        authority = self._open_direct_epoch()
        capture = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            capture_kind="output",
            observation_id="native-output-observation.coverage-overlap",
            assignment_id="native-assignment.coverage-overlap",
            provenance={"kind": "native_subagent_output", "child": "worker.coverage"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactInput(
                    role="first",
                    logical_name="first.txt",
                    content_bytes=b"first exact artifact",
                ),
                RawCaptureArtifactInput(
                    role="second",
                    logical_name="second.txt",
                    content_bytes=b"second exact artifact",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.lease,
            actor="native-capture-boundary",
        )

        def coverage_state():
            with self.store.direct_recovery_read_scope():
                cut = self.store.read_root_retrieval_cut()
                connection = self.store._direct_recovery_read_connection.get()
                self.assertIsNotNone(connection)
                assert connection is not None
                commitment = store_module._capture_coverage_commitment_at_cut(
                    connection,
                    project_id="project.rh",
                    project_commit_no=int(cut["project_commit"]),
                )
                tree = store_module._CaptureCoverageMutation(
                    connection,
                    project_id="project.rh",
                    commitment=commitment,
                )
                counts = tuple(
                    tree.lookup(
                        mission_id="mission.1",
                        capture_id=capture.capture_id,
                        artifact_ordinal=ordinal,
                    )
                    for ordinal in (0, 1)
                )
                node_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM capture_artifact_coverage_node"
                    ).fetchone()[0]
                )
                return commitment, counts, node_count

        self.assertEqual(coverage_state()[1], (0, 0))

        def evidence_record(evidence_id, sources, prior=None):
            return prepare_evidence_meaning(
                authority=authority,
                evidence_id=evidence_id,
                statement=f"Exact coverage owner {evidence_id}.",
                exact_scope="the explicitly named exact Capture artifacts",
                strength="exact_finite_identity",
                semantic_role="result",
                authority_basis="direct review of exact captured material",
                sources=sources,
                non_inferences=("does not establish a mathematical theorem",),
                expected_head_revision=(None if prior is None else prior.revision),
                expected_head_payload_digest=(
                    None if prior is None else prior.payload_digest
                ),
            )

        complete_zero = CaptureScope(
            capture.capture_id,
            0,
            {"coverage": "complete artifact"},
        )
        partial_one = CaptureScope(
            capture.capture_id,
            1,
            {"coverage": "partial artifact"},
        )
        first = evidence_record(
            "evidence.coverage.first",
            (complete_zero, complete_zero, partial_one),
        )
        commit_evidence_meaning(
            self.store,
            record=first,
            lease=self.lease,
            actor="coordinating-codex",
        )
        first_read = read_evidence_meaning(
            self.store, evidence_id=first.evidence_id
        )
        self.assertEqual(coverage_state()[1], (1, 0))

        second = evidence_record(
            "evidence.coverage.second",
            (complete_zero,),
        )
        commit_evidence_meaning(
            self.store,
            record=second,
            lease=self.lease,
            actor="coordinating-codex",
        )
        second_read = read_evidence_meaning(
            self.store, evidence_id=second.evidence_id
        )
        root_after_two, counts_after_two, nodes_after_two = coverage_state()
        self.assertEqual(counts_after_two, (2, 0))

        active = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id="capture-annotation.coverage-overlap",
            capture_id=capture.capture_id,
            exact_scope={"artifact_ordinal": 0, "coverage": "complete artifact"},
            lifecycle="active",
        )
        commit_capture_scope_annotation(
            self.store,
            record=active,
            lease=self.lease,
            actor="coordinating-codex",
        )
        _root_after_active, counts_after_active, nodes_after_active = coverage_state()
        self.assertEqual(counts_after_active, (3, 0))
        self.assertGreater(nodes_after_active, nodes_after_two)
        active_read = read_capture_scope_annotation(
            self.store, annotation_id=active.annotation_id
        )
        removed = prepare_capture_scope_annotation(
            authority=authority,
            annotation_id=active.annotation_id,
            capture_id=capture.capture_id,
            exact_scope={"artifact_ordinal": 0, "coverage": "complete artifact"},
            lifecycle="removed",
            expected_head_revision=active_read.revision,
            expected_head_payload_digest=active_read.payload_digest,
        )
        commit_capture_scope_annotation(
            self.store,
            record=removed,
            lease=self.lease,
            actor="coordinating-codex",
        )
        root_after_remove, counts_after_remove, nodes_after_remove = coverage_state()
        self.assertEqual(counts_after_remove, (2, 0))
        self.assertEqual(root_after_remove, root_after_two)
        self.assertEqual(nodes_after_remove, nodes_after_active)

        complete_one = CaptureScope(
            capture.capture_id,
            1,
            {"coverage": "complete artifact"},
        )
        first_moved = evidence_record(
            first.evidence_id,
            (complete_one,),
            first_read,
        )
        commit_evidence_meaning(
            self.store,
            record=first_moved,
            lease=self.lease,
            actor="coordinating-codex",
        )
        self.assertEqual(coverage_state()[1], (1, 1))
        second_moved = evidence_record(
            second.evidence_id,
            (complete_one,),
            second_read,
        )
        commit_evidence_meaning(
            self.store,
            record=second_moved,
            lease=self.lease,
            actor="coordinating-codex",
        )
        self.assertEqual(coverage_state()[1], (0, 2))

        with self.store.direct_recovery_read_scope():
            cut = self.store.read_root_retrieval_cut()
            horizon = self.store.read_root_retrieval_horizon(
                cut_project_commit=int(cut["project_commit"])
            )
            page = self.store.read_root_capture_page(
                mission_id="mission.1",
                cut_project_commit=int(cut["project_commit"]),
                horizon=horizon,
                after=None,
                limit=10,
            )
        selected = next(
            item
            for item in page["records"]
            if item["capture_id"] == capture.capture_id
        )
        self.assertEqual(
            [artifact["pending"] for artifact in selected["artifacts"]],
            [True, False],
        )
        self.store.verify_integrity()

    def test_derived_evidence_uses_two_exact_interpreted_inputs_without_capture(
        self,
    ) -> None:
        authority = self._open_direct_epoch()
        capture = prepare_raw_capture(
            self.store,
            mission_id="mission.1",
            executive_epoch_id=authority.executive_epoch_id,
            capture_kind="output",
            observation_id="native-output-observation.synthesis-inputs",
            assignment_id="native-assignment.synthesis-inputs",
            provenance={"kind": "native_subagent_output", "child": "worker.synthesis"},
            completion={"lifecycle": "completed"},
            artifacts=(
                RawCaptureArtifactInput(
                    role="output",
                    logical_name="lemma-a.txt",
                    content_bytes=b"Exact finite identity A.",
                    media_type="text/plain",
                    encoding="utf-8",
                ),
                RawCaptureArtifactInput(
                    role="output",
                    logical_name="lemma-b.txt",
                    content_bytes=b"Exact compatibility condition B.",
                    media_type="text/plain",
                    encoding="utf-8",
                ),
            ),
        )
        commit_raw_capture(
            self.store,
            cas=self.cas,
            record=capture,
            lease=self.lease,
            actor="native-capture-boundary",
        )

        inputs = []
        for ordinal, evidence_id in enumerate(
            ("evidence.synthesis.a", "evidence.synthesis.b")
        ):
            prepared = prepare_evidence_meaning(
                authority=authority,
                evidence_id=evidence_id,
                statement=f"Interpreted premise {ordinal}.",
                exact_scope=f"exact premise scope {ordinal}",
                strength="exact_finite_identity",
                semantic_role="result",
                authority_basis="executive interpretation of exact captured material",
                sources=(
                    CaptureScope(capture.capture_id, ordinal, "complete artifact"),
                ),
                non_inferences=("does not establish RH",),
            )
            commit_evidence_meaning(
                self.store,
                record=prepared,
                lease=self.lease,
                actor="coordinating-codex",
            )
            persisted = read_evidence_meaning(self.store, evidence_id=evidence_id)
            inputs.append(
                InterpretedOwnerReference(
                    kind="evidence",
                    identity=evidence_id,
                    revision=persisted.revision,
                    payload_sha256=persisted.payload_digest,
                )
            )

        dependencies = {
            f"evidence:{item.identity}": (item.revision, item.payload_sha256)
            for item in inputs
        }
        derived = prepare_evidence_meaning(
            authority=authority,
            evidence_id="evidence.synthesis.derived",
            statement="Premise A composes with compatibility condition B.",
            exact_scope="the exact stated finite composition",
            strength="rigorous_bounded_result",
            semantic_role="derivation",
            authority_basis="executive synthesis of two exact interpreted meanings",
            sources=(),
            interpreted_inputs=tuple(reversed(inputs)),
            non_inferences=("does not extend beyond the stated finite scope",),
            expected_dependency_heads=dependencies,
        )
        before_rejection = int(
            self.store.read_metadata()["current_project_commit"]
        )
        first_blob = capture.artifacts[0].blob_sha256
        first_blob_path = self.cas.path_for_digest(first_blob)
        first_blob_bytes = first_blob_path.read_bytes()
        first_blob_path.chmod(stat.S_IREAD | stat.S_IWRITE)
        first_blob_path.unlink()
        with self.assertRaisesRegex(
            StaleCommandError,
            "Blob bytes are unavailable or corrupt",
        ):
            commit_evidence_meaning(
                self.store,
                record=derived,
                lease=self.lease,
                actor="coordinating-codex",
            )
        self.assertEqual(
            int(self.store.read_metadata()["current_project_commit"]),
            before_rejection,
        )
        with self.store.snapshot_connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM evidence_item_revision WHERE evidence_id = ?",
                    (derived.evidence_id,),
                ).fetchone()
            )
        first_blob_path.write_bytes(first_blob_bytes)
        first_blob_path.chmod(stat.S_IREAD)

        observed_security_scopes = []
        real_security_check = store_module._require_ordinary_security_availability

        def reject_first_source(connection, **kwargs):
            observed_security_scopes.append(
                (
                    tuple(kwargs["blob_sha256s"]),
                    tuple(kwargs["evidence_references"]),
                )
            )
            if first_blob in kwargs["blob_sha256s"]:
                raise store_module._OrdinarySecurityDispositionError(
                    "selected source is security-disposed"
                )
            return real_security_check(connection, **kwargs)

        with (
            patch.object(
                store_module,
                "_require_ordinary_security_availability",
                side_effect=reject_first_source,
            ),
            patch.object(
                store_module,
                "_validated_journal_index",
                side_effect=AssertionError(
                    "derived Evidence validation built a full journal index"
                ),
            ),
            patch.object(
                store_module,
                "_validated_transition_journal_chain",
                side_effect=AssertionError(
                    "derived Evidence validation scanned the journal"
                ),
            ),
            self.assertRaisesRegex(StaleCommandError, "security-disposed"),
        ):
            commit_evidence_meaning(
                self.store,
                record=derived,
                lease=self.lease,
                actor="coordinating-codex",
            )
        self.assertTrue(
            any(
                first_blob in blob_sha256s
                and evidence_references == (("evidence.synthesis.a", 1),)
                for blob_sha256s, evidence_references in observed_security_scopes
            )
        )
        self.assertEqual(
            int(self.store.read_metadata()["current_project_commit"]),
            before_rejection,
        )
        with self.store.snapshot_connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM evidence_item_revision WHERE evidence_id = ?",
                    (derived.evidence_id,),
                ).fetchone()
            )

        committed = commit_evidence_meaning(
            self.store,
            record=derived,
            lease=self.lease,
            actor="coordinating-codex",
        )
        self.assertFalse(committed.replayed)
        persisted = read_evidence_meaning(self.store, evidence_id=derived.evidence_id)
        self.assertEqual(persisted.sources, ())
        self.assertEqual(
            persisted.interpreted_inputs,
            tuple(sorted(inputs, key=lambda item: item.identity)),
        )
        with self.store.snapshot_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM evidence_capture_source WHERE evidence_id = ?",
                    (derived.evidence_id,),
                ).fetchone()[0],
                0,
            )

        before_noop = int(self.store.read_metadata()["current_project_commit"])
        no_op = prepare_evidence_meaning(
            authority=authority,
            evidence_id=derived.evidence_id,
            statement="Premise A composes with compatibility condition B.",
            exact_scope="the exact stated finite composition",
            strength="rigorous_bounded_result",
            semantic_role="derivation",
            authority_basis="executive synthesis of two exact interpreted meanings",
            sources=(),
            interpreted_inputs=tuple(inputs),
            non_inferences=("does not extend beyond the stated finite scope",),
            expected_head_revision=persisted.revision,
            expected_head_payload_digest=persisted.payload_digest,
            expected_dependency_heads=dependencies,
        )
        replayed = commit_evidence_meaning(
            self.store,
            record=no_op,
            lease=self.lease,
            actor="coordinating-codex",
        )
        self.assertTrue(replayed.replayed)
        self.assertEqual(
            int(self.store.read_metadata()["current_project_commit"]), before_noop
        )
        self.assertEqual(
            read_evidence_meaning(self.store, evidence_id=derived.evidence_id).revision,
            1,
        )

        with self.assertRaisesRegex(ValueError, "at least two distinct"):
            prepare_evidence_meaning(
                authority=authority,
                evidence_id="evidence.synthesis.invalid",
                statement="One premise cannot establish a synthesis.",
                exact_scope="one premise only",
                strength="heuristic",
                semantic_role="derivation",
                authority_basis="invalid synthesis fixture",
                sources=(),
                interpreted_inputs=(inputs[0],),
                non_inferences=("no conclusion",),
            )


if __name__ == "__main__":
    unittest.main()
