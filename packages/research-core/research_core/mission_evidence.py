"""Raw capture custody and proof-neutral Evidence meaning for RH research.

Raw captures preserve exact source material and completion facts. Evidence
revisions record independently correctable mathematical interpretation under
the active direct Executive Epoch. Neither surface changes canonical
mathematics or invents a Session, Attempt, aggregate acceptance lane, or
process-local semantic seal.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_store import (
    BlobRecord,
    EvidenceCAS,
    EvidenceItemRevision,
    cas_relative_path,
    validate_quarantine_reason,
)
from .json_support import canonical_json_bytes
from .mission_executive import DirectExecutiveEpochAuthority
from .research_model import deep_freeze, deep_thaw
from .workspace_store import (
    CommandOutcome,
    StaleCommandError,
    WorkspaceIntegrityError,
    WorkspaceStore,
    WriterLease,
    canonical_raw_capture_id,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_INTERPRETATION_STRENGTHS = frozenset(
    {
        "heuristic",
        "numerical_diagnostic",
        "exact_finite_identity",
        "rigorous_bounded_result",
        "conditional_theorem",
        "general_theorem",
        "formal_certificate",
        "exact_counterexample",
        "source",
        "objection",
        "no_result",
        "uninterpreted",
    }
)


def _sha256(value: bytes | Mapping[str, Any]) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_digest(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _require_strings(
    values: Sequence[str],
    label: str,
    *,
    nonempty: bool = True,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be a sequence of strings")
    result = tuple(
        _require_text(value, f"{label}[{index}]") for index, value in enumerate(values)
    )
    if nonempty and not result:
        raise ValueError(f"{label} must not be empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return result


# Raw custody is factual. Each independently correctable interpretation owns
# an ordinary Evidence identity and head.
EVIDENCE_MEANING_SUBTYPE = "evidence_meaning"


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _require_json_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    # Canonical encoding is both the supported-value check and the exact
    # normalization boundary.  Deep freezing prevents later caller mutation.
    canonical_json_bytes(value)
    return deep_freeze(deep_thaw(value))


@dataclass(frozen=True, slots=True)
class RawCaptureArtifactInput:
    """One exact artifact handed into immutable raw-capture custody."""

    role: str
    logical_name: str
    content_bytes: bytes = field(repr=False)
    media_type: str = "application/octet-stream"
    encoding: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.role, "artifact.role")
        _require_text(self.logical_name, "artifact.logical_name")
        if type(self.content_bytes) is not bytes:
            raise TypeError("artifact.content_bytes must be exact bytes")
        _require_text(self.media_type, "artifact.media_type")
        if self.encoding is not None:
            _require_text(self.encoding, "artifact.encoding")

    def descriptor(self, ordinal: int) -> "RawCaptureArtifactDescriptor":
        return RawCaptureArtifactDescriptor(
            ordinal=_require_nonnegative_int(ordinal, "artifact.ordinal"),
            role=self.role,
            logical_name=self.logical_name,
            blob_sha256=_sha256(self.content_bytes),
        )


@dataclass(frozen=True, slots=True)
class RawCaptureArtifactFileInput:
    """One exact sealed file handed into immutable raw-capture custody."""

    role: str
    logical_name: str
    source_path: Path
    sha256: str
    byte_length: int
    media_type: str = "application/octet-stream"
    encoding: str | None = None
    quarantine_reason: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.role, "artifact.role")
        _require_text(self.logical_name, "artifact.logical_name")
        try:
            source_path = Path(self.source_path)
        except TypeError as exc:
            raise TypeError("artifact.source_path must be one filesystem path") from exc
        if not source_path.is_absolute():
            raise ValueError("artifact.source_path must be absolute")
        object.__setattr__(self, "source_path", source_path)
        _require_digest(self.sha256, "artifact.sha256")
        _require_nonnegative_int(self.byte_length, "artifact.byte_length")
        _require_text(self.media_type, "artifact.media_type")
        if self.encoding is not None:
            _require_text(self.encoding, "artifact.encoding")
        validate_quarantine_reason(self.quarantine_reason)

    def descriptor(self, ordinal: int) -> "RawCaptureArtifactDescriptor":
        return RawCaptureArtifactDescriptor(
            ordinal=_require_nonnegative_int(ordinal, "artifact.ordinal"),
            role=self.role,
            logical_name=self.logical_name,
            blob_sha256=self.sha256,
        )


@dataclass(frozen=True, slots=True)
class RawCaptureArtifactDescriptor:
    """Stable raw-capture locator; bytes remain in the one Workspace CAS."""

    ordinal: int
    role: str
    logical_name: str
    blob_sha256: str

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.ordinal, "artifact.ordinal")
        _require_text(self.role, "artifact.role")
        _require_text(self.logical_name, "artifact.logical_name")
        _require_digest(self.blob_sha256, "artifact.blob_sha256")

    def to_payload(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "role": self.role,
            "logical_name": self.logical_name,
            "blob_sha256": self.blob_sha256,
        }


@dataclass(frozen=True, slots=True)
class RawCaptureArtifactContent:
    """One lossless raw artifact read from committed factual custody."""

    capture_id: str
    mission_id: str
    artifact: RawCaptureArtifactDescriptor
    media_type: str
    encoding: str | None
    content_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require_text(self.capture_id, "capture_id")
        _require_text(self.mission_id, "mission_id")
        if type(self.artifact) is not RawCaptureArtifactDescriptor:
            raise TypeError("raw artifact content requires its exact descriptor")
        _require_text(self.media_type, "media_type")
        if self.encoding is not None:
            _require_text(self.encoding, "encoding")
        if type(self.content_bytes) is not bytes:
            raise TypeError("raw artifact content must be exact bytes")
        if _sha256(self.content_bytes) != self.artifact.blob_sha256:
            raise WorkspaceIntegrityError(
                "raw artifact bytes differ from their committed descriptor"
            )


@dataclass(frozen=True, slots=True)
class RawCaptureRecord:
    """One observation-derived capture with zero semantic interpretation."""

    capture_id: str
    project_id: str
    mission_id: str
    executive_epoch_id: str | None
    capture_kind: str
    observation_id: str
    assignment_id: str
    provenance: Mapping[str, Any]
    completion: Mapping[str, Any] | None
    artifacts: tuple[RawCaptureArtifactDescriptor, ...]
    digest_sha256: str
    created_at: str | None = None
    _artifact_inputs: tuple[
        RawCaptureArtifactInput | RawCaptureArtifactFileInput, ...
    ] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.mission_id, "mission_id")
        if self.executive_epoch_id is not None:
            _require_text(self.executive_epoch_id, "executive_epoch_id")
        if self.capture_kind not in {"assignment", "output"}:
            raise ValueError("capture_kind must be assignment or output")
        _require_text(self.observation_id, "observation_id")
        _require_text(self.assignment_id, "assignment_id")
        object.__setattr__(
            self, "provenance", _require_json_mapping(self.provenance, "provenance")
        )
        if self.completion is not None:
            object.__setattr__(
                self,
                "completion",
                _require_json_mapping(self.completion, "completion"),
            )
        if self.capture_kind == "assignment" and self.completion is not None:
            raise ValueError("assignment capture cannot carry output completion facts")
        if not self.artifacts:
            raise ValueError("raw capture requires at least one artifact")
        if any(
            artifact.ordinal != ordinal
            for ordinal, artifact in enumerate(self.artifacts)
        ):
            raise ValueError("raw capture artifact ordinals must be contiguous")
        expected_id = canonical_raw_capture_id(
            project_id=self.project_id,
            capture_kind=self.capture_kind,
            observation_id=self.observation_id,
        )
        if self.capture_id != expected_id:
            raise ValueError("raw capture identity is not observation-derived")
        if self.digest_sha256 != _sha256(self.to_payload()):
            raise ValueError("raw capture digest is stale")
        if self.created_at is not None:
            _require_text(self.created_at, "created_at")
        if self._artifact_inputs:
            if len(self._artifact_inputs) != len(self.artifacts):
                raise ValueError("raw capture artifact input inventory is stale")
            for ordinal, (artifact_input, descriptor) in enumerate(
                zip(self._artifact_inputs, self.artifacts, strict=True)
            ):
                if artifact_input.descriptor(ordinal) != descriptor:
                    raise ValueError("raw capture artifact bytes are stale")

    def to_payload(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "project_id": self.project_id,
            "mission_id": self.mission_id,
            "executive_epoch_id": self.executive_epoch_id,
            "capture_kind": self.capture_kind,
            "observation_id": self.observation_id,
            "assignment_id": self.assignment_id,
            "provenance": deep_thaw(self.provenance),
            "completion": (
                None if self.completion is None else deep_thaw(self.completion)
            ),
            "artifacts": [artifact.to_payload() for artifact in self.artifacts],
        }

    def store_record(self) -> dict[str, Any]:
        payload = self.to_payload()
        payload.pop("project_id")
        payload.pop("artifacts")
        return payload

    def verify_prepared(self) -> None:
        if type(self) is not RawCaptureRecord:
            raise ValueError("RawCaptureRecord subclasses have no commit authority")
        if not self._artifact_inputs:
            raise ValueError("raw capture commit requires prepared custody")
        self.__post_init__()


def _raw_capture_expected_blobs(
    record: RawCaptureRecord,
) -> tuple[BlobRecord, ...]:
    """Build and de-duplicate the complete declared physical custody facts."""

    expected_by_digest: dict[str, BlobRecord] = {}
    for artifact_input in record._artifact_inputs:
        if type(artifact_input) is RawCaptureArtifactInput:
            sha256 = _sha256(artifact_input.content_bytes)
            byte_length = len(artifact_input.content_bytes)
            quarantine_reason = None
        else:
            assert type(artifact_input) is RawCaptureArtifactFileInput
            sha256 = artifact_input.sha256
            byte_length = artifact_input.byte_length
            quarantine_reason = artifact_input.quarantine_reason
        expected = BlobRecord(
            sha256=sha256,
            length=byte_length,
            media_type=artifact_input.media_type,
            encoding=artifact_input.encoding,
            logical_path=cas_relative_path(sha256),
            availability_state="verified_available",
            quarantine_state=(
                "quarantined" if quarantine_reason is not None else "clear"
            ),
            quarantine_reason=quarantine_reason,
        )
        previous = expected_by_digest.get(sha256)
        if previous is not None and (
            previous.length,
            previous.media_type,
            previous.encoding,
            previous.quarantine_state,
            previous.quarantine_reason,
        ) != (
            expected.length,
            expected.media_type,
            expected.encoding,
            expected.quarantine_state,
            expected.quarantine_reason,
        ):
            raise ValueError(
                "same raw bytes cannot carry conflicting physical Blob metadata"
            )
        expected_by_digest.setdefault(sha256, expected)
    return tuple(expected_by_digest[key] for key in sorted(expected_by_digest))


def prepare_raw_capture(
    store: WorkspaceStore,
    *,
    mission_id: str,
    executive_epoch_id: str | None,
    capture_kind: str,
    observation_id: str,
    assignment_id: str,
    provenance: Mapping[str, Any],
    artifacts: Sequence[RawCaptureArtifactInput | RawCaptureArtifactFileInput],
    completion: Mapping[str, Any] | None = None,
    cas: EvidenceCAS | None = None,
) -> RawCaptureRecord:
    """Prepare factual custody without requiring the producing epoch to be current."""

    if type(store) is not WorkspaceStore:
        raise TypeError("prepare_raw_capture requires the owning WorkspaceStore")
    artifact_inputs = tuple(artifacts)
    if any(
        type(item) not in {RawCaptureArtifactInput, RawCaptureArtifactFileInput}
        for item in artifact_inputs
    ):
        raise TypeError(
            "artifacts require exact RawCaptureArtifactInput or "
            "RawCaptureArtifactFileInput values"
        )
    file_inputs = tuple(
        item for item in artifact_inputs if type(item) is RawCaptureArtifactFileInput
    )
    if file_inputs:
        if type(cas) is not EvidenceCAS:
            raise TypeError(
                "file-backed raw capture preparation requires the owning EvidenceCAS"
            )
    descriptors = tuple(
        item.descriptor(ordinal) for ordinal, item in enumerate(artifact_inputs)
    )
    capture_id = canonical_raw_capture_id(
        project_id=store.project_id,
        capture_kind=capture_kind,
        observation_id=observation_id,
    )
    provisional = {
        "capture_id": capture_id,
        "project_id": store.project_id,
        "mission_id": mission_id,
        "executive_epoch_id": executive_epoch_id,
        "capture_kind": capture_kind,
        "observation_id": observation_id,
        "assignment_id": assignment_id,
        "provenance": deep_thaw(_require_json_mapping(provenance, "provenance")),
        "completion": (
            None
            if completion is None
            else deep_thaw(_require_json_mapping(completion, "completion"))
        ),
        "artifacts": [item.to_payload() for item in descriptors],
    }
    digest = _sha256(provisional)
    prepared = RawCaptureRecord(
        capture_id=capture_id,
        project_id=store.project_id,
        mission_id=mission_id,
        executive_epoch_id=executive_epoch_id,
        capture_kind=capture_kind,
        observation_id=observation_id,
        assignment_id=assignment_id,
        provenance=provenance,
        completion=completion,
        artifacts=descriptors,
        digest_sha256=digest,
        _artifact_inputs=artifact_inputs,
    )
    _raw_capture_expected_blobs(prepared)

    # A durable capture is the authority for an exact replay.  Reconcile its
    # semantic identity before touching a now-disposable producer file; the
    # commit path below separately checks every persisted physical Blob fact.
    try:
        existing = read_raw_capture(store, capture_id=capture_id)
    except StaleCommandError:
        existing = None
    if existing is not None:
        # The writer transaction is the authority for exact equality.  Even a
        # conflicting identity must not cause disposable source bytes to be
        # reopened merely to discover that the durable identity already wins.
        return prepared

    if file_inputs:
        assert type(cas) is EvidenceCAS
        source_paths: dict[str, tuple[str, int]] = {}
        for item in file_inputs:
            verified_path = cas.verify_exact_file_source(
                item.source_path,
                expected_sha256=item.sha256,
                expected_length=item.byte_length,
            )
            source_key = os.path.normcase(str(verified_path))
            source_binding = (item.sha256, item.byte_length)
            previous_binding = source_paths.get(source_key)
            if previous_binding is not None and previous_binding != source_binding:
                raise ValueError(
                    "file-backed raw capture source path has conflicting byte identity"
                )
            source_paths.setdefault(source_key, source_binding)
    return prepared


def commit_raw_capture(
    store: WorkspaceStore,
    *,
    cas: EvidenceCAS,
    record: RawCaptureRecord,
    lease: WriterLease,
    actor: str,
    command_id: str | None = None,
) -> CommandOutcome:
    """Install exact artifacts and commit one immutable factual capture."""

    if type(record) is not RawCaptureRecord:
        raise TypeError("commit_raw_capture requires an exact RawCaptureRecord")
    record.verify_prepared()
    if store.project_id != record.project_id:
        raise ValueError("raw capture targets another project")
    expected_blobs = _raw_capture_expected_blobs(record)
    normalized_actor = _require_text(actor, "actor")
    custody_binding = _sha256(
        {
            "capture_digest_sha256": record.digest_sha256,
            "blobs": [
                {
                    "sha256": item.sha256,
                    "length": item.length,
                    "media_type": item.media_type,
                    "encoding": item.encoding,
                    "logical_path": item.logical_path,
                    "integrity_state": item.integrity_state,
                    "availability_state": item.availability_state,
                    "quarantine_state": item.quarantine_state,
                    "quarantine_reason": item.quarantine_reason,
                }
                for item in expected_blobs
            ],
        }
    )
    normalized_command_id = command_id or (
        f"raw-capture:{record.capture_id}:{custody_binding}"
    )

    # Do not reopen or reinstall disposable producer files on an exact replay.
    # WorkspaceStore rechecks the existing semantic capture and every stable
    # Blob field transactionally before returning; CAS verification then proves
    # that the already-installed bytes remain intact.
    try:
        existing = read_raw_capture(store, capture_id=record.capture_id)
    except StaleCommandError:
        existing = None
    if existing is not None:
        outcome = store.commit_raw_capture(
            record=record.store_record(),
            artifacts=tuple(item.to_payload() for item in record.artifacts),
            blobs=expected_blobs,
            lease=lease,
            command_id=normalized_command_id,
            actor=normalized_actor,
        )
        for expected in expected_blobs:
            cas.verify_record(expected)
        return outcome

    installed_by_digest: dict[str, BlobRecord] = {}
    for artifact_input, descriptor in zip(
        record._artifact_inputs, record.artifacts, strict=True
    ):
        if type(artifact_input) is RawCaptureArtifactInput:
            installed = cas.ingest_bytes(
                artifact_input.content_bytes,
                original_name=artifact_input.logical_name,
                media_type=artifact_input.media_type,
                encoding=artifact_input.encoding,
            )
        else:
            assert type(artifact_input) is RawCaptureArtifactFileInput
            installed = cas.ingest_exact_file(
                artifact_input.source_path,
                expected_sha256=artifact_input.sha256,
                expected_length=artifact_input.byte_length,
                original_name=artifact_input.logical_name,
                media_type=artifact_input.media_type,
                encoding=artifact_input.encoding,
                quarantine_reason=artifact_input.quarantine_reason,
            )
        if installed.record.sha256 != descriptor.blob_sha256:
            raise WorkspaceIntegrityError("raw capture CAS installation changed digest")
        available = replace(
            installed.record,
            availability_state="verified_available",
        )
        previous = installed_by_digest.get(available.sha256)
        if previous is not None and (
            previous.media_type != available.media_type
            or previous.encoding != available.encoding
            or previous.quarantine_state != available.quarantine_state
            or previous.quarantine_reason != available.quarantine_reason
        ):
            raise ValueError(
                "same raw bytes cannot carry conflicting physical Blob metadata"
            )
        installed_by_digest.setdefault(available.sha256, available)
    return store.commit_raw_capture(
        record=record.store_record(),
        artifacts=tuple(item.to_payload() for item in record.artifacts),
        blobs=tuple(installed_by_digest[key] for key in sorted(installed_by_digest)),
        lease=lease,
        command_id=normalized_command_id,
        actor=normalized_actor,
    )


def read_raw_capture(store: WorkspaceStore, *, capture_id: str) -> RawCaptureRecord:
    """Read one capture and its ordered immutable CAS descriptors."""

    stored = store.read_raw_capture(_require_text(capture_id, "capture_id"))
    raw_record = stored["record"]
    raw_artifacts = tuple(stored["artifacts"])
    descriptors = tuple(
        RawCaptureArtifactDescriptor(
            ordinal=int(item["ordinal"]),
            role=str(item["role"]),
            logical_name=str(item["logical_name"]),
            blob_sha256=str(item["blob_sha256"]),
        )
        for item in raw_artifacts
    )
    payload = {
        "capture_id": raw_record["capture_id"],
        "project_id": raw_record["project_id"],
        "mission_id": raw_record["mission_id"],
        "executive_epoch_id": raw_record["executive_epoch_id"],
        "capture_kind": raw_record["capture_kind"],
        "observation_id": raw_record["observation_id"],
        "assignment_id": raw_record["assignment_id"],
        "provenance": deep_thaw(raw_record["provenance"]),
        "completion": (
            None
            if raw_record["completion"] is None
            else deep_thaw(raw_record["completion"])
        ),
        "artifacts": [item.to_payload() for item in descriptors],
    }
    return RawCaptureRecord(
        capture_id=str(raw_record["capture_id"]),
        project_id=str(raw_record["project_id"]),
        mission_id=str(raw_record["mission_id"]),
        executive_epoch_id=(
            None
            if raw_record["executive_epoch_id"] is None
            else str(raw_record["executive_epoch_id"])
        ),
        capture_kind=str(raw_record["capture_kind"]),
        observation_id=str(raw_record["observation_id"]),
        assignment_id=str(raw_record["assignment_id"]),
        provenance=raw_record["provenance"],
        completion=raw_record["completion"],
        artifacts=descriptors,
        digest_sha256=_sha256(payload),
        created_at=str(raw_record["created_at"]),
    )


def read_raw_capture_artifact(
    store: WorkspaceStore,
    *,
    cas: EvidenceCAS,
    mission_id: str,
    capture_id: str,
    artifact_ordinal: int,
) -> RawCaptureArtifactContent:
    """Read exact raw bytes without requiring or creating Evidence meaning."""

    if type(store) is not WorkspaceStore:
        raise TypeError("read_raw_capture_artifact requires the owning WorkspaceStore")
    if type(cas) is not EvidenceCAS:
        raise TypeError("read_raw_capture_artifact requires the owning EvidenceCAS")
    descriptor, content_bytes = store.read_raw_capture_artifact_bytes(
        cas=cas,
        mission_id=_require_text(mission_id, "mission_id"),
        capture_id=_require_text(capture_id, "capture_id"),
        artifact_ordinal=_require_nonnegative_int(artifact_ordinal, "artifact_ordinal"),
    )
    if set(descriptor) != {
        "capture_id",
        "mission_id",
        "artifact_ordinal",
        "role",
        "logical_name",
        "blob_sha256",
        "media_type",
        "encoding",
    }:
        raise WorkspaceIntegrityError(
            "raw artifact read returned a noncanonical custody descriptor"
        )
    artifact = RawCaptureArtifactDescriptor(
        ordinal=int(descriptor["artifact_ordinal"]),
        role=str(descriptor["role"]),
        logical_name=str(descriptor["logical_name"]),
        blob_sha256=str(descriptor["blob_sha256"]),
    )
    return RawCaptureArtifactContent(
        capture_id=str(descriptor["capture_id"]),
        mission_id=str(descriptor["mission_id"]),
        artifact=artifact,
        media_type=str(descriptor["media_type"]),
        encoding=(
            str(descriptor["encoding"]) if descriptor["encoding"] is not None else None
        ),
        content_bytes=content_bytes,
    )


@dataclass(frozen=True, slots=True)
class CaptureScope:
    """One ordered exact locator into a captured artifact."""

    capture_id: str
    artifact_ordinal: int
    exact_scope: str | Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_text(self.capture_id, "capture_scope.capture_id")
        _require_nonnegative_int(
            self.artifact_ordinal, "capture_scope.artifact_ordinal"
        )
        if isinstance(self.exact_scope, str):
            object.__setattr__(
                self,
                "exact_scope",
                deep_freeze(
                    {
                        "description": _require_text(
                            self.exact_scope, "capture_scope.exact_scope"
                        )
                    }
                ),
            )
        elif isinstance(self.exact_scope, Mapping):
            object.__setattr__(
                self,
                "exact_scope",
                _require_json_mapping(self.exact_scope, "capture_scope.exact_scope"),
            )
        else:
            raise TypeError("capture_scope.exact_scope must be text or a mapping")

    def to_payload(self, source_ordinal: int) -> dict[str, Any]:
        return {
            "source_ordinal": _require_nonnegative_int(
                source_ordinal, "source_ordinal"
            ),
            "capture_id": self.capture_id,
            "artifact_ordinal": self.artifact_ordinal,
            "exact_scope": deep_thaw(self.exact_scope),
        }


@dataclass(frozen=True, slots=True)
class InterpretedOwnerReference:
    """One exact Evidence or Candidate revision used as a synthesis premise."""

    kind: str
    identity: str
    revision: int
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.kind not in {"evidence", "candidate"}:
            raise ValueError(
                "interpreted synthesis inputs must be Evidence or Candidate revisions"
            )
        _require_text(self.identity, "interpreted_owner_reference.identity")
        _require_positive_int(self.revision, "interpreted_owner_reference.revision")
        _require_digest(
            self.payload_sha256, "interpreted_owner_reference.payload_sha256"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "revision": self.revision,
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class EvidenceMeaningRecord:
    """One independently correctable Evidence revision and exact sources."""

    evidence: EvidenceItemRevision
    sources: tuple[CaptureScope, ...]
    interpreted_inputs: tuple[InterpretedOwnerReference, ...]
    payload_digest: str
    expected_head_revision: int | None
    expected_head_payload_digest: str | None
    expected_dependency_heads: Mapping[str, tuple[int, str]]
    _authority: DirectExecutiveEpochAuthority | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.evidence) is not EvidenceItemRevision:
            raise TypeError("Evidence meaning requires EvidenceItemRevision")
        if self.evidence.subtype != EVIDENCE_MEANING_SUBTYPE:
            raise ValueError("Evidence meaning has the wrong subtype")
        if self.evidence.blob_roles:
            raise ValueError(
                "Evidence meaning must reference capture scopes, not copy blobs"
            )
        if self.evidence.availability_state != "verified_available":
            raise ValueError("Evidence meaning revision must be readable")
        if bool(self.sources) == bool(self.interpreted_inputs):
            raise ValueError(
                "Evidence meaning requires exactly one source route: capture scopes "
                "or interpreted synthesis inputs"
            )
        source_payloads = tuple(
            source.to_payload(ordinal) for ordinal, source in enumerate(self.sources)
        )
        source_keys = tuple(canonical_json_bytes(item) for item in source_payloads)
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("Evidence capture sources must be exact and unique")
        if any(
            type(item) is not InterpretedOwnerReference
            for item in self.interpreted_inputs
        ):
            raise TypeError(
                "derived Evidence inputs require exact InterpretedOwnerReference values"
            )
        input_payloads = tuple(item.to_payload() for item in self.interpreted_inputs)
        input_keys = tuple(canonical_json_bytes(item) for item in input_payloads)
        if self.interpreted_inputs and (
            len(self.interpreted_inputs) < 2 or len(input_keys) != len(set(input_keys))
        ):
            raise ValueError(
                "derived Evidence requires at least two distinct interpreted owner inputs"
            )
        if (self.expected_head_revision is None) != (
            self.expected_head_payload_digest is None
        ):
            raise ValueError(
                "expected Evidence head revision and digest travel together"
            )
        if self.expected_head_revision is not None:
            _require_positive_int(self.expected_head_revision, "expected_head_revision")
            _require_digest(
                self.expected_head_payload_digest,
                "expected_head_payload_digest",
            )
        if self._authority is not None:
            target_revision = (
                1
                if self.expected_head_revision is None
                else self.expected_head_revision + 1
            )
            if self.evidence.revision != target_revision:
                raise ValueError("Evidence meaning revision is not contiguous")
        normalized_dependencies = _normalize_dependency_heads(
            self.expected_dependency_heads
        )
        object.__setattr__(self, "expected_dependency_heads", normalized_dependencies)
        if self.payload_digest != _sha256(self.evidence.to_payload()):
            raise ValueError("Evidence meaning payload digest is stale")
        if self._authority is not None:
            if type(self._authority) is not DirectExecutiveEpochAuthority:
                raise TypeError(
                    "Evidence meaning requires direct Executive Epoch authority"
                )
            self._authority.verify_integrity()

    @property
    def evidence_id(self) -> str:
        return self.evidence.evidence_id

    @property
    def revision(self) -> int:
        return self.evidence.revision

    def verify_prepared(self) -> None:
        if type(self) is not EvidenceMeaningRecord:
            raise ValueError("EvidenceMeaningRecord subclasses have no authority")
        if self._authority is None:
            raise ValueError("Evidence meaning commit requires prepared interpretation")
        self.__post_init__()


def _normalize_dependency_heads(
    heads: Mapping[str, tuple[int, str]] | None,
) -> Mapping[str, tuple[int, str]]:
    if heads is None:
        return deep_freeze({})
    if not isinstance(heads, Mapping):
        raise TypeError("expected_dependency_heads must be a mapping")
    normalized: dict[str, tuple[int, str]] = {}
    for key, value in heads.items():
        name = _require_text(key, "dependency owner key")
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes))
            or len(value) != 2
        ):
            raise ValueError("dependency head must be (revision, payload_digest)")
        revision = _require_positive_int(value[0], f"{name}.revision")
        digest = _require_digest(value[1], f"{name}.payload_digest")
        normalized[name] = (revision, digest)
    return deep_freeze(dict(sorted(normalized.items())))


def _optional_strings(values: Sequence[str], label: str) -> tuple[str, ...]:
    return _require_strings(values, label, nonempty=False)


def prepare_evidence_meaning(
    *,
    authority: DirectExecutiveEpochAuthority,
    evidence_id: str,
    statement: str,
    exact_scope: str,
    strength: str,
    semantic_role: str,
    authority_basis: str,
    sources: Sequence[CaptureScope],
    non_inferences: Sequence[str],
    interpreted_inputs: Sequence[InterpretedOwnerReference] = (),
    objects: Sequence[str] = (),
    hypotheses: Sequence[str] = (),
    domain: str | None = None,
    normalization: str | None = None,
    quantifiers: str | None = None,
    dependencies: Sequence[Mapping[str, Any]] = (),
    limitations: Sequence[str] = (),
    parent_bet_outcome: str | None = None,
    decision_consequence: str | None = None,
    standing: str | None = None,
    expected_head_revision: int | None = None,
    expected_head_payload_digest: str | None = None,
    expected_dependency_heads: Mapping[str, tuple[int, str]] | None = None,
    security_classification: str = "workspace_internal",
    retention: str = "mission_evidence_meaning",
) -> EvidenceMeaningRecord:
    """Prepare one sparse Evidence meaning under current executive authority."""

    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("Evidence meaning requires direct Executive Epoch authority")
    authority.verify_integrity()
    if (expected_head_revision is None) != (expected_head_payload_digest is None):
        raise ValueError("expected Evidence head revision and digest travel together")
    revision = 1 if expected_head_revision is None else expected_head_revision + 1
    subject: dict[str, Any] = {
        "mission_id": authority.mission_id,
        "statement": _require_text(statement, "statement"),
        "semantic_role": _require_text(semantic_role, "semantic_role"),
        "authority_basis": _require_text(authority_basis, "authority_basis"),
    }
    optional_sequences = (
        ("objects", objects),
        ("hypotheses", hypotheses),
    )
    for key, values in optional_sequences:
        normalized = _optional_strings(values, key)
        if normalized:
            subject[key] = list(normalized)
    for key, value in (
        ("domain", domain),
        ("normalization", normalization),
        ("quantifiers", quantifiers),
        ("parent_bet_outcome", parent_bet_outcome),
        ("decision_consequence", decision_consequence),
        ("standing", standing),
    ):
        if value is not None:
            subject[key] = _require_text(value, key)
    dependency_payloads = tuple(
        deep_thaw(_require_json_mapping(item, f"dependencies[{index}]"))
        for index, item in enumerate(dependencies)
    )
    if dependency_payloads:
        subject["dependencies"] = list(dependency_payloads)
    source_records = tuple(sources)
    if any(type(item) is not CaptureScope for item in source_records):
        raise TypeError("sources require exact CaptureScope values")
    supplied_interpreted_inputs = tuple(interpreted_inputs)
    if any(
        type(item) is not InterpretedOwnerReference
        for item in supplied_interpreted_inputs
    ):
        raise TypeError(
            "interpreted_inputs require exact InterpretedOwnerReference values"
        )
    interpreted_records = tuple(
        sorted(
            supplied_interpreted_inputs,
            key=lambda item: (
                item.kind,
                item.identity,
                item.revision,
                item.payload_sha256,
            ),
        )
    )
    if interpreted_records:
        subject["interpreted_owner_inputs"] = [
            item.to_payload() for item in interpreted_records
        ]
    evidence = EvidenceItemRevision(
        evidence_id=_require_text(evidence_id, "evidence_id"),
        revision=revision,
        subtype=EVIDENCE_MEANING_SUBTYPE,
        subject=subject,
        exact_scope=_require_text(exact_scope, "exact_scope"),
        rigor=_require_text(strength, "strength"),
        limitations=_optional_strings(limitations, "limitations"),
        non_inferences=_require_strings(non_inferences, "non_inferences"),
        security_classification=_require_text(
            security_classification, "security_classification"
        ),
        retention=_require_text(retention, "retention"),
        blob_roles=(),
        availability_state="verified_available",
    )
    payload_digest = _sha256(evidence.to_payload())
    normalized_heads = _normalize_dependency_heads(expected_dependency_heads)
    return EvidenceMeaningRecord(
        evidence=evidence,
        sources=source_records,
        interpreted_inputs=interpreted_records,
        payload_digest=payload_digest,
        expected_head_revision=expected_head_revision,
        expected_head_payload_digest=expected_head_payload_digest,
        expected_dependency_heads=normalized_heads,
        _authority=authority,
    )


def commit_evidence_meaning(
    store: WorkspaceStore,
    *,
    record: EvidenceMeaningRecord,
    lease: WriterLease,
    actor: str,
    command_id: str | None = None,
) -> CommandOutcome:
    """Commit one Evidence head without copying its capture bytes."""

    if type(record) is not EvidenceMeaningRecord:
        raise TypeError("commit_evidence_meaning requires EvidenceMeaningRecord")
    record.verify_prepared()
    assert record._authority is not None
    return store.commit_evidence_meaning_revision(
        executive_epoch_id=record._authority.executive_epoch_id,
        record=record.evidence,
        sources=tuple(
            source.to_payload(ordinal) for ordinal, source in enumerate(record.sources)
        ),
        interpreted_inputs=tuple(
            item.to_payload() for item in record.interpreted_inputs
        ),
        expected_head_revision=record.expected_head_revision,
        expected_head_payload_digest=record.expected_head_payload_digest,
        expected_dependency_heads={
            key: (int(value[0]), str(value[1]))
            for key, value in record.expected_dependency_heads.items()
        },
        lease=lease,
        command_id=(
            command_id
            or f"evidence-meaning:{record.evidence_id}:r{record.revision}:{record.payload_digest}"
        ),
        actor=_require_text(actor, "actor"),
        expected_canonical_authority_digest=(
            record._authority.canonical_authority_digest
        ),
    )


def _evidence_from_payload(payload: Mapping[str, Any]) -> EvidenceItemRevision:
    return EvidenceItemRevision(
        evidence_id=str(payload["evidence_id"]),
        revision=int(payload["revision"]),
        subtype=str(payload["subtype"]),
        subject=payload["subject"],
        exact_scope=str(payload["exact_scope"]),
        rigor=str(payload["rigor"]),
        limitations=tuple(payload["limitations"]),
        non_inferences=tuple(payload["non_inferences"]),
        security_classification=str(payload["security_classification"]),
        retention=str(payload["retention"]),
        blob_roles=tuple(tuple(item) for item in payload["blob_roles"]),
        availability_state=str(payload["availability_state"]),
        canonical_effect=str(payload["canonical_effect"]),
    )


def read_evidence_meaning(
    store: WorkspaceStore,
    *,
    evidence_id: str,
    revision: int | None = None,
) -> EvidenceMeaningRecord:
    """Read one independent Evidence meaning and its ordered capture scopes."""

    stored = store.read_evidence_meaning_revision(
        _require_text(evidence_id, "evidence_id"), revision=revision
    )
    evidence = _evidence_from_payload(stored["record"])
    if evidence.subtype != EVIDENCE_MEANING_SUBTYPE:
        raise WorkspaceIntegrityError("requested Evidence is not an Evidence meaning")
    sources = tuple(
        CaptureScope(
            capture_id=str(item["capture_id"]),
            artifact_ordinal=int(item["artifact_ordinal"]),
            exact_scope=item["exact_scope"],
        )
        for item in stored["sources"]
    )
    if any(
        int(item["source_ordinal"]) != ordinal
        for ordinal, item in enumerate(stored["sources"])
    ):
        raise WorkspaceIntegrityError("Evidence capture sources are not contiguous")
    interpreted_inputs = tuple(
        InterpretedOwnerReference(
            kind=str(item["kind"]),
            identity=str(item["identity"]),
            revision=int(item["revision"]),
            payload_sha256=str(item["payload_sha256"]),
        )
        for item in stored.get("interpreted_inputs", ())
    )
    return EvidenceMeaningRecord(
        evidence=evidence,
        sources=sources,
        interpreted_inputs=interpreted_inputs,
        payload_digest=str(stored["payload_digest"]),
        expected_head_revision=None,
        expected_head_payload_digest=None,
        expected_dependency_heads={},
    )


@dataclass(frozen=True, slots=True)
class CaptureScopeAnnotationRecord:
    """Optional revisable neutral-material annotation; never Evidence meaning."""

    annotation_id: str
    revision: int
    capture_id: str
    exact_scope: Mapping[str, Any]
    lifecycle: str
    payload_digest: str
    expected_head_revision: int | None
    expected_head_payload_digest: str | None
    dependency_heads: Mapping[str, tuple[int, str]]
    created_actor: str | None = None
    created_epoch_id: str | None = None
    created_at: str | None = None
    _authority: DirectExecutiveEpochAuthority | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _require_text(self.annotation_id, "annotation_id")
        _require_positive_int(self.revision, "annotation revision")
        _require_text(self.capture_id, "annotation capture_id")
        scope = _require_json_mapping(self.exact_scope, "annotation exact_scope")
        if not scope:
            raise ValueError("annotation exact_scope must not be empty")
        object.__setattr__(self, "exact_scope", scope)
        if self.lifecycle not in {"active", "removed"}:
            raise ValueError("annotation lifecycle must be active or removed")
        if (self.expected_head_revision is None) != (
            self.expected_head_payload_digest is None
        ):
            raise ValueError("annotation expected revision and digest travel together")
        if self.expected_head_revision is not None:
            _require_positive_int(
                self.expected_head_revision, "annotation expected revision"
            )
            _require_digest(
                self.expected_head_payload_digest,
                "annotation expected payload digest",
            )
        object.__setattr__(
            self,
            "dependency_heads",
            _normalize_dependency_heads(self.dependency_heads),
        )
        if self.payload_digest != _sha256(self.semantic_payload()):
            raise ValueError("capture annotation payload digest is stale")
        if self._authority is not None:
            expected_revision = (
                1
                if self.expected_head_revision is None
                else self.expected_head_revision + 1
            )
            if self.revision != expected_revision:
                raise ValueError("capture annotation revision authority is stale")
            if type(self._authority) is not DirectExecutiveEpochAuthority:
                raise TypeError(
                    "capture annotation requires direct Executive Epoch authority"
                )
            self._authority.verify_integrity()

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "capture_id": self.capture_id,
            "exact_scope": deep_thaw(self.exact_scope),
            "lifecycle": self.lifecycle,
        }

    @property
    def annotation_kind(self) -> str:
        return "reviewed-no-current-semantic-delta"

    def verify_prepared(self) -> None:
        if type(self) is not CaptureScopeAnnotationRecord:
            raise ValueError("capture annotation subclasses have no authority")
        if self._authority is None:
            raise ValueError("annotation commit requires prepared executive judgment")
        self.__post_init__()


def prepare_capture_scope_annotation(
    *,
    authority: DirectExecutiveEpochAuthority,
    annotation_id: str,
    capture_id: str,
    exact_scope: Mapping[str, Any],
    lifecycle: str,
    expected_head_revision: int | None = None,
    expected_head_payload_digest: str | None = None,
    dependency_heads: Mapping[str, tuple[int, str]] | None = None,
) -> CaptureScopeAnnotationRecord:
    """Prepare the optional exact-scope neutral-material annotation."""

    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("capture annotation requires direct Executive Epoch authority")
    authority.verify_integrity()
    scope = _require_json_mapping(exact_scope, "annotation exact_scope")
    semantic_payload = {
        "annotation_id": _require_text(annotation_id, "annotation_id"),
        "capture_id": _require_text(capture_id, "capture_id"),
        "exact_scope": deep_thaw(scope),
        "lifecycle": lifecycle,
    }
    return CaptureScopeAnnotationRecord(
        annotation_id=annotation_id,
        revision=(1 if expected_head_revision is None else expected_head_revision + 1),
        capture_id=capture_id,
        exact_scope=scope,
        lifecycle=lifecycle,
        payload_digest=_sha256(semantic_payload),
        expected_head_revision=expected_head_revision,
        expected_head_payload_digest=expected_head_payload_digest,
        dependency_heads=_normalize_dependency_heads(dependency_heads),
        _authority=authority,
    )


def commit_capture_scope_annotation(
    store: WorkspaceStore,
    *,
    record: CaptureScopeAnnotationRecord,
    lease: WriterLease,
    actor: str,
    command_id: str | None = None,
) -> CommandOutcome:
    """Commit or remove the annotation without creating Evidence or workflow state."""

    if type(record) is not CaptureScopeAnnotationRecord:
        raise TypeError("capture annotation commit requires its exact record")
    record.verify_prepared()
    assert record._authority is not None
    return store.commit_capture_scope_annotation_revision(
        executive_epoch_id=record._authority.executive_epoch_id,
        annotation_id=record.annotation_id,
        capture_id=record.capture_id,
        exact_scope=deep_thaw(record.exact_scope),
        lifecycle=record.lifecycle,
        created_epoch_id=record._authority.executive_epoch_id,
        expected_head_revision=record.expected_head_revision,
        expected_head_payload_digest=record.expected_head_payload_digest,
        dependency_heads=record.dependency_heads,
        lease=lease,
        command_id=(
            command_id
            or f"capture-annotation:{record.annotation_id}:r{record.revision}:{record.payload_digest}"
        ),
        actor=_require_text(actor, "actor"),
        expected_canonical_authority_digest=(
            record._authority.canonical_authority_digest
        ),
    )


def read_capture_scope_annotation(
    store: WorkspaceStore,
    *,
    annotation_id: str,
    revision: int | None = None,
) -> CaptureScopeAnnotationRecord:
    """Read the current or one historical annotation revision."""

    stored = store.read_capture_scope_annotation(
        _require_text(annotation_id, "annotation_id"), revision=revision
    )
    return CaptureScopeAnnotationRecord(
        annotation_id=str(stored["annotation_id"]),
        revision=int(stored["revision"]),
        capture_id=str(stored["capture_id"]),
        exact_scope=stored["exact_scope"],
        lifecycle=str(stored["lifecycle"]),
        payload_digest=str(stored["payload_digest"]),
        expected_head_revision=None,
        expected_head_payload_digest=None,
        dependency_heads={},
        created_actor=str(stored["created_actor"]),
        created_epoch_id=str(stored["created_epoch_id"]),
        created_at=str(stored["created_at"]),
    )


__all__ = [
    "EVIDENCE_MEANING_SUBTYPE",
    "CaptureScope",
    "CaptureScopeAnnotationRecord",
    "EvidenceMeaningRecord",
    "InterpretedOwnerReference",
    "RawCaptureArtifactContent",
    "RawCaptureArtifactDescriptor",
    "RawCaptureArtifactFileInput",
    "RawCaptureArtifactInput",
    "RawCaptureRecord",
    "commit_capture_scope_annotation",
    "commit_evidence_meaning",
    "commit_raw_capture",
    "prepare_capture_scope_annotation",
    "prepare_evidence_meaning",
    "prepare_raw_capture",
    "read_capture_scope_annotation",
    "read_evidence_meaning",
    "read_raw_capture",
    "read_raw_capture_artifact",
]
