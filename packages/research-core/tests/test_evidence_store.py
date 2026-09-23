from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.evidence_store import (  # noqa: E402
    QUARANTINE_REASON_OPAQUE_RESTRICTED,
    BlobRecord,
    ClosureManifest,
    ClosureMember,
    ClosureReference,
    DeletionDirective,
    EvidenceCAS,
    EvidenceItemRevision,
    EvidenceRelation,
    EvidenceSecurityDisposition,
    EvidenceStoreError,
    EvidenceTombstone,
    IndependenceDisclosure,
    IntegrityFreeze,
    ProvenanceEvent,
    ResolutionRequirementScope,
    ResolutionRequirementSatisfactionScope,
    StagedBlob,
    VerifiedEvidenceReadHandle,
    build_closure_manifest,
    build_resolution_requirement_relation,
    build_resolution_requirement_satisfaction_relation,
    closure_manifest_from_payload,
    file_is_owner_read_only,
    prepare_evidence_security_disposition,
    resolution_requirement_is_satisfied,
    verify_closure_manifest,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.observability import (  # noqa: E402
    CaptureWorkspaceObserver,
    EventKind,
    ObservationEnvironment,
)
from research_core.workspace_paths import WorkspacePaths  # noqa: E402
from research_core.workspace_store import WorkspaceStore  # noqa: E402


def digest(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def evidence_cas(root: Path) -> EvidenceCAS:
    root.mkdir(parents=True, exist_ok=True)
    return EvidenceCAS(WorkspacePaths.from_root(root))


@contextmanager
def observed_cas_read(path: Path, *, before_open=None, after_read=None):
    """Meter/inject at the real descriptor stream, not a mocked validator."""

    original_open, original_fdopen = os.open, os.fdopen
    selected_descriptors = set()
    counts = {"opens": 0, "bytes": 0, "read_sizes": []}

    class ObservedStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            raw = self.stream.read(size)
            counts["bytes"] += len(raw)
            counts["read_sizes"].append(size)
            if after_read is not None:
                after_read(raw, size)
            return raw

    def open_selected(selected, *args, **kwargs):
        if Path(selected) == path:
            counts["opens"] += 1
            if before_open is not None:
                before_open(args[0] if args else kwargs["flags"])
            descriptor = original_open(selected, *args, **kwargs)
            selected_descriptors.add(descriptor)
            return descriptor
        return original_open(selected, *args, **kwargs)

    def fdopen_selected(descriptor, *args, **kwargs):
        stream = original_fdopen(descriptor, *args, **kwargs)
        return ObservedStream(stream) if descriptor in selected_descriptors else stream

    with (
        patch("research_core.evidence_store.os.open", side_effect=open_selected),
        patch("research_core.evidence_store.os.fdopen", side_effect=fdopen_selected),
    ):
        yield counts


def closure_members(blob_sha256: str) -> tuple[ClosureMember, ...]:
    blob = ClosureMember("blob", blob_sha256, None, blob_sha256)
    provenance = ClosureMember("provenance", "provenance.1", 1, digest("provenance"))
    independence = ClosureMember(
        "independence_disclosure", "independence.1", 1, digest("independence")
    )
    dependency = ClosureMember("dependency", "dependency.1", 1, digest("dependency"))
    evidence = ClosureMember(
        "evidence",
        "evidence.1",
        1,
        digest("evidence"),
        references=(
            ClosureReference("blob", "blob", blob_sha256),
            ClosureReference("provenance", "provenance", "provenance.1", 1),
            ClosureReference(
                "independence_disclosure",
                "independence_disclosure",
                "independence.1",
                1,
            ),
        ),
    )
    candidate = ClosureMember(
        "candidate",
        "candidate.1",
        1,
        digest("candidate"),
        references=(
            ClosureReference("evidence", "evidence", "evidence.1", 1),
            ClosureReference("dependency", "dependency", "dependency.1", 1),
        ),
    )
    return candidate, evidence, provenance, independence, dependency, blob


def valid_closure(cas: EvidenceCAS, blob_record) -> ClosureManifest:
    return build_closure_manifest(
        closure_id="closure.1",
        root=ClosureReference("candidate", "candidate", "candidate.1", 1),
        members=closure_members(blob_record.sha256),
        blobs={blob_record.sha256: blob_record},
        cas=cas,
        omissions=("no literature audit",),
        canonical_pre_state={"sha256": digest("canonical-pre-state")},
    )


class EvidenceStoreTests(unittest.TestCase):
    def test_verified_record_bytes_reads_one_validated_stream(self) -> None:
        payloads = (b"", b"one exact mathematical observation", bytes(range(256)) * 4097)
        with tempfile.TemporaryDirectory() as directory:
            for index, payload in enumerate(payloads):
                with self.subTest(payload=index):
                    root = Path(directory) / str(index)
                    root.mkdir()
                    observer = CaptureWorkspaceObserver()
                    cas = EvidenceCAS(WorkspacePaths.from_root(root), observer=observer)
                    installed = cas.ingest_bytes(payload, original_name="artifact.bin")
                    if index == 2:
                        # Installed CAS custody is not the single-link ingress contract.
                        os.link(installed.path, root / "same-inode.bin")
                    event_start = len(observer.events)
                    read_buffers = []
                    with observed_cas_read(
                        installed.path, after_read=lambda raw, size: read_buffers.append(raw),
                    ) as counts:
                        actual = cas._read_verified_record_bytes(installed.record)
                    self.assertEqual(actual, payload)
                    self.assertIs(actual, read_buffers[0])
                    self.assertEqual(counts["opens"], 1)
                    self.assertEqual(counts["bytes"], len(payload))
                    self.assertEqual(counts["read_sizes"], [len(payload) + 1])
                    self.assertEqual(
                        [event.kind for event in observer.events[event_start:]],
                        [EventKind.CAS_VERIFIED],
                    )
                    if not payload:
                        with observed_cas_read(installed.path) as counts:
                            self.assertEqual(cas._read_verified_record_range(
                                installed.record, offset_bytes=0, max_bytes=7,
                            ), b"")
                        self.assertEqual(counts["read_sizes"], [1])

    def test_verified_record_range_reads_one_full_snapshot_before_slicing(self) -> None:
        payload = bytes(range(256)) * 12289
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory))
            installed = cas.ingest_bytes(payload, original_name="large.bin")
            for offset, limit in ((0, 7), (1024 * 1024 - 3, 19), (len(payload) - 5, 17), (len(payload), 1)):
                with self.subTest(offset=offset), observed_cas_read(installed.path) as counts:
                    actual = cas._read_verified_record_range(
                        installed.record, offset_bytes=offset, max_bytes=limit,
                    )
                self.assertEqual(actual, payload[offset:offset + limit])
                self.assertEqual(counts["opens"], 1)
                self.assertEqual(counts["bytes"], len(payload))
                # Canonical classification still needs the whole immutable
                # buffer. Bounded returned pages are not bounded allocation.
                self.assertEqual(counts["read_sizes"], [len(payload) + 1])
            # A small prefix page may not hide corruption in the final chunk.
            installed.path.chmod(0o600)
            with installed.path.open("r+b") as stream:
                stream.seek(len(payload) - 1)
                stream.write(b"!")
            with self.assertRaises(EvidenceStoreError) as corrupt:
                cas._read_verified_record_range(installed.record, offset_bytes=0, max_bytes=7)
            self.assertEqual(corrupt.exception.code, "blob_corrupt")

    def test_verified_record_bytes_preserves_secret_scanner_and_integrity_precedence(self) -> None:
        boundary = 1024 * 1024
        secret_payloads = (
            b"x" * (boundary - 10) + b"-----BEGIN PRIVATE KEY-----\nfixture",
            ("x" * (boundary // 2 - 8) + "-----BEGIN PRIVATE KEY-----\nfixture").encode("utf-16-le"),
            ("x" * (boundary // 2 - 8) + "-----BEGIN PRIVATE KEY-----\nfixture").encode("utf-16-be"),
            # These cross more than the existing file scanner's overlap.
            # A fixed raw-byte tail cannot preserve whole-buffer semantics.
            b"client_secret" + b" " * (boundary + 4097) + b"=fixture",
            b"api" + b"\x00" * (boundary + 4097) + b"_key=fixture",
            b"-----BEGIN" + b" synthetic" * (boundary // 10 + 500) + b" PRIVATE KEY-----",
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, payload in enumerate(secret_payloads):
                with self.subTest(encoding=index):
                    cas = evidence_cas(Path(directory) / str(index))
                    installed = cas.ingest_bytes(
                        payload, original_name="opaque.bin",
                        quarantine_reason=QUARANTINE_REASON_OPAQUE_RESTRICTED,
                    )
                    forged_clear = replace(installed.record, quarantine_state="clear", quarantine_reason=None)
                    for full_read in (False, True):
                        with self.subTest(full_read=full_read), observed_cas_read(installed.path) as counts:
                            with self.assertRaises(EvidenceStoreError) as secret:
                                if full_read:
                                    cas._read_verified_record_bytes(forged_clear)
                                else:
                                    cas._read_verified_record_range(forged_clear, offset_bytes=0, max_bytes=7)
                        self.assertEqual(secret.exception.code, "secret_payload_requires_quarantine")
                        self.assertEqual(counts["opens"], 1)
                        self.assertEqual(counts["bytes"], len(payload))

            cas = evidence_cas(Path(directory) / "integrity-first")
            secret = b"-----BEGIN PRIVATE KEY-----\nfixture"
            installed = cas.ingest_bytes(b"x" * len(secret), original_name="original.bin")
            installed.path.chmod(0o600)
            installed.path.write_bytes(secret)
            with self.assertRaises(EvidenceStoreError) as corrupt:
                cas._read_verified_record_bytes(installed.record)
            self.assertEqual(corrupt.exception.code, "blob_corrupt")

            for disposition in ("truncated", "missing"):
                with self.subTest(disposition=disposition):
                    item = cas.ingest_bytes(disposition.encode() + b" original", original_name="item.bin")
                    item.path.chmod(0o600)
                    if disposition == "truncated":
                        item.path.write_bytes(b"short")
                    else:
                        item.path.unlink()
                    with self.assertRaises(EvidenceStoreError) as rejected:
                        cas._read_verified_record_bytes(item.record)
                    self.assertEqual(rejected.exception.code, "blob_corrupt" if disposition == "truncated" else "blob_missing")

    def test_verified_record_bytes_rejects_changed_custody(self) -> None:
        payload = b"a" * (1024 * 1024) + b"exact last chunk"
        with tempfile.TemporaryDirectory() as directory:
            for phase in ("before_open", "before_open_fifo", "after_first_read", "after_eof"):
                with self.subTest(phase=phase):
                    if phase == "before_open_fifo" and not hasattr(os, "mkfifo"):
                        continue
                    if phase == "after_eof" and os.name == "nt":
                        # Windows denies replacement of this still-open descriptor;
                        # the native Linux staging run proves the replacement race.
                        continue
                    cas = evidence_cas(Path(directory) / phase)
                    installed = cas.ingest_bytes(payload, original_name="artifact.bin")
                    installed.path.chmod(0o600)
                    replacement = installed.path.with_name("replacement-fixture")
                    replacement.write_bytes(payload)
                    injected = False

                    def replace_before_open(flags):
                        nonlocal injected
                        if phase == "before_open_fifo":
                            # Fail before the real open if a regression would
                            # otherwise block forever waiting for a FIFO writer.
                            self.assertTrue(flags & os.O_NONBLOCK)
                            installed.path.unlink()
                            os.mkfifo(installed.path, 0o600)
                        else:
                            os.replace(replacement, installed.path)
                        injected = True

                    def change_after_read(raw, requested_size):
                        nonlocal injected
                        if injected:
                            return
                        if phase == "after_first_read" and raw:
                            injected = True
                            before = installed.path.stat()
                            with installed.path.open("r+b") as stream:
                                stream.write(b"b")
                            os.utime(installed.path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                        elif (
                            phase == "after_eof" and len(raw) < requested_size
                        ):
                            injected = True
                            os.replace(replacement, installed.path)

                    with observed_cas_read(
                        installed.path,
                        before_open=replace_before_open if phase.startswith("before_open") else None,
                        after_read=change_after_read if not phase.startswith("before_open") else None,
                    ):
                        with self.assertRaises(EvidenceStoreError) as changed:
                            cas._read_verified_record_range(installed.record, offset_bytes=0, max_bytes=7)
                    self.assertTrue(injected)
                    self.assertEqual(changed.exception.code, "blob_corrupt")

    def test_verified_record_bytes_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            payload = b"same bytes behind a forbidden contained link"
            installed = cas.ingest_bytes(payload, original_name="artifact.bin")
            target = cas.root / "contained-target.bin"
            target.write_bytes(payload)
            installed.path.chmod(0o600)
            installed.path.unlink()
            try:
                installed.path.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"native symlink fixture is unavailable: {type(exc).__name__}")
            with observed_cas_read(installed.path) as counts:
                with self.assertRaises(EvidenceStoreError) as rejected:
                    cas._read_verified_record_bytes(installed.record)
            self.assertEqual(rejected.exception.code, "blob_corrupt")
            self.assertEqual(counts["opens"], 0)

    def test_public_evidence_read_still_requires_store_issued_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            installed = cas.ingest_bytes(b"private physical validation is not authority", original_name="artifact.bin")
            # Invalid authority must fail before consulting any Store state.
            store = object.__new__(WorkspaceStore)
            authorization = {"purpose": "negative authority fixture"}
            forged = VerifiedEvidenceReadHandle(
                project_id="project.fixture", root_identity="root.fixture", project_commit=0,
                project_root_digest=digest("root"), closure_id="closure.fixture",
                closure_manifest_sha256=digest("closure"), evidence_id="evidence.fixture",
                evidence_revision=1, role="primary", ordinal=0,
                blob_sha256=installed.record.sha256,
                authorization_sha256=digest(canonical_json_bytes(authorization)),
            )
            with patch.object(WorkspaceStore, "_derive_evidence_read_handle", side_effect=AssertionError("invalid handle reached Store derivation")):
                for handle, expected in (
                    (installed.record, "verified_handle_required"),
                    (forged, "evidence_read_handle_authority_invalid"),
                ):
                    with self.subTest(handle=type(handle).__name__):
                        with self.assertRaises(EvidenceStoreError) as denied:
                            cas.read_bytes(handle, store=store, authorization=authorization)
                        self.assertEqual(denied.exception.code, expected)

    def test_exact_file_quarantine_classification_is_closed_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cas = evidence_cas(root / "workspace")
            ordinary = root / "ordinary.md"
            ordinary.write_bytes(b"one useful mathematical observation")
            secret = root / "sealed.txt"
            secret.write_bytes(b"-----BEGIN PRIVATE KEY-----\nopaque fixture")
            archive_named = root / "bundle.zip"
            archive_named.write_bytes(b"not actually an archive")

            self.assertIsNone(
                cas.classify_exact_file_quarantine(
                    ordinary,
                    expected_sha256=digest(ordinary.read_bytes()),
                    expected_length=ordinary.stat().st_size,
                    original_name=ordinary.name,
                    media_type="text/markdown",
                )
            )
            for path, media_type in (
                (secret, "text/plain"),
                (archive_named, "application/octet-stream"),
            ):
                self.assertEqual(
                    cas.classify_exact_file_quarantine(
                        path,
                        expected_sha256=digest(path.read_bytes()),
                        expected_length=path.stat().st_size,
                        original_name=path.name,
                        media_type=media_type,
                    ),
                    QUARANTINE_REASON_OPAQUE_RESTRICTED,
                )
            self.assertFalse(any(cas.staging_root.iterdir()))

    def test_blob_quarantine_state_and_closed_reason_are_biconditional(self) -> None:
        blob_sha256 = digest("quarantine-invariant")
        common = {
            "sha256": blob_sha256,
            "length": 1,
            "media_type": "application/octet-stream",
            "encoding": None,
            "logical_path": f"cas/sha256/{blob_sha256[:2]}/{blob_sha256[2:]}",
        }
        self.assertEqual(BlobRecord(**common).quarantine_state, "clear")
        self.assertEqual(
            BlobRecord(
                **common,
                quarantine_state="quarantined",
                quarantine_reason=QUARANTINE_REASON_OPAQUE_RESTRICTED,
            ).quarantine_reason,
            QUARANTINE_REASON_OPAQUE_RESTRICTED,
        )
        with self.assertRaises(ValueError):
            BlobRecord(
                **common,
                quarantine_state="quarantined",
                quarantine_reason=None,
            )
        with self.assertRaises(ValueError):
            BlobRecord(
                **common,
                quarantine_state="clear",
                quarantine_reason=QUARANTINE_REASON_OPAQUE_RESTRICTED,
            )
        with self.assertRaises(ValueError):
            BlobRecord(
                **common,
                quarantine_state="quarantined",
                quarantine_reason="provider said the payload contained a key",
            )

        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            with self.assertRaises(ValueError):
                cas.stage_bytes(
                    b"ordinary",
                    original_name="ordinary.bin",
                    quarantine_reason="model-generated explanation",
                )
            self.assertFalse(cas.staging_root.exists())

    def test_resolution_requirement_scope_is_closed_canonical_and_immutable(
        self,
    ) -> None:
        trigger = {"basis": "unresolved sign", "indices": [1, 3]}
        subject = {"branch_id": "branch.theta", "claim_id": "claim.T-001"}
        relation = build_resolution_requirement_relation(
            relation_id="relation.requirement.1",
            source_settled_session_output_evidence=("evidence.session-output", 2),
            target_requirement_evidence=("evidence.requirement", 1),
            session_id="session.17",
            requirement_kind="normalization",
            authored_trigger_basis=trigger,
            subject_scope=subject,
        )
        trigger["basis"] = "mutated after construction"
        subject["claim_id"] = "claim.other"

        payload = relation.to_payload()
        self.assertEqual(payload["relation_kind"], "resolution_requirement")
        self.assertEqual(
            payload["exact_scope"],
            {
                "session_id": "session.17",
                "requirement_kind": "normalization",
                "authored_trigger_basis": {
                    "basis": "unresolved sign",
                    "indices": [1, 3],
                },
                "subject_scope": {
                    "branch_id": "branch.theta",
                    "claim_id": "claim.T-001",
                },
            },
        )
        self.assertNotIn("resolved", payload)
        self.assertEqual(
            canonical_json_bytes(payload),
            canonical_json_bytes(relation.to_payload()),
        )

    def test_resolution_requirement_rejects_open_or_noncanonical_scope(self) -> None:
        base = {
            "session_id": "session.17",
            "requirement_kind": "collision",
            "authored_trigger_basis": {"basis": "identity collision"},
            "subject_scope": {"candidate_id": "candidate.1"},
        }
        for kind in (
            "normalization",
            "collision",
            "source",
            "review",
            "synthesis",
            "strategy",
        ):
            scope = ResolutionRequirementScope.from_payload(
                {**base, "requirement_kind": kind}
            )
            self.assertEqual(scope.to_payload()["requirement_kind"], kind)

        invalid_payloads = (
            {**base, "requirement_kind": "arbitrary"},
            {key: value for key, value in base.items() if key != "subject_scope"},
            {**base, "unexpected": True},
            {**base, "authored_trigger_basis": {}},
            {**base, "subject_scope": {"score": float("nan")}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    EvidenceRelation(
                        relation_id="relation.requirement.invalid",
                        relation_kind="resolution_requirement",
                        source_evidence=("evidence.session-output", 1),
                        target_evidence=("evidence.requirement", 1),
                        exact_scope=payload,
                    )

        legacy = EvidenceRelation(
            relation_id="relation.review",
            relation_kind="review",
            source_evidence=("evidence.A", 1),
            target_evidence=("evidence.B", 1),
            exact_scope="existing scalar scope",
        )
        self.assertEqual(legacy.to_payload()["exact_scope"], "existing scalar scope")

    def test_resolution_satisfaction_is_derived_only_from_exact_consumer_use_target(
        self,
    ) -> None:
        requirement = build_resolution_requirement_relation(
            relation_id="relation.requirement.1",
            source_settled_session_output_evidence=("evidence.session-output", 1),
            target_requirement_evidence=("evidence.requirement", 1),
            session_id="session.17",
            requirement_kind="review",
            authored_trigger_basis={"basis": "independent audit required"},
            subject_scope={"candidate_id": "candidate.1"},
        )
        unrelated_use = EvidenceRelation(
            relation_id="relation.use.other",
            relation_kind="consumer_use",
            source_evidence=("evidence.review", 1),
            target_evidence=("evidence.other-requirement", 1),
            exact_scope="consumer-owned use",
        )
        review_edge = EvidenceRelation(
            relation_id="relation.review",
            relation_kind="review",
            source_evidence=("evidence.review", 1),
            target_evidence=("evidence.requirement", 1),
            exact_scope="review alone is not satisfaction",
        )
        arbitrary_same_target_use = EvidenceRelation(
            relation_id="relation.use.same-target-but-not-satisfaction",
            relation_kind="consumer_use",
            source_evidence=("evidence.review", 1),
            target_evidence=("evidence.requirement", 1),
            exact_scope="ordinary consumer-owned use",
        )
        wrong_requirement = EvidenceRelation(
            relation_id="relation.use.wrong-requirement",
            relation_kind="consumer_use",
            source_evidence=("evidence.review", 1),
            target_evidence=("evidence.requirement", 1),
            exact_scope=ResolutionRequirementSatisfactionScope(
                requirement_relation_id="relation.requirement.other"
            ),
        )
        satisfaction = build_resolution_requirement_satisfaction_relation(
            relation_id="relation.use.requirement",
            source_resolving_evidence=("evidence.review", 1),
            requirement=requirement,
        )

        self.assertFalse(
            resolution_requirement_is_satisfied(
                requirement,
                (
                    unrelated_use,
                    review_edge,
                    arbitrary_same_target_use,
                    wrong_requirement,
                ),
            )
        )
        self.assertTrue(
            resolution_requirement_is_satisfied(
                requirement,
                (unrelated_use, satisfaction),
            )
        )
        self.assertEqual(
            satisfaction.to_payload()["exact_scope"],
            {
                "scope_kind": "resolution_requirement_satisfaction",
                "consumer_use": "satisfies_resolution_requirement",
                "requirement_relation_id": "relation.requirement.1",
            },
        )
        self.assertNotIn("resolved", requirement.to_payload())
        with self.assertRaises(ValueError):
            resolution_requirement_is_satisfied(review_edge, (satisfaction,))

        for circular_source in (
            requirement.source_evidence,
            requirement.target_evidence,
        ):
            with self.subTest(circular_source=circular_source):
                with self.assertRaises(ValueError):
                    build_resolution_requirement_satisfaction_relation(
                        relation_id="relation.use.circular",
                        source_resolving_evidence=circular_source,
                        requirement=requirement,
                    )

                circular = EvidenceRelation(
                    relation_id="relation.use.circular.manual",
                    relation_kind="consumer_use",
                    source_evidence=circular_source,
                    target_evidence=requirement.target_evidence,
                    exact_scope=ResolutionRequirementSatisfactionScope(
                        requirement_relation_id=requirement.relation_id
                    ),
                )
                self.assertFalse(
                    resolution_requirement_is_satisfied(
                        requirement,
                        (circular,),
                    )
                )

        with self.assertRaises(ValueError):
            build_resolution_requirement_relation(
                relation_id="relation.requirement.self",
                source_settled_session_output_evidence=("evidence.same", 1),
                target_requirement_evidence=("evidence.same", 1),
                session_id="session.17",
                requirement_kind="review",
                authored_trigger_basis={"basis": "must remain noncircular"},
                subject_scope={"candidate_id": "candidate.1"},
            )

    def test_real_cas_boundaries_emit_safe_events_and_sink_failure_is_non_authoritative(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "observed"
            root.mkdir(parents=True)
            observer = CaptureWorkspaceObserver()
            cas = EvidenceCAS(
                WorkspacePaths.from_root(root),
                observer=observer,
                observation_environment=ObservationEnvironment.CI,
            )
            installed = cas.ingest_bytes(
                b"observable Evidence",
                original_name="observable-evidence.txt",
            )
            cas.verify_record(installed.record)
            scrubbed = cas.scrub((installed.record,))
            self.assertEqual(scrubbed[0]["status"], "verified")
            event_kinds = tuple(event.kind for event in observer.events)
            for expected in (
                EventKind.CAS_STAGED,
                EventKind.CAS_INSTALLED,
                EventKind.CAS_VERIFIED,
                EventKind.CAS_SCRUBBED,
            ):
                self.assertIn(expected, event_kinds)
            self.assertTrue(
                all(
                    event.environment is ObservationEnvironment.CI
                    for event in observer.events
                )
            )
            self.assertTrue(
                all(
                    "proof_bytes" not in event.attributes
                    and "worker_output" not in event.attributes
                    for event in observer.events
                )
            )

            class FailingObserver:
                def emit_event(self, event):
                    raise RuntimeError("observer unavailable")

                def observe_metric(self, sample):
                    raise RuntimeError("observer unavailable")

            failing_root = Path(temporary) / "failing-observer"
            failing_root.mkdir(parents=True)
            failing_cas = EvidenceCAS(
                WorkspacePaths.from_root(failing_root),
                observer=FailingObserver(),
            )
            authoritative = failing_cas.ingest_bytes(
                b"still authoritative",
                original_name="authoritative.txt",
            )
            failing_cas.verify_record(authoritative.record)
            self.assertTrue(authoritative.path.is_file())

    def test_security_disposition_factory_seals_exact_unavailable_evidence_and_authorization(
        self,
    ) -> None:
        blob_one = digest("security-blob-one")
        blob_two = digest("security-blob-two")
        evidence = EvidenceItemRevision(
            evidence_id="evidence.security.1",
            revision=3,
            subtype="security_disposition",
            subject={"candidate_id": "candidate.1"},
            exact_scope="exact unavailable revision",
            rigor="diagnostic",
            limitations=("security disposition only",),
            non_inferences=("does not establish RH",),
            security_classification="restricted",
            retention="security-policy",
            blob_roles=(("primary", blob_one), ("support", blob_two)),
            availability_state="unavailable",
        )
        authorization = {
            "authorization_kind": "human_security_disposition",
            "authorized_by": "principal.justin",
            "scope": {
                "evidence_id": evidence.evidence_id,
                "revision": evidence.revision,
            },
        }
        reason = "  exact security disposition reason  "
        disposition = prepare_evidence_security_disposition(
            evidence=evidence,
            authorization=authorization,
            reason=reason,
            blob_sha256s=(blob_two, blob_one),
            directive_id="directive.security.1",
            tombstone_id="tombstone.security.1",
        )
        disposition.verify_issued()
        disposition.directive.verify_issued()
        disposition.tombstone.verify_issued()
        self.assertEqual(
            disposition.authorization_sha256,
            digest(canonical_json_bytes(authorization)),
        )
        self.assertEqual(disposition.directive.reason, reason)
        self.assertEqual(
            disposition.directive.blob_sha256s,
            tuple(sorted((blob_one, blob_two))),
        )
        self.assertEqual(
            disposition.directive.evidence_references,
            ((evidence.evidence_id, evidence.revision),),
        )
        self.assertEqual(
            disposition.tombstone.reason_sha256,
            digest(reason),
        )
        self.assertEqual(
            set(disposition.directive.to_payload()),
            {
                "directive_id",
                "authorization_sha256",
                "reason",
                "blob_sha256s",
                "evidence_references",
                "lifecycle",
            },
        )
        self.assertEqual(
            set(disposition.tombstone.to_payload()),
            {
                "tombstone_id",
                "evidence_id",
                "evidence_revision",
                "directive_id",
                "reason_sha256",
            },
        )

        direct_directive = DeletionDirective(
            **{
                **disposition.directive.to_payload(),
                "blob_sha256s": tuple(
                    disposition.directive.to_payload()["blob_sha256s"]
                ),
                "evidence_references": tuple(
                    tuple(item)
                    for item in disposition.directive.to_payload()[
                        "evidence_references"
                    ]
                ),
            }
        )
        direct_tombstone = EvidenceTombstone(**disposition.tombstone.to_payload())
        for unissued in (
            direct_directive,
            direct_tombstone,
            replace(disposition.directive),
            replace(disposition.tombstone),
        ):
            with (
                self.subTest(value=unissued),
                self.assertRaises(EvidenceStoreError) as rejected,
            ):
                unissued.verify_issued()
            self.assertEqual(
                rejected.exception.code,
                "evidence_security_authority_invalid",
            )
        with self.assertRaises(EvidenceStoreError):
            replace(disposition).verify_issued()
        with self.assertRaises(EvidenceStoreError):
            EvidenceSecurityDisposition(
                disposition.directive,
                disposition.tombstone,
                disposition.authorization_sha256,
                disposition.evidence_payload_sha256,
            ).verify_issued()

        tampered = prepare_evidence_security_disposition(
            evidence=evidence,
            authorization=authorization,
            reason=reason,
            blob_sha256s=(blob_one, blob_two),
            directive_id="directive.security.2",
            tombstone_id="tombstone.security.2",
        )
        object.__setattr__(tampered.directive, "reason", "forged reason")
        with self.assertRaises(EvidenceStoreError):
            tampered.directive.verify_issued()
        with self.assertRaises(EvidenceStoreError):
            tampered.verify_issued()

    def test_security_disposition_factory_rejects_available_partial_and_duplicate_sources(
        self,
    ) -> None:
        blob_one = digest("security-blob-one")
        blob_two = digest("security-blob-two")
        unavailable = EvidenceItemRevision(
            evidence_id="evidence.security.2",
            revision=1,
            subtype="security_disposition",
            subject={"candidate_id": "candidate.1"},
            exact_scope="exact unavailable revision",
            rigor="diagnostic",
            limitations=(),
            non_inferences=("does not establish RH",),
            security_classification="restricted",
            retention="security-policy",
            blob_roles=(("primary", blob_one), ("support", blob_two)),
            availability_state="unavailable",
        )
        common = {
            "authorization": {"authorized_by": "principal.justin"},
            "reason": "security reason",
            "directive_id": "directive.security.invalid",
            "tombstone_id": "tombstone.security.invalid",
        }
        with self.assertRaises(EvidenceStoreError):
            prepare_evidence_security_disposition(
                evidence=replace(unavailable, availability_state="verified_available"),
                blob_sha256s=(blob_one, blob_two),
                **common,
            )
        for invalid_blobs in ((blob_one,), (blob_one, blob_one, blob_two)):
            with (
                self.subTest(blobs=invalid_blobs),
                self.assertRaises(EvidenceStoreError),
            ):
                prepare_evidence_security_disposition(
                    evidence=unavailable,
                    blob_sha256s=invalid_blobs,
                    **common,
                )
        with self.assertRaises(EvidenceStoreError):
            prepare_evidence_security_disposition(
                evidence=unavailable,
                authorization={},
                reason="security reason",
                blob_sha256s=(blob_one, blob_two),
                directive_id="directive.security.invalid",
                tombstone_id="tombstone.security.invalid",
            )

    def test_staged_blob_issuance_blocks_construction_replace_and_install_rescan_bypass(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            staged = cas.stage_bytes(b"ordinary evidence", original_name="ordinary.txt")
            staged.verify_issued()

            direct = StagedBlob(
                ingestion_id=staged.ingestion_id,
                stage_directory=staged.stage_directory,
                payload_path=staged.payload_path,
                sha256=staged.sha256,
                length=staged.length,
                media_type=staged.media_type,
                encoding=staged.encoding,
                original_name=staged.original_name,
                quarantine_reason=staged.quarantine_reason,
            )
            for unissued in (
                direct,
                replace(staged),
                replace(staged, quarantine_reason="forged"),
            ):
                with (
                    self.subTest(value=unissued),
                    self.assertRaises(EvidenceStoreError) as rejected,
                ):
                    unissued.verify_issued()
                self.assertEqual(
                    rejected.exception.code,
                    "staged_blob_authority_invalid",
                )
            with self.assertRaises(EvidenceStoreError) as install_rejected:
                cas.install(replace(staged))
            self.assertEqual(
                install_rejected.exception.code,
                "staged_blob_authority_invalid",
            )

            secret = b"client_secret=install-rescan-fixture"
            with patch(
                "research_core.evidence_store._contains_secret_like",
                return_value=False,
            ):
                staged_secret = cas.stage_bytes(
                    secret,
                    original_name="rescan.txt",
                )
            staged_secret.verify_issued()
            with self.assertRaises(EvidenceStoreError) as rescanned:
                cas.install(staged_secret)
            self.assertEqual(
                rescanned.exception.code,
                "secret_payload_requires_quarantine",
            )
            self.assertFalse(cas.path_for_digest(staged_secret.sha256).exists())

    def test_closure_issuance_distinguishes_verified_commit_from_read_rehydration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            record = cas.ingest_bytes(b"evidence", original_name="evidence.txt").record
            manifest = valid_closure(cas, record)
            manifest.verify_issued()
            manifest.verify_issued(for_commit=True)

            rehydrated = closure_manifest_from_payload(manifest.to_payload())
            rehydrated.verify_issued()
            with self.assertRaises(EvidenceStoreError) as read_only:
                rehydrated.verify_issued(for_commit=True)
            self.assertEqual(
                read_only.exception.code,
                "evidence_closure_authority_invalid",
            )

            direct = ClosureManifest(
                closure_id=manifest.closure_id,
                root=manifest.root,
                members=manifest.members,
                omissions=manifest.omissions,
                conflicts=manifest.conflicts,
                canonical_pre_state=manifest.canonical_pre_state,
                manifest_sha256=manifest.manifest_sha256,
            )
            for unissued in (direct, replace(manifest)):
                with (
                    self.subTest(value=unissued),
                    self.assertRaises(EvidenceStoreError) as rejected,
                ):
                    unissued.verify_issued()
                self.assertEqual(
                    rejected.exception.code,
                    "evidence_closure_authority_invalid",
                )

            tampered = valid_closure(cas, record)
            object.__setattr__(tampered, "closure_id", "closure.tampered")
            with self.assertRaises(EvidenceStoreError) as invalid_seal:
                tampered.verify_issued(for_commit=True)
            self.assertEqual(
                invalid_seal.exception.code,
                "evidence_closure_authority_invalid",
            )

    def test_install_deduplicates_bytes_without_collapsing_evidentiary_acts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            canonical = Path(directory) / "research_state.json"
            canonical.write_bytes(b'{"proof_status":"incomplete"}\n')
            canonical_before = canonical.read_bytes()
            cas = evidence_cas(root)
            first = cas.ingest_bytes(
                b"independent exact derivation",
                original_name="derivation.txt",
                media_type="text/plain",
                encoding="utf-8",
            )
            second = cas.ingest_bytes(
                b"independent exact derivation",
                original_name="review.txt",
                media_type="text/plain",
                encoding="utf-8",
            )
            self.assertFalse(first.deduplicated)
            self.assertTrue(second.deduplicated)
            self.assertEqual(first.record.sha256, second.record.sha256)
            self.assertTrue(file_is_owner_read_only(first.path))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(first.path.stat().st_mode), stat.S_IRUSR)
            self.assertFalse(first.ordinary_available)
            self.assertEqual(
                first.record.availability_state, "installed_pending_metadata"
            )
            with self.assertRaises(EvidenceStoreError) as uncommitted_read:
                cas.read_bytes(first.record, store=object())  # type: ignore[arg-type]
            self.assertEqual(
                uncommitted_read.exception.code, "verified_handle_required"
            )

            evidence_a = EvidenceItemRevision(
                evidence_id="evidence.A",
                revision=1,
                subtype="derivation",
                subject={"candidate_id": "candidate.1"},
                exact_scope="one finite identity",
                rigor="exact_symbolic",
                limitations=("does not establish positivity",),
                non_inferences=("does not establish RH",),
                security_classification="internal",
                retention="retain",
                blob_roles=(("primary", first.record.sha256),),
            )
            evidence_b = replace(
                evidence_a, evidence_id="evidence.B", subtype="independent_review"
            )
            provenance_a = ProvenanceEvent(
                "prov.A",
                "evidence.A",
                1,
                "session.A",
                "derive",
                "agent.A",
                None,
                (),
                None,
                "sealed",
            )
            provenance_b = ProvenanceEvent(
                "prov.B",
                "evidence.B",
                1,
                "session.B",
                "review",
                "agent.B",
                None,
                (),
                None,
                "sealed",
            )
            disclosure = IndependenceDisclosure(
                "ind.B",
                "evidence.B",
                1,
                "model-x",
                ("candidate statement",),
                "fresh derivation",
                ("source.theta",),
                "separate implementation",
                "fixture",
                ("same coordinator",),
            )
            self.assertNotEqual(evidence_a.evidence_id, evidence_b.evidence_id)
            self.assertNotEqual(provenance_a.to_payload(), provenance_b.to_payload())
            self.assertEqual(disclosure.to_payload()["evidence_id"], "evidence.B")
            closure = valid_closure(cas, first.record)
            verify_closure_manifest(
                closure,
                blobs={first.record.sha256: first.record},
                cas=cas,
            )
            self.assertEqual(closure.blob_sha256s, (first.record.sha256,))
            self.assertEqual(closure.canonical_effect, "none")
            self.assertEqual(canonical.read_bytes(), canonical_before)

    def test_typed_closure_rejects_missing_unavailable_unreachable_and_deleted_members(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            record = cas.ingest_bytes(b"evidence", original_name="evidence.txt").record
            members = list(closure_members(record.sha256))
            evidence_index = next(
                index
                for index, member in enumerate(members)
                if member.kind == "evidence"
            )
            evidence = members[evidence_index]
            members[evidence_index] = replace(
                evidence,
                references=tuple(
                    item
                    for item in evidence.references
                    if item.relation != "provenance"
                ),
            )
            with self.assertRaises(EvidenceStoreError) as missing_provenance:
                build_closure_manifest(
                    closure_id="closure.missing-provenance",
                    root=ClosureReference("candidate", "candidate", "candidate.1", 1),
                    members=members,
                    blobs={record.sha256: record},
                    cas=cas,
                    canonical_pre_state={"sha256": digest("canonical")},
                )
            self.assertEqual(
                missing_provenance.exception.code, "evidence_closure_invalid"
            )

            members = list(closure_members(record.sha256))
            dependency_index = next(
                index
                for index, member in enumerate(members)
                if member.kind == "dependency"
            )
            members[dependency_index] = replace(
                members[dependency_index], available=False
            )
            with self.assertRaises(EvidenceStoreError):
                build_closure_manifest(
                    closure_id="closure.unavailable",
                    root=ClosureReference("candidate", "candidate", "candidate.1", 1),
                    members=members,
                    blobs={record.sha256: record},
                    cas=cas,
                    canonical_pre_state={"sha256": digest("canonical")},
                )

            members = list(closure_members(record.sha256))
            members.append(
                ClosureMember("review", "review.unreachable", 1, digest("review"))
            )
            with self.assertRaises(EvidenceStoreError) as unreachable:
                build_closure_manifest(
                    closure_id="closure.unreachable",
                    root=ClosureReference("candidate", "candidate", "candidate.1", 1),
                    members=members,
                    blobs={record.sha256: record},
                    cas=cas,
                    canonical_pre_state={"sha256": digest("canonical")},
                )
            self.assertIn("unreachable", str(unreachable.exception))

            directive = DeletionDirective(
                "delete.evidence.1",
                digest("authorization"),
                "security deletion fixture",
                evidence_references=(("evidence.1", 1),),
            )
            tombstone = EvidenceTombstone(
                "tombstone.1", "evidence.1", 1, directive.directive_id, digest("reason")
            )
            with self.assertRaises(EvidenceStoreError) as deleted:
                build_closure_manifest(
                    closure_id="closure.deleted",
                    root=ClosureReference("candidate", "candidate", "candidate.1", 1),
                    members=closure_members(record.sha256),
                    blobs={record.sha256: record},
                    cas=cas,
                    canonical_pre_state={"sha256": digest("canonical")},
                    deletion_directives=(directive,),
                    tombstones=(tombstone,),
                )
            self.assertIn("tombstoned", str(deleted.exception))

            empty = ClosureManifest(
                closure_id="closure.empty",
                root=ClosureReference("candidate", "candidate", "candidate.1", 1),
                members=(),
                omissions=(),
                conflicts=(),
                canonical_pre_state={"sha256": digest("canonical")},
                manifest_sha256="0" * 64,
            )
            with self.assertRaises(EvidenceStoreError):
                verify_closure_manifest(empty, blobs={}, cas=cas)

            without_dependency = tuple(
                member
                for member in closure_members(record.sha256)
                if member.kind != "dependency"
            )
            without_dependency = tuple(
                replace(
                    member,
                    references=tuple(
                        reference
                        for reference in member.references
                        if reference.target_kind != "dependency"
                    ),
                )
                for member in without_dependency
            )
            incomplete = ClosureManifest(
                closure_id="closure.no-dependency",
                root=ClosureReference("candidate", "candidate", "candidate.1", 1),
                members=without_dependency,
                omissions=(),
                conflicts=(),
                canonical_pre_state={"sha256": digest("canonical")},
                manifest_sha256="0" * 64,
            )
            incomplete = replace(
                incomplete,
                manifest_sha256=digest(canonical_json_bytes(incomplete.body_payload())),
            )
            with self.assertRaises(EvidenceStoreError) as missing_dependency:
                verify_closure_manifest(
                    incomplete,
                    blobs={record.sha256: record},
                    cas=cas,
                )
            self.assertIn("dependency", str(missing_dependency.exception))

    def test_pre_and_post_install_faults_leave_only_recoverable_staging_or_orphan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            staged = cas.stage_bytes(b"pre-install", original_name="pre.txt")

            def fail_before(phase, _staged, _destination):
                if phase == "before_install":
                    raise RuntimeError("simulated crash")

            with self.assertRaisesRegex(RuntimeError, "simulated"):
                cas.install(staged, fault_hook=fail_before)
            self.assertTrue(staged.payload_path.exists())
            self.assertFalse(cas.path_for_digest(staged.sha256).exists())
            cas.remove_unreferenced_staging(staged.stage_directory)

            staged_after = cas.stage_bytes(b"post-install", original_name="post.txt")

            def fail_after(phase, _staged, _destination):
                if phase == "after_install":
                    raise RuntimeError("simulated crash")

            with self.assertRaisesRegex(RuntimeError, "simulated"):
                cas.install(staged_after, fault_hook=fail_after)
            orphan = cas.path_for_digest(staged_after.sha256)
            self.assertTrue(orphan.exists())
            self.assertTrue(file_is_owner_read_only(orphan))
            self.assertEqual(cas.discover_orphans(()), (staged_after.sha256,))

    def test_cas_capacity_exhaustion_has_closed_stage_and_install_dispositions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            real_fsync = os.fsync
            payload_fsync_failed = False

            def exhaust_payload_fsync_once(descriptor: int) -> None:
                nonlocal payload_fsync_failed
                if not payload_fsync_failed:
                    payload_fsync_failed = True
                    raise OSError(errno.ENOSPC, "no space left")
                real_fsync(descriptor)

            with patch(
                "research_core.evidence_store.os.fsync",
                side_effect=exhaust_payload_fsync_once,
            ):
                with self.assertRaises(EvidenceStoreError) as staging_failure:
                    cas.stage_bytes(b"partial", original_name="partial.txt")
            self.assertEqual(staging_failure.exception.code, "cas_capacity_exhausted")
            self.assertEqual(tuple(cas.staging_root.iterdir()), ())
            self.assertEqual(cas.discover_orphans(()), ())

            staged = cas.stage_bytes(b"retryable", original_name="retryable.txt")
            with patch(
                "research_core.evidence_store.os.link",
                side_effect=OSError(errno.ENOSPC, "no space left"),
            ):
                with self.assertRaises(EvidenceStoreError) as install_failure:
                    cas.install(staged)
            self.assertEqual(install_failure.exception.code, "cas_capacity_exhausted")
            self.assertTrue(staged.payload_path.is_file())
            self.assertFalse(cas.path_for_digest(staged.sha256).exists())
            self.assertEqual(cas.discover_orphans(()), ())

            installed = cas.install(staged)
            self.assertEqual(installed.record.sha256, staged.sha256)
            self.assertTrue(installed.path.is_file())

    def test_failed_stage_cleanup_ambiguity_is_an_integrity_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            with (
                patch(
                    "research_core.evidence_store.os.fsync",
                    side_effect=OSError(errno.ENOSPC, "no space left"),
                ),
                patch(
                    "research_core.evidence_store.shutil.rmtree",
                    side_effect=OSError(errno.EACCES, "cleanup denied"),
                ),
            ):
                with self.assertRaises(IntegrityFreeze) as captured:
                    cas.stage_bytes(b"ambiguous", original_name="ambiguous.txt")
            self.assertEqual(captured.exception.code, "cas_staging_cleanup_failed")
            self.assertEqual(len(tuple(cas.staging_root.iterdir())), 1)

    def test_existing_digest_path_with_different_bytes_is_integrity_freeze(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            installed = cas.ingest_bytes(b"trusted", original_name="trusted.txt")
            os.chmod(
                installed.path,
                stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
            )
            installed.path.write_bytes(b"tampered")
            staged = cas.stage_bytes(b"trusted", original_name="retry.txt")
            with self.assertRaises(IntegrityFreeze) as captured:
                cas.install(staged)
            self.assertEqual(captured.exception.code, "same_digest_conflict")

    def test_missing_corrupt_quarantined_and_metadata_unavailable_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            installed = cas.ingest_bytes(b"available", original_name="available.txt")
            os.chmod(
                installed.path,
                stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
            )
            installed.path.unlink()
            with self.assertRaises(EvidenceStoreError) as missing:
                cas.verify_record(installed.record)
            self.assertEqual(missing.exception.code, "blob_missing")

            installed = cas.ingest_bytes(b"available", original_name="available.txt")
            os.chmod(
                installed.path,
                stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
            )
            installed.path.write_bytes(b"corrupt")
            with self.assertRaises(EvidenceStoreError) as corrupt:
                cas.verify_record(installed.record)
            self.assertEqual(corrupt.exception.code, "blob_corrupt")

            quarantined = cas.ingest_bytes(
                b"-----BEGIN EC PRIVATE KEY-----\nrestricted",
                original_name="restricted.txt",
                quarantine_reason=QUARANTINE_REASON_OPAQUE_RESTRICTED,
            )
            with self.assertRaises(EvidenceStoreError) as denied:
                build_closure_manifest(
                    closure_id="closure.quarantined",
                    root=ClosureReference("candidate", "candidate", "candidate.1", 1),
                    members=closure_members(quarantined.record.sha256),
                    blobs={quarantined.record.sha256: quarantined.record},
                    cas=cas,
                    canonical_pre_state={"sha256": digest("canonical")},
                )
            self.assertEqual(denied.exception.code, "evidence_closure_invalid")
            unavailable = replace(
                quarantined.record,
                quarantine_state="clear",
                quarantine_reason=None,
                availability_state="unavailable",
            )
            with self.assertRaises(EvidenceStoreError) as unavailable_error:
                build_closure_manifest(
                    closure_id="closure.unavailable",
                    root=ClosureReference("candidate", "candidate", "candidate.1", 1),
                    members=closure_members(unavailable.sha256),
                    blobs={unavailable.sha256: unavailable},
                    cas=cas,
                    canonical_pre_state={"sha256": digest("canonical")},
                )
            self.assertEqual(
                unavailable_error.exception.code, "evidence_closure_invalid"
            )

    def test_path_archive_expanded_secret_and_bulk_import_attacks_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(TypeError):
                EvidenceCAS()  # type: ignore[call-arg]
            with self.assertRaises(TypeError):
                EvidenceCAS(root / "workspace")  # type: ignore[arg-type]
            cas = evidence_cas(root / "workspace")
            self.assertFalse(hasattr(cas, "max_bytes"))
            with self.assertRaises(TypeError):
                EvidenceCAS(cas.paths, max_bytes=1)  # type: ignore[call-arg]
            for unsafe in (
                "../escape.txt",
                "a/../../escape.txt",
                "C:/absolute.txt",
                "a\\b.txt",
            ):
                with self.subTest(unsafe=unsafe), self.assertRaises(EvidenceStoreError):
                    cas.stage_bytes(b"x", original_name=unsafe)
            for archive in (
                b"PK\x03\x04archive",
                b"\x1f\x8bcompressed",
                b"BZhcompressed",
            ):
                with (
                    self.subTest(archive=archive),
                    self.assertRaises(EvidenceStoreError) as captured,
                ):
                    cas.stage_bytes(archive, original_name="renamed.bin")
                self.assertEqual(captured.exception.code, "archive_payload_forbidden")
            secret_cases = (
                b"-----BEGIN EC PRIVATE KEY-----",
                b"-----BEGIN DSA PRIVATE KEY-----",
                b"-----BEGIN OPENSSH PRIVATE KEY-----",
                b"-----BEGIN PGP PRIVATE KEY BLOCK-----",
                b"AKIAABCDEFGHIJKLMNOP",
                b"client_secret=not-real",
                b'"private_key": "not-real"',
                b"AccountKey=not-real",
                b"RAILWAY_API_TOKEN=not-real-but-secret",
                b"ghp_abcdefghijklmnopqrstuvwxyz",
                b"x" * 8192 + b"-----BEGIN PRIVATE KEY-----",
                "-----BEGIN PRIVATE KEY-----\nsecret".encode("utf-16-le"),
                "-----BEGIN PRIVATE KEY-----\nsecret".encode("utf-16-be"),
            )
            for index, secret in enumerate(secret_cases):
                with (
                    self.subTest(index=index),
                    self.assertRaises(EvidenceStoreError) as captured,
                ):
                    cas.stage_bytes(secret, original_name=f"secret-{index}.txt")
                self.assertEqual(
                    captured.exception.code, "secret_payload_requires_quarantine"
                )
            quarantined = cas.ingest_bytes(
                "-----BEGIN PRIVATE KEY-----\nsecret".encode("utf-16-le"),
                original_name="encoded-secret.bin",
                quarantine_reason=QUARANTINE_REASON_OPAQUE_RESTRICTED,
            )
            forged_clear = replace(
                quarantined.record,
                quarantine_state="clear",
                quarantine_reason=None,
            )
            with self.assertRaises(EvidenceStoreError) as reclassified:
                cas.verify_record(forged_clear)
            self.assertEqual(
                reclassified.exception.code,
                "secret_payload_requires_quarantine",
            )
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "one.txt").write_text("one", encoding="utf-8")
            with self.assertRaises(EvidenceStoreError) as bulk:
                cas.stage_file(artifacts)
            self.assertEqual(bulk.exception.code, "selected_file_required")
            self.assertFalse(hasattr(cas, "bulk_import"))

    def test_evidence_availability_gate_rejects_missing_metadata_and_quarantine(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = evidence_cas(Path(directory) / "workspace")
            installed = cas.ingest_bytes(b"evidence", original_name="evidence.txt")
            evidence = EvidenceItemRevision(
                evidence_id="evidence.1",
                revision=1,
                subtype="test",
                subject={"claim_id": "claim.C-1"},
                exact_scope="fixture",
                rigor="diagnostic",
                limitations=(),
                non_inferences=("no proof effect",),
                security_classification="internal",
                retention="retain",
                blob_roles=(("primary", installed.record.sha256),),
            )
            self.assertEqual(evidence.availability_state, "pending_closure")
            with self.assertRaises(EvidenceStoreError) as uncommitted:
                cas.verify_record(installed.record, ordinary_use=True)
            self.assertEqual(uncommitted.exception.code, "verified_handle_required")


if __name__ == "__main__":
    unittest.main()
