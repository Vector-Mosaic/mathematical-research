"""Proof-neutral Evidence byte custody for the inactive research workspace.

This module deliberately stops at the filesystem/metadata boundary.  It does
not write workspace SQL, canonical mathematics, Campaign state, or provider
state.  Callers install bytes first, then persist the returned immutable
payloads in their own fenced transaction.  Every path accepted here is rooted
in a caller-supplied disposable/workspace root.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import math
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Iterable, Mapping, Sequence

from .json_support import canonical_json_bytes
from .observability import (
    NOOP_WORKSPACE_OBSERVER,
    EventKind,
    ObservationEnvironment,
    ObservationLevel,
    WorkspaceObserver,
    emit_event_safely,
)
from .research_model import deep_freeze, deep_thaw
from .research_semantics import ResolutionRequirementKind
from .workspace_paths import WorkspacePaths

if TYPE_CHECKING:
    from .workspace_store import WorkspaceStore


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARCHIVE_SUFFIXES = {
    ".zip",
    ".tar",
    ".tgz",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".cab",
}
ARCHIVE_MEDIA_TYPES = {
    "application/zip",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/gzip",
}
ARCHIVE_MAGIC = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"\x1f\x8b",
)
SECRET_MARKERS = (
    b"-----begin private key-----",
    b"-----begin rsa private key-----",
    b"-----begin openssh private key-----",
    b"authorization: bearer ",
    b"api_key=",
    b"api-token=",
    b"secret_access_key=",
)
SECRET_PATTERNS = (
    re.compile(rb"-----begin(?: [a-z0-9_-]+)* private key-----", re.IGNORECASE),
    re.compile(rb"-----begin pgp private key block-----", re.IGNORECASE),
    re.compile(rb"(?:akia|asia)[0-9a-z]{16}", re.IGNORECASE),
    re.compile(rb"(?:aws_)?secret_access_key\s*[:=]", re.IGNORECASE),
    re.compile(rb"(?:azure_)?client_secret\s*[:=]", re.IGNORECASE),
    re.compile(
        rb"(?:accountkey|cloudflare_api_token|hcloud_token)\s*[:=]", re.IGNORECASE
    ),
    re.compile(rb"railway_api_token\s*[:=]", re.IGNORECASE),
    re.compile(rb'"(?:private_key|private_key_id|client_secret)"\s*:', re.IGNORECASE),
    re.compile(rb"gh[pousr]_[0-9a-z]{20,}", re.IGNORECASE),
    re.compile(rb"sk-[0-9a-z_-]{20,}", re.IGNORECASE),
)

# Quarantine metadata crosses the durable custody boundary.  Keep it to one
# non-sensitive classification code instead of accepting payload excerpts,
# provider messages, or free-form operator prose.
QUARANTINE_REASON_OPAQUE_RESTRICTED = "opaque_restricted"
QUARANTINE_REASONS = frozenset({QUARANTINE_REASON_OPAQUE_RESTRICTED})

_STAGED_BLOB_ISSUANCE_KEY = os.urandom(32)
_CLOSURE_MANIFEST_ISSUANCE_KEY = os.urandom(32)
_SECURITY_DISPOSITION_ISSUANCE_KEY = os.urandom(32)
_EVIDENCE_READ_HANDLE_ISSUANCE_KEY = os.urandom(32)


def _secret_views(data: bytes) -> tuple[bytes, ...]:
    """Return fail-closed byte views for ASCII and common UTF-16 encodings."""

    lowered = data.lower()
    without_nuls = lowered.replace(b"\x00", b"")
    return (lowered,) if without_nuls == lowered else (lowered, without_nuls)


def _contains_secret_like(data: bytes) -> bool:
    return any(
        any(marker in view for marker in SECRET_MARKERS)
        or any(pattern.search(view) for pattern in SECRET_PATTERNS)
        for view in _secret_views(data)
    )


CLOSURE_MEMBER_KINDS = frozenset(
    {
        "candidate",
        "mission",
        "session",
        "context",
        "evidence",
        "blob",
        "provenance",
        "independence_disclosure",
        "dependency",
        "composition",
        "source_audit",
        "circularity_audit",
        "review",
        "objection",
        "code_environment",
    }
)
CLOSURE_REFERENCE_KINDS = frozenset(
    {
        "evidence",
        "blob",
        "provenance",
        "independence_disclosure",
        "dependency",
        "composition",
        "source_audit",
        "circularity_audit",
        "review",
        "objection",
        "code_environment",
        "session",
        "context",
        "mission",
        "candidate",
    }
)


class EvidenceStoreError(RuntimeError):
    """Fail-closed Evidence custody error with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class IntegrityFreeze(EvidenceStoreError):
    """An A2-class byte-integrity conflict requiring an external hold."""


_CAPACITY_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "ENOSPC", None),
        getattr(errno, "EDQUOT", None),
    )
    if value is not None
)


def _is_capacity_error(exc: OSError) -> bool:
    return exc.errno in _CAPACITY_ERRNOS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _validate_digest(digest: str) -> str:
    if not SHA256_RE.fullmatch(digest):
        raise EvidenceStoreError(
            "invalid_digest", "SHA-256 must be 64 lowercase hex characters"
        )
    return digest


def validate_quarantine_reason(reason: str | None) -> str | None:
    """Validate the closed, non-sensitive quarantine classification."""

    if reason is not None and (
        type(reason) is not str or reason not in QUARANTINE_REASONS
    ):
        raise ValueError("invalid quarantine_reason classification")
    return reason


def _validate_logical_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise EvidenceStoreError(
            "unsafe_logical_path", "logical name must be nonempty portable text"
        )
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise EvidenceStoreError(
            "unsafe_logical_path", "logical name must be a contained relative path"
        )
    if candidate.parts and candidate.parts[0].endswith(":"):
        raise EvidenceStoreError(
            "unsafe_logical_path", "drive-qualified logical paths are forbidden"
        )
    return candidate.as_posix()


def _contained(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise EvidenceStoreError(
            "path_escape", f"path escapes Evidence root: {candidate}"
        ) from exc
    return resolved


def _ensure_no_symlink(path: Path, stop: Path) -> None:
    stop = stop.resolve()
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise EvidenceStoreError(
                "symlink_forbidden",
                f"symlink is forbidden in Evidence custody: {current}",
            )
        if current.resolve(strict=False) == stop:
            return
        if current.parent == current:
            raise EvidenceStoreError(
                "path_escape", f"unable to prove containment for {path}"
            )
        current = current.parent


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_read_only(path: Path) -> None:
    mode = stat.S_IREAD if os.name == "nt" else stat.S_IRUSR
    os.chmod(path, mode)


def cas_relative_path(digest: str) -> str:
    digest = _validate_digest(digest)
    return f"cas/sha256/{digest[:2]}/{digest[2:]}"


@dataclass(frozen=True)
class BlobRecord:
    sha256: str
    length: int
    media_type: str
    encoding: str | None
    logical_path: str
    integrity_state: str = "verified"
    availability_state: str = "installed_pending_metadata"
    quarantine_state: str = "clear"
    quarantine_reason: str | None = None
    first_verified_at: str = field(default_factory=_utc_now)
    last_verified_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        _validate_digest(self.sha256)
        if isinstance(self.length, bool) or self.length < 0:
            raise ValueError("blob length must be a nonnegative integer")
        if not self.media_type or not isinstance(self.media_type, str):
            raise ValueError("blob media_type is required")
        expected = cas_relative_path(self.sha256)
        if self.logical_path != expected:
            raise ValueError(f"logical_path must equal {expected}")
        if self.integrity_state not in {"verified", "corrupt", "unknown"}:
            raise ValueError("invalid integrity_state")
        if self.availability_state not in {
            "installed_pending_metadata",
            "verified_available",
            "missing",
            "unavailable",
        }:
            raise ValueError("invalid availability_state")
        if self.quarantine_state not in {"clear", "quarantined"}:
            raise ValueError("invalid quarantine_state")
        validate_quarantine_reason(self.quarantine_reason)
        if (self.quarantine_state == "quarantined") != (
            self.quarantine_reason is not None
        ):
            raise ValueError(
                "quarantine_state must be quarantined iff quarantine_reason is set"
            )

    @property
    def ordinary_available(self) -> bool:
        return (
            self.integrity_state == "verified"
            and self.availability_state == "verified_available"
            and self.quarantine_state == "clear"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "length": self.length,
            "media_type": self.media_type,
            "encoding": self.encoding,
            "logical_path": self.logical_path,
            "integrity_state": self.integrity_state,
            "availability_state": self.availability_state,
            "quarantine_state": self.quarantine_state,
            "quarantine_reason": self.quarantine_reason,
            "first_verified_at": self.first_verified_at,
            "last_verified_at": self.last_verified_at,
            "mathematical_grade": None,
        }


@dataclass(frozen=True)
class StagedBlob:
    ingestion_id: str
    stage_directory: Path
    payload_path: Path
    sha256: str
    length: int
    media_type: str
    encoding: str | None
    original_name: str
    quarantine_reason: str | None = None
    _issuance_tag: bytes | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def verify_issued(self) -> None:
        """Require an exact artifact issued by this process's staging path."""

        if type(self) is not StagedBlob:
            raise EvidenceStoreError(
                "staged_blob_authority_invalid",
                "StagedBlob subclasses cannot carry staging authority",
            )
        expected = _staged_blob_issuance_tag(self)
        if self._issuance_tag is None or not hmac.compare_digest(
            self._issuance_tag, expected
        ):
            raise EvidenceStoreError(
                "staged_blob_authority_invalid",
                "StagedBlob was not issued by the Evidence staging boundary",
            )


def _staged_blob_issuance_material(staged: StagedBlob) -> dict[str, Any]:
    return {
        "ingestion_id": staged.ingestion_id,
        "stage_directory": str(staged.stage_directory),
        "payload_path": str(staged.payload_path),
        "sha256": staged.sha256,
        "length": staged.length,
        "media_type": staged.media_type,
        "encoding": staged.encoding,
        "original_name": staged.original_name,
        "quarantine_reason": staged.quarantine_reason,
    }


def _staged_blob_issuance_tag(staged: StagedBlob) -> bytes:
    return hmac.new(
        _STAGED_BLOB_ISSUANCE_KEY,
        canonical_json_bytes(_staged_blob_issuance_material(staged)),
        hashlib.sha256,
    ).digest()


def _issue_staged_blob(**values: Any) -> StagedBlob:
    staged = StagedBlob(**values)
    object.__setattr__(staged, "_issuance_tag", _staged_blob_issuance_tag(staged))
    staged.verify_issued()
    return staged


@dataclass(frozen=True)
class InstalledBlob:
    record: BlobRecord
    path: Path
    deduplicated: bool

    @property
    def ordinary_available(self) -> bool:
        """Physical installation alone never authorizes an ordinary read."""

        return False


@dataclass(frozen=True)
class VerifiedEvidenceReadHandle:
    """Ephemeral store-issued capability for one exact Evidence blob role.

    The owning :class:`WorkspaceStore` rederives every state field from its
    current committed snapshot before each read.  The process-local issuance
    seal prevents direct construction or mutation from creating authority, and
    ``authorization_sha256`` binds the complete caller authorization mapping
    supplied at issuance.  External authentication of that mapping belongs to
    a later integration boundary; this package only binds and compares it.
    """

    project_id: str
    root_identity: str
    project_commit: int
    project_root_digest: str
    closure_id: str
    closure_manifest_sha256: str
    evidence_id: str
    evidence_revision: int
    role: str
    ordinal: int
    blob_sha256: str
    authorization_sha256: str
    canonical_effect: str = "none"
    _issuance_tag: bytes | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.project_id,
                self.root_identity,
                self.closure_id,
                self.evidence_id,
                self.role,
            )
        ):
            raise ValueError("Evidence read handle requires exact nonempty identities")
        if self.project_commit < 0 or self.evidence_revision < 1 or self.ordinal < 0:
            raise ValueError("Evidence read handle revisions and ordinals are invalid")
        for value in (
            self.project_root_digest,
            self.closure_manifest_sha256,
            self.blob_sha256,
            self.authorization_sha256,
        ):
            _validate_digest(value)
        if self.canonical_effect != "none":
            raise ValueError("Evidence read handles cannot claim canonical effect")

    def verify_issued(
        self,
        *,
        authorization: Mapping[str, Any] | None,
    ) -> None:
        """Require this exact process-issued handle and caller authorization."""

        if type(self) is not VerifiedEvidenceReadHandle:
            raise EvidenceStoreError(
                "evidence_read_handle_authority_invalid",
                "Evidence read-handle subclasses cannot carry authority",
            )
        supplied_digest = _evidence_read_authorization_digest(authorization)
        if not hmac.compare_digest(self.authorization_sha256, supplied_digest):
            raise EvidenceStoreError(
                "evidence_read_authorization_mismatch",
                "Evidence read authorization differs from handle issuance",
            )
        expected = _evidence_read_handle_issuance_tag(self)
        if self._issuance_tag is None or not hmac.compare_digest(
            self._issuance_tag,
            expected,
        ):
            raise EvidenceStoreError(
                "evidence_read_handle_authority_invalid",
                "Evidence read handle was not issued by the owning boundary",
            )


def _evidence_read_authorization_digest(
    authorization: Mapping[str, Any] | None,
) -> str:
    """Digest the complete nonempty caller mapping without selecting fields."""

    if not isinstance(authorization, Mapping) or not authorization:
        raise EvidenceStoreError(
            "evidence_read_authorization_required",
            "Evidence read authorization must be a complete nonempty mapping",
        )
    if any(not isinstance(key, str) or not key for key in authorization):
        raise EvidenceStoreError(
            "evidence_read_authorization_invalid",
            "Evidence read authorization keys must be nonempty strings",
        )
    try:
        return _canonical_digest(dict(authorization))
    except (TypeError, ValueError) as exc:
        raise EvidenceStoreError(
            "evidence_read_authorization_invalid",
            "Evidence read authorization must have one finite JSON meaning",
        ) from exc


def _evidence_read_handle_issuance_material(
    handle: VerifiedEvidenceReadHandle,
) -> dict[str, Any]:
    return {
        "kind": "verified_evidence_read_handle",
        "project_id": handle.project_id,
        "root_identity": handle.root_identity,
        "project_commit": handle.project_commit,
        "project_root_digest": handle.project_root_digest,
        "closure_id": handle.closure_id,
        "closure_manifest_sha256": handle.closure_manifest_sha256,
        "evidence_id": handle.evidence_id,
        "evidence_revision": handle.evidence_revision,
        "role": handle.role,
        "ordinal": handle.ordinal,
        "blob_sha256": handle.blob_sha256,
        "authorization_sha256": handle.authorization_sha256,
        "canonical_effect": handle.canonical_effect,
    }


def _evidence_read_handle_issuance_tag(
    handle: VerifiedEvidenceReadHandle,
) -> bytes:
    return hmac.new(
        _EVIDENCE_READ_HANDLE_ISSUANCE_KEY,
        canonical_json_bytes(_evidence_read_handle_issuance_material(handle)),
        hashlib.sha256,
    ).digest()


def _issue_verified_evidence_read_handle(
    *,
    authorization: Mapping[str, Any],
    **values: Any,
) -> VerifiedEvidenceReadHandle:
    """Issue one HMAC-sealed handle bound to the full caller mapping."""

    if "authorization_sha256" in values:
        raise EvidenceStoreError(
            "evidence_read_authorization_invalid",
            "authorization digest is derived only by the Evidence boundary",
        )
    handle = VerifiedEvidenceReadHandle(
        **values,
        authorization_sha256=_evidence_read_authorization_digest(authorization),
    )
    object.__setattr__(
        handle,
        "_issuance_tag",
        _evidence_read_handle_issuance_tag(handle),
    )
    handle.verify_issued(authorization=authorization)
    return handle


@dataclass(frozen=True)
class EvidenceOrphan:
    """One physical CAS object absent from committed Blob metadata."""

    sha256: str
    byte_length: int
    logical_path: str
    project_id: str
    root_identity: str
    observed_project_commit: int
    observed_root_digest: str
    state: str = "unreferenced_physical_object"
    canonical_effect: str = "none"

    def __post_init__(self) -> None:
        _validate_digest(self.sha256)
        if self.byte_length < 0 or self.logical_path != cas_relative_path(self.sha256):
            raise ValueError("orphan identity must match one physical CAS object")
        if (
            self.state != "unreferenced_physical_object"
            or self.canonical_effect != "none"
        ):
            raise ValueError("orphan records are proof-neutral physical observations")


@dataclass(frozen=True)
class EvidenceOrphanDisposition:
    """Non-executing, store-bound disposition boundary for one orphan."""

    orphan: EvidenceOrphan
    action: str
    authorization_sha256: str | None
    next_boundary: str
    executable: bool = False
    canonical_effect: str = "none"

    def __post_init__(self) -> None:
        if self.action not in {"quarantine", "adopt", "authorized_removal"}:
            raise ValueError("unsupported orphan disposition")
        expected = {
            "quarantine": "quarantine_command",
            "adopt": "writer_metadata_command",
            "authorized_removal": "external_security_deletion_command",
        }[self.action]
        if self.next_boundary != expected:
            raise ValueError("orphan disposition names the wrong execution boundary")
        if self.action == "authorized_removal":
            if self.authorization_sha256 is None:
                raise ValueError("orphan removal requires exact authorization")
            _validate_digest(self.authorization_sha256)
        elif self.authorization_sha256 is not None:
            _validate_digest(self.authorization_sha256)
        if self.executable or self.canonical_effect != "none":
            raise ValueError("orphan dispositions are plans, never direct mutations")


@dataclass(frozen=True)
class EvidenceItemRevision:
    evidence_id: str
    revision: int
    subtype: str
    subject: Mapping[str, Any]
    exact_scope: str
    rigor: str
    limitations: tuple[str, ...]
    non_inferences: tuple[str, ...]
    security_classification: str
    retention: str
    blob_roles: tuple[tuple[str, str], ...]
    availability_state: str = "pending_closure"
    canonical_effect: str = "none"

    def __post_init__(self) -> None:
        if not self.evidence_id or self.revision < 1:
            raise ValueError("Evidence identity and positive revision are required")
        if self.canonical_effect != "none":
            raise ValueError("Evidence metadata cannot claim a canonical effect")
        if self.availability_state not in {
            "pending_closure",
            "verified_available",
            "unavailable",
        }:
            raise ValueError("invalid Evidence availability state")
        if not self.non_inferences:
            raise ValueError("Evidence must state at least one non-inference")
        object.__setattr__(self, "subject", deep_freeze(self.subject))

    def to_payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "revision": self.revision,
            "subtype": self.subtype,
            "subject": deep_thaw(self.subject),
            "exact_scope": self.exact_scope,
            "rigor": self.rigor,
            "limitations": list(self.limitations),
            "non_inferences": list(self.non_inferences),
            "security_classification": self.security_classification,
            "retention": self.retention,
            "blob_roles": [list(item) for item in self.blob_roles],
            "availability_state": self.availability_state,
            "canonical_effect": "none",
        }


@dataclass(frozen=True)
class ProvenanceEvent:
    provenance_id: str
    evidence_id: str
    evidence_revision: int
    origin: str
    activity: str
    actor: str
    tool: str | None
    input_digests: tuple[str, ...]
    transformation: str | None
    custody: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "provenance_id": self.provenance_id,
            "evidence_id": self.evidence_id,
            "evidence_revision": self.evidence_revision,
            "origin": self.origin,
            "activity": self.activity,
            "actor": self.actor,
            "tool": self.tool,
            "input_digests": list(self.input_digests),
            "transformation": self.transformation,
            "custody": self.custody,
        }


@dataclass(frozen=True)
class IndependenceDisclosure:
    disclosure_id: str
    evidence_id: str
    evidence_revision: int
    model: str | None
    prompt_context_exposure: tuple[str, ...]
    method: str
    sources: tuple[str, ...]
    implementation: str
    environment: str
    shared_intermediate_correlation: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "disclosure_id": self.disclosure_id,
            "evidence_id": self.evidence_id,
            "evidence_revision": self.evidence_revision,
            "model": self.model,
            "prompt_context_exposure": list(self.prompt_context_exposure),
            "method": self.method,
            "sources": list(self.sources),
            "implementation": self.implementation,
            "environment": self.environment,
            "shared_intermediate_correlation": list(
                self.shared_intermediate_correlation
            ),
        }


_RESOLUTION_REQUIREMENT_SCOPE_KEYS = frozenset(
    {
        "session_id",
        "requirement_kind",
        "authored_trigger_basis",
        "subject_scope",
    }
)
_RESOLUTION_SATISFACTION_SCOPE_KEYS = frozenset(
    {"scope_kind", "consumer_use", "requirement_relation_id"}
)
RESOLUTION_SATISFACTION_SCOPE_KIND = "resolution_requirement_satisfaction"
RESOLUTION_SATISFACTION_CONSUMER_USE = "satisfies_resolution_requirement"


def _freeze_canonical_json_object(
    value: Mapping[str, Any],
    *,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty JSON object")

    def validate(item: Any, *, location: str) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(f"{location} keys must be non-empty strings")
                validate(child, location=f"{location}.{key}")
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                validate(child, location=f"{location}[{index}]")
            return
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        raise ValueError(f"{location} must contain only finite JSON values")

    validate(value, location=label)
    canonical_json_bytes(value)
    return deep_freeze(value)


@dataclass(frozen=True)
class ResolutionRequirementScope:
    """Canonical authored scope for one proof-neutral resolution requirement."""

    session_id: str
    requirement_kind: ResolutionRequirementKind | str
    authored_trigger_basis: Mapping[str, Any]
    subject_scope: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("resolution requirement session_id must be non-empty")
        try:
            kind = ResolutionRequirementKind(self.requirement_kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported resolution requirement kind") from exc
        object.__setattr__(self, "requirement_kind", kind)
        object.__setattr__(
            self,
            "authored_trigger_basis",
            _freeze_canonical_json_object(
                self.authored_trigger_basis,
                label="authored_trigger_basis",
            ),
        )
        object.__setattr__(
            self,
            "subject_scope",
            _freeze_canonical_json_object(
                self.subject_scope,
                label="subject_scope",
            ),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ResolutionRequirementScope":
        if (
            not isinstance(payload, Mapping)
            or set(payload) != _RESOLUTION_REQUIREMENT_SCOPE_KEYS
        ):
            raise ValueError("resolution requirement scope has an invalid shape")
        return cls(
            session_id=payload["session_id"],
            requirement_kind=payload["requirement_kind"],
            authored_trigger_basis=payload["authored_trigger_basis"],
            subject_scope=payload["subject_scope"],
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "requirement_kind": self.requirement_kind.value,
            "authored_trigger_basis": deep_thaw(self.authored_trigger_basis),
            "subject_scope": deep_thaw(self.subject_scope),
        }


@dataclass(frozen=True)
class ResolutionRequirementSatisfactionScope:
    """Exact consumer-owned use edge that satisfies one named requirement."""

    requirement_relation_id: str
    scope_kind: str = RESOLUTION_SATISFACTION_SCOPE_KIND
    consumer_use: str = RESOLUTION_SATISFACTION_CONSUMER_USE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.requirement_relation_id, str)
            or not self.requirement_relation_id.strip()
        ):
            raise ValueError("requirement_relation_id must be non-empty")
        if self.scope_kind != RESOLUTION_SATISFACTION_SCOPE_KIND:
            raise ValueError("unsupported resolution satisfaction scope kind")
        if self.consumer_use != RESOLUTION_SATISFACTION_CONSUMER_USE:
            raise ValueError("unsupported resolution satisfaction consumer use")

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "ResolutionRequirementSatisfactionScope":
        if (
            not isinstance(payload, Mapping)
            or set(payload) != _RESOLUTION_SATISFACTION_SCOPE_KEYS
        ):
            raise ValueError("resolution satisfaction scope has an invalid shape")
        return cls(
            requirement_relation_id=payload["requirement_relation_id"],
            scope_kind=payload["scope_kind"],
            consumer_use=payload["consumer_use"],
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "scope_kind": self.scope_kind,
            "consumer_use": self.consumer_use,
            "requirement_relation_id": self.requirement_relation_id,
        }


@dataclass(frozen=True)
class EvidenceRelation:
    relation_id: str
    relation_kind: str
    source_evidence: tuple[str, int]
    target_evidence: tuple[str, int]
    exact_scope: (
        str
        | Mapping[str, Any]
        | ResolutionRequirementScope
        | ResolutionRequirementSatisfactionScope
    )
    consumer_use: str | None = None

    def __post_init__(self) -> None:
        if self.relation_kind not in {
            "successor",
            "correction",
            "reproduction",
            "review",
            "duplicate",
            "dispute",
            "consumer_use",
            "resolution_requirement",
        }:
            raise ValueError("unsupported Evidence relation")
        if self.relation_kind == "resolution_requirement":
            scope = (
                self.exact_scope
                if isinstance(self.exact_scope, ResolutionRequirementScope)
                else ResolutionRequirementScope.from_payload(self.exact_scope)
            )
            object.__setattr__(self, "exact_scope", deep_freeze(scope.to_payload()))
        elif self.relation_kind == "consumer_use" and not isinstance(
            self.exact_scope, str
        ):
            scope = (
                self.exact_scope
                if isinstance(self.exact_scope, ResolutionRequirementSatisfactionScope)
                else ResolutionRequirementSatisfactionScope.from_payload(
                    self.exact_scope
                )
            )
            object.__setattr__(self, "exact_scope", deep_freeze(scope.to_payload()))
            object.__setattr__(self, "consumer_use", scope.consumer_use)
        elif not isinstance(self.exact_scope, str):
            raise ValueError("existing Evidence relation scopes remain strings")

    def to_payload(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "relation_kind": self.relation_kind,
            "source_evidence": list(self.source_evidence),
            "target_evidence": list(self.target_evidence),
            "exact_scope": (
                deep_thaw(self.exact_scope)
                if isinstance(self.exact_scope, Mapping)
                else self.exact_scope
            ),
            "consumer_use": self.consumer_use,
        }


def build_resolution_requirement_relation(
    *,
    relation_id: str,
    source_settled_session_output_evidence: tuple[str, int],
    target_requirement_evidence: tuple[str, int],
    session_id: str,
    requirement_kind: ResolutionRequirementKind | str,
    authored_trigger_basis: Mapping[str, Any],
    subject_scope: Mapping[str, Any],
) -> EvidenceRelation:
    """Build the immutable relation; settlement/type checks remain source-bound."""

    if source_settled_session_output_evidence == target_requirement_evidence:
        raise ValueError("resolution requirement source and target must be distinct")
    return EvidenceRelation(
        relation_id=relation_id,
        relation_kind="resolution_requirement",
        source_evidence=source_settled_session_output_evidence,
        target_evidence=target_requirement_evidence,
        exact_scope=ResolutionRequirementScope(
            session_id=session_id,
            requirement_kind=requirement_kind,
            authored_trigger_basis=authored_trigger_basis,
            subject_scope=subject_scope,
        ),
    )


def resolution_requirement_is_satisfied(
    requirement: EvidenceRelation,
    relations: Iterable[EvidenceRelation],
) -> bool:
    """Derive satisfaction from a later consumer-use edge, never mutable state."""

    if requirement.relation_kind != "resolution_requirement":
        raise ValueError("satisfaction requires a resolution_requirement relation")
    for relation in relations:
        if (
            relation.relation_kind != "consumer_use"
            or relation.target_evidence != requirement.target_evidence
            or relation.source_evidence
            in {requirement.source_evidence, requirement.target_evidence}
            or not isinstance(relation.exact_scope, Mapping)
        ):
            continue
        try:
            scope = ResolutionRequirementSatisfactionScope.from_payload(
                relation.exact_scope
            )
        except ValueError:
            continue
        if scope.requirement_relation_id == requirement.relation_id:
            return True
    return False


def build_resolution_requirement_satisfaction_relation(
    *,
    relation_id: str,
    source_resolving_evidence: tuple[str, int],
    requirement: EvidenceRelation,
) -> EvidenceRelation:
    """Build the exact immutable consumer-use edge targeting a requirement."""

    if requirement.relation_kind != "resolution_requirement":
        raise ValueError("satisfaction target must be a resolution requirement")
    if source_resolving_evidence in {
        requirement.source_evidence,
        requirement.target_evidence,
    }:
        raise ValueError(
            "resolution satisfaction must use distinct later resolving Evidence"
        )
    scope = ResolutionRequirementSatisfactionScope(
        requirement_relation_id=requirement.relation_id
    )
    return EvidenceRelation(
        relation_id=relation_id,
        relation_kind="consumer_use",
        source_evidence=source_resolving_evidence,
        target_evidence=requirement.target_evidence,
        exact_scope=scope,
        consumer_use=scope.consumer_use,
    )


@dataclass(frozen=True, order=True)
class ClosureReference:
    relation: str
    target_kind: str
    target_id: str
    target_revision: int | None = None

    def __post_init__(self) -> None:
        if self.relation not in CLOSURE_REFERENCE_KINDS:
            raise ValueError(f"unsupported closure reference relation: {self.relation}")
        if self.target_kind not in CLOSURE_MEMBER_KINDS or not self.target_id:
            raise ValueError("closure reference requires a typed target")
        if self.target_revision is not None and (
            isinstance(self.target_revision, bool) or self.target_revision < 1
        ):
            raise ValueError("closure target revision must be positive or absent")

    @property
    def key(self) -> tuple[str, str, int | None]:
        return (self.target_kind, self.target_id, self.target_revision)

    def to_payload(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "target_revision": self.target_revision,
        }


@dataclass(frozen=True)
class ClosureMember:
    kind: str
    member_id: str
    revision: int | None
    payload_sha256: str
    references: tuple[ClosureReference, ...] = ()
    available: bool = True

    def __post_init__(self) -> None:
        if self.kind not in CLOSURE_MEMBER_KINDS or not self.member_id:
            raise ValueError("closure member requires a supported kind and ID")
        if self.revision is not None and (
            isinstance(self.revision, bool) or self.revision < 1
        ):
            raise ValueError("closure member revision must be positive or absent")
        _validate_digest(self.payload_sha256)
        if self.kind == "blob":
            if self.revision is not None or self.member_id != self.payload_sha256:
                raise ValueError(
                    "blob closure members use their digest as ID and have no revision"
                )
        if not isinstance(self.available, bool):
            raise ValueError("closure availability must be boolean")
        object.__setattr__(
            self,
            "references",
            tuple(
                sorted(
                    self.references,
                    key=lambda item: (
                        item.relation,
                        item.target_kind,
                        item.target_id,
                        item.target_revision
                        if item.target_revision is not None
                        else -1,
                    ),
                )
            ),
        )

    @property
    def key(self) -> tuple[str, str, int | None]:
        return (self.kind, self.member_id, self.revision)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "member_id": self.member_id,
            "revision": self.revision,
            "payload_sha256": self.payload_sha256,
            "references": [item.to_payload() for item in self.references],
            "available": self.available,
        }


@dataclass(frozen=True)
class DeletionDirective:
    directive_id: str
    authorization_sha256: str
    reason: str
    blob_sha256s: tuple[str, ...] = ()
    evidence_references: tuple[tuple[str, int], ...] = ()
    lifecycle: str = "active"
    _disposition_digest: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _issuance_tag: bytes | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not self.directive_id
            or not self.reason
            or self.lifecycle not in {"active", "closed"}
        ):
            raise ValueError(
                "deletion directive identity, reason, and lifecycle are required"
            )
        _validate_digest(self.authorization_sha256)
        for digest in self.blob_sha256s:
            _validate_digest(digest)
        for evidence_id, revision in self.evidence_references:
            if not evidence_id or revision < 1:
                raise ValueError(
                    "deletion Evidence references require ID and positive revision"
                )
        object.__setattr__(self, "blob_sha256s", tuple(sorted(set(self.blob_sha256s))))
        object.__setattr__(
            self, "evidence_references", tuple(sorted(set(self.evidence_references)))
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "directive_id": self.directive_id,
            "authorization_sha256": self.authorization_sha256,
            "reason": self.reason,
            "blob_sha256s": list(self.blob_sha256s),
            "evidence_references": [list(item) for item in self.evidence_references],
            "lifecycle": self.lifecycle,
        }

    def verify_issued(self) -> None:
        _verify_security_record_issuance(self, "deletion_directive")


@dataclass(frozen=True)
class EvidenceTombstone:
    tombstone_id: str
    evidence_id: str
    evidence_revision: int
    directive_id: str
    reason_sha256: str
    _disposition_digest: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _issuance_tag: bytes | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not self.tombstone_id
            or not self.evidence_id
            or self.evidence_revision < 1
            or not self.directive_id
        ):
            raise ValueError(
                "Evidence tombstone requires exact Evidence and directive identity"
            )
        _validate_digest(self.reason_sha256)

    def to_payload(self) -> dict[str, Any]:
        return {
            "tombstone_id": self.tombstone_id,
            "evidence_id": self.evidence_id,
            "evidence_revision": self.evidence_revision,
            "directive_id": self.directive_id,
            "reason_sha256": self.reason_sha256,
        }

    def verify_issued(self) -> None:
        _verify_security_record_issuance(self, "evidence_tombstone")


@dataclass(frozen=True)
class EvidenceSecurityDisposition:
    """One exact, issued directive/tombstone pair and its source digests."""

    directive: DeletionDirective
    tombstone: EvidenceTombstone
    authorization_sha256: str
    evidence_payload_sha256: str
    _issuance_tag: bytes | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _validate_digest(self.authorization_sha256)
        _validate_digest(self.evidence_payload_sha256)

    def verify_issued(self) -> None:
        if type(self) is not EvidenceSecurityDisposition:
            raise EvidenceStoreError(
                "evidence_security_authority_invalid",
                "security disposition subclasses cannot carry authority",
            )
        self.directive.verify_issued()
        self.tombstone.verify_issued()
        if (
            self.directive._disposition_digest is None
            or self.directive._disposition_digest != self.tombstone._disposition_digest
            or self.authorization_sha256 != self.directive.authorization_sha256
            or self.directive.directive_id != self.tombstone.directive_id
            or self.directive.evidence_references
            != ((self.tombstone.evidence_id, self.tombstone.evidence_revision),)
            or self.tombstone.reason_sha256
            != hashlib.sha256(self.directive.reason.encode("utf-8")).hexdigest()
        ):
            raise EvidenceStoreError(
                "evidence_security_authority_invalid",
                "security disposition pair is not exactly bound",
            )
        expected = _security_disposition_wrapper_tag(self)
        if self._issuance_tag is None or not hmac.compare_digest(
            self._issuance_tag, expected
        ):
            raise EvidenceStoreError(
                "evidence_security_authority_invalid",
                "security disposition issuance seal is absent or invalid",
            )


def _security_record_tag(
    record: DeletionDirective | EvidenceTombstone,
    kind: str,
    disposition_digest: str,
) -> bytes:
    return hmac.new(
        _SECURITY_DISPOSITION_ISSUANCE_KEY,
        canonical_json_bytes(
            {
                "record_kind": kind,
                "disposition_digest": disposition_digest,
                "record": record.to_payload(),
            }
        ),
        hashlib.sha256,
    ).digest()


def _verify_security_record_issuance(
    record: DeletionDirective | EvidenceTombstone,
    kind: str,
) -> None:
    expected_type = (
        DeletionDirective if kind == "deletion_directive" else EvidenceTombstone
    )
    if type(record) is not expected_type or record._disposition_digest is None:
        raise EvidenceStoreError(
            "evidence_security_authority_invalid",
            f"{expected_type.__name__} was not issued by the Evidence security boundary",
        )
    _validate_digest(record._disposition_digest)
    expected = _security_record_tag(record, kind, record._disposition_digest)
    if record._issuance_tag is None or not hmac.compare_digest(
        record._issuance_tag, expected
    ):
        raise EvidenceStoreError(
            "evidence_security_authority_invalid",
            f"{expected_type.__name__} issuance seal is absent or invalid",
        )


def _issue_security_record(
    record: DeletionDirective | EvidenceTombstone,
    *,
    kind: str,
    disposition_digest: str,
) -> None:
    object.__setattr__(record, "_disposition_digest", disposition_digest)
    object.__setattr__(
        record,
        "_issuance_tag",
        _security_record_tag(record, kind, disposition_digest),
    )
    record.verify_issued()


def _security_disposition_wrapper_tag(
    disposition: EvidenceSecurityDisposition,
) -> bytes:
    return hmac.new(
        _SECURITY_DISPOSITION_ISSUANCE_KEY,
        canonical_json_bytes(
            {
                "kind": "evidence_security_disposition",
                "authorization_sha256": disposition.authorization_sha256,
                "evidence_payload_sha256": disposition.evidence_payload_sha256,
                "disposition_digest": disposition.directive._disposition_digest,
                "directive": disposition.directive.to_payload(),
                "tombstone": disposition.tombstone.to_payload(),
            }
        ),
        hashlib.sha256,
    ).digest()


def prepare_evidence_security_disposition(
    *,
    evidence: EvidenceItemRevision,
    authorization: Mapping[str, Any],
    reason: str,
    blob_sha256s: Sequence[str],
    directive_id: str,
    tombstone_id: str,
) -> EvidenceSecurityDisposition:
    """Issue one deletion directive and tombstone from complete exact sources.

    ``authorization_sha256`` is always derived from the entire supplied
    authorization mapping; callers cannot inject a precomputed digest.
    """

    if type(evidence) is not EvidenceItemRevision:
        raise TypeError("security disposition requires an exact EvidenceItemRevision")
    if evidence.availability_state != "unavailable":
        raise EvidenceStoreError(
            "evidence_security_disposition_invalid",
            "only unavailable Evidence may receive a deletion disposition",
        )
    if not isinstance(authorization, Mapping) or not authorization:
        raise EvidenceStoreError(
            "evidence_security_disposition_invalid",
            "complete nonempty authorization mapping is required",
        )
    if not isinstance(reason, str) or not reason.strip():
        raise EvidenceStoreError(
            "evidence_security_disposition_invalid",
            "exact nonempty disposition reason is required",
        )
    if not isinstance(directive_id, str) or not directive_id.strip():
        raise EvidenceStoreError(
            "evidence_security_disposition_invalid",
            "directive ID is required",
        )
    if not isinstance(tombstone_id, str) or not tombstone_id.strip():
        raise EvidenceStoreError(
            "evidence_security_disposition_invalid",
            "tombstone ID is required",
        )
    requested_blobs = tuple(blob_sha256s)
    for digest in requested_blobs:
        _validate_digest(digest)
    if len(requested_blobs) != len(set(requested_blobs)):
        raise EvidenceStoreError(
            "evidence_security_disposition_invalid",
            "disposition Blob inventory cannot contain duplicates",
        )
    expected_blobs = tuple(sorted({digest for _, digest in evidence.blob_roles}))
    if tuple(sorted(requested_blobs)) != expected_blobs:
        raise EvidenceStoreError(
            "evidence_security_disposition_invalid",
            "disposition Blob inventory must exactly equal the Evidence revision",
        )

    authorization_document = deep_thaw(authorization)
    authorization_sha256 = _canonical_digest(authorization_document)
    exact_reason = reason
    directive = DeletionDirective(
        directive_id=directive_id,
        authorization_sha256=authorization_sha256,
        reason=exact_reason,
        blob_sha256s=expected_blobs,
        evidence_references=((evidence.evidence_id, evidence.revision),),
        lifecycle="active",
    )
    tombstone = EvidenceTombstone(
        tombstone_id=tombstone_id,
        evidence_id=evidence.evidence_id,
        evidence_revision=evidence.revision,
        directive_id=directive.directive_id,
        reason_sha256=hashlib.sha256(exact_reason.encode("utf-8")).hexdigest(),
    )
    evidence_payload_sha256 = _canonical_digest(evidence.to_payload())
    disposition_digest = _canonical_digest(
        {
            "authorization": authorization_document,
            "evidence": evidence.to_payload(),
            "directive": directive.to_payload(),
            "tombstone": tombstone.to_payload(),
        }
    )
    _issue_security_record(
        directive,
        kind="deletion_directive",
        disposition_digest=disposition_digest,
    )
    _issue_security_record(
        tombstone,
        kind="evidence_tombstone",
        disposition_digest=disposition_digest,
    )
    disposition = EvidenceSecurityDisposition(
        directive=directive,
        tombstone=tombstone,
        authorization_sha256=authorization_sha256,
        evidence_payload_sha256=evidence_payload_sha256,
    )
    object.__setattr__(
        disposition,
        "_issuance_tag",
        _security_disposition_wrapper_tag(disposition),
    )
    disposition.verify_issued()
    return disposition


@dataclass(frozen=True)
class ClosureManifest:
    closure_id: str
    root: ClosureReference
    members: tuple[ClosureMember, ...]
    omissions: tuple[str, ...]
    conflicts: tuple[str, ...]
    canonical_pre_state: Mapping[str, Any]
    manifest_sha256: str
    canonical_effect: str = "none"
    _issuance_mode: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _issuance_tag: bytes | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not self.closure_id:
            raise ValueError("closure ID is required")
        _validate_digest(self.manifest_sha256)
        if self.canonical_effect != "none":
            raise ValueError("closure manifests cannot claim a canonical effect")
        object.__setattr__(
            self, "members", tuple(sorted(self.members, key=lambda item: item.key))
        )
        object.__setattr__(
            self, "canonical_pre_state", deep_freeze(self.canonical_pre_state)
        )

    @property
    def blob_sha256s(self) -> tuple[str, ...]:
        return tuple(
            member.member_id for member in self.members if member.kind == "blob"
        )

    def body_payload(self) -> dict[str, Any]:
        return {
            "closure_id": self.closure_id,
            "root": self.root.to_payload(),
            "members": [item.to_payload() for item in self.members],
            "omissions": list(self.omissions),
            "conflicts": list(self.conflicts),
            "canonical_pre_state": deep_thaw(self.canonical_pre_state),
            "canonical_effect": "none",
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.body_payload(), "manifest_sha256": self.manifest_sha256}

    def verify_issued(self, *, for_commit: bool = False) -> None:
        """Verify process-local issuance and, when requested, commit authority.

        Persisted manifests are reissued in ``read`` mode after exact shape and
        digest validation.  Only a manifest built and completely verified from
        physical CAS records in this process receives ``commit`` authority.
        """

        if type(self) is not ClosureManifest or self._issuance_mode not in {
            "commit",
            "read",
        }:
            raise EvidenceStoreError(
                "evidence_closure_authority_invalid",
                "ClosureManifest was not issued by an Evidence boundary",
            )
        if for_commit and self._issuance_mode != "commit":
            raise EvidenceStoreError(
                "evidence_closure_authority_invalid",
                "rehydrated ClosureManifest cannot authorize a new commit",
            )
        expected = _closure_manifest_issuance_tag(self, self._issuance_mode)
        if self._issuance_tag is None or not hmac.compare_digest(
            self._issuance_tag, expected
        ):
            raise EvidenceStoreError(
                "evidence_closure_authority_invalid",
                "ClosureManifest issuance seal is absent or invalid",
            )


def _closure_manifest_issuance_tag(
    manifest: ClosureManifest,
    mode: str,
) -> bytes:
    return hmac.new(
        _CLOSURE_MANIFEST_ISSUANCE_KEY,
        canonical_json_bytes(
            {
                "mode": mode,
                "manifest": manifest.to_payload(),
            }
        ),
        hashlib.sha256,
    ).digest()


def _issue_closure_manifest(
    manifest: ClosureManifest,
    *,
    mode: str,
) -> ClosureManifest:
    if mode not in {"commit", "read"}:
        raise ValueError("unsupported ClosureManifest issuance mode")
    object.__setattr__(manifest, "_issuance_mode", mode)
    object.__setattr__(
        manifest,
        "_issuance_tag",
        _closure_manifest_issuance_tag(manifest, mode),
    )
    manifest.verify_issued(for_commit=mode == "commit")
    return manifest


def closure_manifest_from_payload(payload: Mapping[str, Any]) -> ClosureManifest:
    """Rehydrate one exact persisted closure manifest, rejecting shape drift."""

    expected_keys = {
        "closure_id",
        "root",
        "members",
        "omissions",
        "conflicts",
        "canonical_pre_state",
        "manifest_sha256",
        "canonical_effect",
    }
    if set(payload) != expected_keys:
        raise EvidenceStoreError(
            "evidence_closure_invalid",
            "persisted closure manifest has missing or unknown fields",
        )
    root_payload = payload["root"]
    members_payload = payload["members"]
    if not isinstance(root_payload, Mapping) or not isinstance(members_payload, list):
        raise EvidenceStoreError(
            "evidence_closure_invalid", "persisted closure shape is invalid"
        )
    try:
        root = ClosureReference(
            relation=str(root_payload["relation"]),
            target_kind=str(root_payload["target_kind"]),
            target_id=str(root_payload["target_id"]),
            target_revision=(
                int(root_payload["target_revision"])
                if root_payload["target_revision"] is not None
                else None
            ),
        )
        members: list[ClosureMember] = []
        for raw_member in members_payload:
            if not isinstance(raw_member, Mapping):
                raise TypeError("closure member must be an object")
            raw_references = raw_member["references"]
            if not isinstance(raw_references, list):
                raise TypeError("closure references must be a list")
            references = tuple(
                ClosureReference(
                    relation=str(raw["relation"]),
                    target_kind=str(raw["target_kind"]),
                    target_id=str(raw["target_id"]),
                    target_revision=(
                        int(raw["target_revision"])
                        if raw["target_revision"] is not None
                        else None
                    ),
                )
                for raw in raw_references
            )
            members.append(
                ClosureMember(
                    kind=str(raw_member["kind"]),
                    member_id=str(raw_member["member_id"]),
                    revision=(
                        int(raw_member["revision"])
                        if raw_member["revision"] is not None
                        else None
                    ),
                    payload_sha256=str(raw_member["payload_sha256"]),
                    references=references,
                    available=raw_member["available"],
                )
            )
        omissions = payload["omissions"]
        conflicts = payload["conflicts"]
        canonical_pre_state = payload["canonical_pre_state"]
        if (
            not isinstance(omissions, list)
            or not all(isinstance(item, str) for item in omissions)
            or not isinstance(conflicts, list)
            or not all(isinstance(item, str) for item in conflicts)
            or not isinstance(canonical_pre_state, Mapping)
        ):
            raise TypeError("closure contract fields have invalid types")
        manifest = ClosureManifest(
            closure_id=str(payload["closure_id"]),
            root=root,
            members=tuple(members),
            omissions=tuple(omissions),
            conflicts=tuple(conflicts),
            canonical_pre_state=dict(canonical_pre_state),
            manifest_sha256=str(payload["manifest_sha256"]),
            canonical_effect=str(payload["canonical_effect"]),
        )
        if canonical_json_bytes(payload) != canonical_json_bytes(manifest.to_payload()):
            raise ValueError("closure manifest payload is not in exact canonical shape")
        if _canonical_digest(manifest.body_payload()) != manifest.manifest_sha256:
            raise ValueError("closure manifest digest does not match its body")
        return _issue_closure_manifest(manifest, mode="read")
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceStoreError(
            "evidence_closure_invalid",
            f"persisted closure manifest cannot be rehydrated: {exc}",
        ) from exc


def _closure_failure(message: str) -> None:
    raise EvidenceStoreError("evidence_closure_invalid", message)


def verify_closure_manifest(
    manifest: ClosureManifest,
    *,
    blobs: Mapping[str, BlobRecord],
    cas: "EvidenceCAS",
    deletion_directives: Sequence[DeletionDirective] = (),
    tombstones: Sequence[EvidenceTombstone] = (),
) -> None:
    if _canonical_digest(manifest.body_payload()) != manifest.manifest_sha256:
        _closure_failure("closure manifest digest mismatch")
    if not manifest.members:
        _closure_failure("closure must contain members")
    members = {member.key: member for member in manifest.members}
    if len(members) != len(manifest.members):
        _closure_failure("closure member identities must be unique")
    root_member = members.get(manifest.root.key)
    if root_member is None or manifest.root.target_kind != manifest.root.relation:
        _closure_failure("closure root must name one exact typed member")
    deleted_blobs = {
        digest
        for directive in deletion_directives
        if directive.lifecycle == "active"
        for digest in directive.blob_sha256s
    }
    deleted_evidence = {
        reference
        for directive in deletion_directives
        if directive.lifecycle == "active"
        for reference in directive.evidence_references
    }
    tombstoned = {(item.evidence_id, item.evidence_revision) for item in tombstones}
    directive_ids = {item.directive_id for item in deletion_directives}
    if any(item.directive_id not in directive_ids for item in tombstones):
        _closure_failure("tombstone references an absent deletion directive")
    for member in manifest.members:
        if not member.available:
            _closure_failure(
                f"closure member is unavailable: {member.kind}:{member.member_id}"
            )
        if member.kind == "blob":
            if member.member_id in deleted_blobs:
                _closure_failure("active closure references security-deleted blob")
            record = blobs.get(member.member_id)
            if record is None:
                _closure_failure(f"blob metadata is absent: {member.member_id}")
            if record.quarantine_state != "clear":
                _closure_failure(
                    "quarantined blob cannot enter an ordinary Evidence closure"
                )
            if (
                record.integrity_state != "verified"
                or record.availability_state
                not in {
                    "installed_pending_metadata",
                    "verified_available",
                }
            ):
                _closure_failure("blob is not physically verified for closure")
            cas.verify_record(record)
            if record.sha256 != member.payload_sha256:
                _closure_failure("blob member digest differs from Blob metadata")
        if member.kind == "evidence":
            reference = (member.member_id, member.revision or 0)
            if reference in deleted_evidence or reference in tombstoned:
                _closure_failure("active closure references tombstoned Evidence")
            required = {"blob", "provenance", "independence_disclosure"}
            present = {item.relation for item in member.references}
            if not required.issubset(present):
                _closure_failure(
                    "each Evidence member must bind blob, provenance, and independence disclosure"
                )
        for reference in member.references:
            if reference.relation != reference.target_kind:
                _closure_failure(
                    "closure reference relation and target kind must agree"
                )
            if reference.key not in members:
                _closure_failure(
                    f"closure reference target is absent: {reference.target_kind}:{reference.target_id}"
                )
    reachable: set[tuple[str, str, int | None]] = set()
    pending = [root_member.key]
    while pending:
        key = pending.pop()
        if key in reachable:
            continue
        reachable.add(key)
        pending.extend(reference.key for reference in members[key].references)
    if reachable != set(members):
        _closure_failure("closure contains members unreachable from its declared root")
    mandatory_kinds = {
        "evidence",
        "blob",
        "provenance",
        "independence_disclosure",
        "dependency",
    }
    present_kinds = {member.kind for member in manifest.members}
    if not mandatory_kinds.issubset(present_kinds):
        _closure_failure(
            "closure must bind Evidence, Blob, provenance, independence, and dependency members"
        )
    if set(blobs) != set(manifest.blob_sha256s):
        _closure_failure(
            "Blob metadata set must exactly equal frozen closure inventory"
        )


def build_closure_manifest(
    *,
    closure_id: str,
    root: ClosureReference,
    members: Iterable[ClosureMember],
    blobs: Mapping[str, BlobRecord],
    cas: "EvidenceCAS",
    omissions: Sequence[str] = (),
    conflicts: Sequence[str] = (),
    canonical_pre_state: Mapping[str, Any],
    deletion_directives: Sequence[DeletionDirective] = (),
    tombstones: Sequence[EvidenceTombstone] = (),
) -> ClosureManifest:
    ordered_members = tuple(sorted(tuple(members), key=lambda item: item.key))
    provisional = ClosureManifest(
        closure_id=closure_id,
        root=root,
        members=ordered_members,
        omissions=tuple(omissions),
        conflicts=tuple(conflicts),
        canonical_pre_state=dict(canonical_pre_state),
        manifest_sha256="0" * 64,
    )
    manifest = ClosureManifest(
        closure_id=closure_id,
        root=root,
        members=ordered_members,
        omissions=tuple(omissions),
        conflicts=tuple(conflicts),
        canonical_pre_state=dict(canonical_pre_state),
        manifest_sha256=_canonical_digest(provisional.body_payload()),
    )
    verify_closure_manifest(
        manifest,
        blobs=blobs,
        cas=cas,
        deletion_directives=deletion_directives,
        tombstones=tombstones,
    )
    return _issue_closure_manifest(manifest, mode="commit")


class EvidenceCAS:
    """Same-volume SHA-256 store with no-overwrite installation semantics.

    An exact ``WorkspacePaths`` binding is mandatory. This byte-custody layer
    intentionally has no
    default/live-root discovery and therefore cannot select an operational
    location by itself.
    """

    def __init__(
        self,
        paths: WorkspacePaths,
        *,
        observer: WorkspaceObserver = NOOP_WORKSPACE_OBSERVER,
        observation_environment: ObservationEnvironment = ObservationEnvironment.LOCAL,
    ) -> None:
        if not isinstance(paths, WorkspacePaths):
            raise TypeError("EvidenceCAS requires an admitted WorkspacePaths binding")
        paths.revalidate_physical(require_root=True)
        self.paths = paths
        self.root = paths.root
        self.cas_root = paths.cas_sha256
        self.staging_root = paths.staging
        self._observer = observer
        self._observation_environment = ObservationEnvironment(observation_environment)

    def _observe_event(
        self,
        kind: EventKind,
        attributes: Mapping[str, Any],
        *,
        level: ObservationLevel = ObservationLevel.INFO,
    ) -> bool:
        return emit_event_safely(
            self._observer,
            kind=kind,
            occurred_at=_utc_now(),
            level=level,
            environment=self._observation_environment,
            attributes=attributes,
        )

    def initialize(self) -> None:
        self.paths.revalidate_physical(require_root=True)
        _ensure_no_symlink(self.root, self.root)
        self.cas_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _ensure_no_symlink(self.cas_root, self.root)
        _ensure_no_symlink(self.staging_root, self.root)

    def path_for_digest(self, digest: str) -> Path:
        digest = _validate_digest(digest)
        return _contained(self.root, self.root / cas_relative_path(digest))

    def _remove_failed_stage(self, stage_directory: Path) -> None:
        """Remove non-authoritative staged bytes or freeze on ambiguity.

        A caller may safely retry the original operation only after the failed
        stage is proven absent.  Cleanup failure therefore cannot be hidden
        behind the original validation or capacity error.
        """

        try:
            shutil.rmtree(stage_directory)
            if stage_directory.exists():
                raise OSError("CAS staging cleanup left the stage directory present")
            _fsync_directory(self.staging_root)
        except BaseException as cleanup_error:
            raise IntegrityFreeze(
                "cas_staging_cleanup_failed",
                "Failed CAS staging could not be proven absent; freeze Evidence ingestion",
            ) from cleanup_error

    def stage_stream(
        self,
        stream: BinaryIO,
        *,
        original_name: str,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
        quarantine_reason: str | None = None,
    ) -> StagedBlob:
        validate_quarantine_reason(quarantine_reason)
        self.initialize()
        logical_name = _validate_logical_name(original_name)
        suffixes = {suffix.lower() for suffix in PurePosixPath(logical_name).suffixes}
        if suffixes & ARCHIVE_SUFFIXES or media_type.lower() in ARCHIVE_MEDIA_TYPES:
            raise EvidenceStoreError(
                "archive_payload_forbidden",
                "archive containers require separate reviewed extraction",
            )
        ingestion_id = f"ingest-{uuid.uuid4().hex}"
        stage_directory = _contained(
            self.staging_root, self.staging_root / ingestion_id
        )
        stage_directory.mkdir(mode=0o700)
        payload = stage_directory / "payload"
        digest = hashlib.sha256()
        length = 0
        prefix = bytearray()
        secret_tail = b""
        secret_found = False
        secret_window_size = 4096
        try:
            descriptor = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise EvidenceStoreError(
                            "invalid_stream", "Evidence stream must yield bytes"
                        )
                    length += len(chunk)
                    if len(prefix) < 4096:
                        prefix.extend(chunk[: 4096 - len(prefix)])
                    secret_window = secret_tail + chunk
                    if _contains_secret_like(secret_window):
                        secret_found = True
                    secret_tail = secret_window[-secret_window_size:]
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            is_archive = (
                any(bytes(prefix).startswith(magic) for magic in ARCHIVE_MAGIC)
                or bytes(prefix).startswith(b"BZh")
                or bytes(prefix).startswith(b"\xfd7zXZ\x00")
                or bytes(prefix)[257:262] == b"ustar"
            )
            has_secret = secret_found or _contains_secret_like(bytes(prefix))
            if is_archive:
                raise EvidenceStoreError(
                    "archive_payload_forbidden", "archive signature is forbidden"
                )
            if has_secret and quarantine_reason is None:
                raise EvidenceStoreError(
                    "secret_payload_requires_quarantine",
                    "secret-like bytes require explicit quarantine",
                )
            _fsync_directory(stage_directory)
            staged = _issue_staged_blob(
                ingestion_id=ingestion_id,
                stage_directory=stage_directory,
                payload_path=payload,
                sha256=digest.hexdigest(),
                length=length,
                media_type=media_type,
                encoding=encoding,
                original_name=logical_name,
                quarantine_reason=quarantine_reason,
            )
            self._observe_event(
                EventKind.CAS_STAGED,
                {
                    "digest_sha256": staged.sha256,
                    "byte_count": staged.length,
                    "status": "staged",
                },
            )
            return staged
        except OSError as exc:
            # Staging is not Evidence authority.  A capacity failure must leave
            # no partial payload that a later scan could mistake for a complete
            # staged object.
            self._remove_failed_stage(stage_directory)
            if _is_capacity_error(exc):
                raise EvidenceStoreError(
                    "cas_capacity_exhausted",
                    "CAS staging capacity was exhausted before a complete blob existed",
                ) from exc
            raise
        except Exception:
            # A validation failure is not a simulated crash; restricted bytes must
            # not remain staged.  Fault-injection hooks run during install instead.
            self._remove_failed_stage(stage_directory)
            raise

    def stage_bytes(self, data: bytes, **kwargs: Any) -> StagedBlob:
        from io import BytesIO

        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        return self.stage_stream(BytesIO(data), **kwargs)

    def stage_file(
        self,
        source: Path | str,
        *,
        original_name: str | None = None,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
        quarantine_reason: str | None = None,
    ) -> StagedBlob:
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise EvidenceStoreError(
                "selected_file_required",
                "only one explicit regular file may be ingested",
            )
        with path.open("rb") as stream:
            return self.stage_stream(
                stream,
                original_name=original_name or path.name,
                media_type=media_type,
                encoding=encoding,
                quarantine_reason=quarantine_reason,
            )

    @staticmethod
    def _is_link_or_reparse(details: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(details, "st_file_attributes", 0)
        return stat.S_ISLNK(details.st_mode) or bool(file_attributes & reparse_flag)

    @staticmethod
    def _same_source_identity(
        first: os.stat_result,
        second: os.stat_result,
    ) -> bool:
        return (
            first.st_dev,
            first.st_ino,
            first.st_mode,
            first.st_nlink,
            first.st_size,
            first.st_mtime_ns,
        ) == (
            second.st_dev,
            second.st_ino,
            second.st_mode,
            second.st_nlink,
            second.st_size,
            second.st_mtime_ns,
        )

    @classmethod
    def _lstat_exact_source(cls, path: Path) -> os.stat_result:
        try:
            details = os.lstat(path)
        except OSError as exc:
            raise EvidenceStoreError(
                "exact_file_unavailable",
                "The exact source file is unavailable.",
            ) from exc
        if cls._is_link_or_reparse(details):
            raise EvidenceStoreError(
                "exact_file_link_unsupported",
                "Exact source files cannot be symlinks or reparse points.",
            )
        if not stat.S_ISREG(details.st_mode):
            raise EvidenceStoreError(
                "exact_file_not_regular",
                "An exact source must identify one regular file.",
            )
        if details.st_nlink != 1:
            raise EvidenceStoreError(
                "exact_file_link_unsupported",
                "An exact source file cannot have multiple hard-link names.",
            )
        return details

    @classmethod
    def _require_exact_source_boundaries(cls, path: Path) -> None:
        current = path
        while True:
            try:
                details = os.lstat(current)
            except OSError as exc:
                raise EvidenceStoreError(
                    "exact_file_unavailable",
                    "The exact source path is unavailable.",
                ) from exc
            if cls._is_link_or_reparse(details):
                raise EvidenceStoreError(
                    "exact_file_link_unsupported",
                    "Exact source paths cannot traverse a symlink or reparse point.",
                )
            if current != path and not stat.S_ISDIR(details.st_mode):
                raise EvidenceStoreError(
                    "exact_file_path_invalid",
                    "An exact source path traverses a non-directory boundary.",
                )
            if current.parent == current:
                return
            current = current.parent

    def _normalize_exact_source(self, source: Path | str) -> Path:
        if not isinstance(source, (str, os.PathLike)):
            raise TypeError("exact source must be one filesystem path")
        path = Path(source)
        if not path.is_absolute():
            raise EvidenceStoreError(
                "exact_file_path_invalid",
                "An exact source path must be absolute.",
            )
        self._require_exact_source_boundaries(path)
        self._lstat_exact_source(path)
        try:
            resolved = path.resolve(strict=True)
            custody_root = self.root.resolve(strict=True)
        except OSError as exc:
            raise EvidenceStoreError(
                "exact_file_unavailable",
                "The exact source path could not be resolved.",
            ) from exc
        if resolved == custody_root or custody_root in resolved.parents:
            raise EvidenceStoreError(
                "exact_file_path_collision",
                "An exact source file cannot overlap the receiving Evidence workspace.",
            )
        return resolved

    @classmethod
    def _stream_exact_source(
        cls,
        path: Path,
        *,
        expected_sha256: str,
        expected_length: int,
        output: BinaryIO | None = None,
        fault_hook: Callable[[str, Path], None] | None = None,
    ) -> tuple[bytes, bool]:
        before = cls._lstat_exact_source(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise EvidenceStoreError(
                "exact_file_unavailable",
                "The exact source file could not be opened safely.",
            ) from exc
        digest = hashlib.sha256()
        length = 0
        prefix = bytearray()
        secret_tail = b""
        secret_found = False
        try:
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                opened = os.fstat(stream.fileno())
                if (
                    cls._is_link_or_reparse(opened)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or not cls._same_source_identity(before, opened)
                ):
                    raise EvidenceStoreError(
                        "exact_file_changed",
                        "The exact source changed while it was being opened.",
                    )
                if opened.st_size != expected_length:
                    raise EvidenceStoreError(
                        "exact_file_size_mismatch",
                        "The exact source byte length differs from its binding.",
                    )
                if fault_hook is not None:
                    fault_hook("after_source_opened", path)
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    length += len(chunk)
                    if output is not None:
                        output.write(chunk)
                    if len(prefix) < 4096:
                        prefix.extend(chunk[: 4096 - len(prefix)])
                    secret_window = secret_tail + chunk
                    if _contains_secret_like(secret_window):
                        secret_found = True
                    secret_tail = secret_window[-4096:]
                opened_after = os.fstat(stream.fileno())
        except OSError as exc:
            if output is not None and _is_capacity_error(exc):
                raise
            raise EvidenceStoreError(
                "exact_file_unavailable",
                "The exact source could not be streamed completely.",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if fault_hook is not None:
            fault_hook("after_source_streamed", path)
        after = cls._lstat_exact_source(path)
        cls._require_exact_source_boundaries(path)
        if not (
            cls._same_source_identity(opened, opened_after)
            and cls._same_source_identity(opened_after, after)
        ):
            raise EvidenceStoreError(
                "exact_file_changed",
                "The exact source changed while its bytes were being streamed.",
            )
        if length != expected_length:
            raise EvidenceStoreError(
                "exact_file_size_mismatch",
                "The exact source byte length differs from its binding.",
            )
        if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
            raise EvidenceStoreError(
                "exact_file_digest_mismatch",
                "The exact source digest differs from its binding.",
            )
        return bytes(prefix), secret_found

    def verify_exact_file_source(
        self,
        source: Path | str,
        *,
        expected_sha256: str,
        expected_length: int,
    ) -> Path:
        """Rehash one single-link regular source without taking byte custody."""

        self.initialize()
        digest_binding = _validate_digest(expected_sha256)
        if (
            isinstance(expected_length, bool)
            or not isinstance(expected_length, int)
            or expected_length < 0
        ):
            raise EvidenceStoreError(
                "exact_file_size_mismatch",
                "The exact source byte length must be a nonnegative integer.",
            )
        path = self._normalize_exact_source(source)
        self._stream_exact_source(
            path,
            expected_sha256=digest_binding,
            expected_length=expected_length,
        )
        return path

    def classify_exact_file_quarantine(
        self,
        source: Path | str,
        *,
        expected_sha256: str,
        expected_length: int,
        original_name: str,
        media_type: str = "application/octet-stream",
    ) -> str | None:
        """Return the closed custody class required for one exact sealed file.

        This is a read-only preflight for a caller that must preserve every
        output while keeping ordinary material readable.  The later custody
        operation reopens and rehashes the file, so a mutation between this
        classification and capture still fails closed.
        """

        self.initialize()
        digest_binding = _validate_digest(expected_sha256)
        if (
            isinstance(expected_length, bool)
            or not isinstance(expected_length, int)
            or expected_length < 0
        ):
            raise EvidenceStoreError(
                "exact_file_size_mismatch",
                "The exact source byte length must be a nonnegative integer.",
            )
        path = self._normalize_exact_source(source)
        logical_name = _validate_logical_name(original_name)
        if not isinstance(media_type, str) or not media_type:
            raise ValueError("media_type must be nonempty text")
        suffixes = {suffix.lower() for suffix in PurePosixPath(logical_name).suffixes}
        declared_archive = (
            bool(suffixes & ARCHIVE_SUFFIXES)
            or media_type.lower() in ARCHIVE_MEDIA_TYPES
        )
        prefix, secret_found = self._stream_exact_source(
            path,
            expected_sha256=digest_binding,
            expected_length=expected_length,
        )
        archive_signature = (
            any(prefix.startswith(magic) for magic in ARCHIVE_MAGIC)
            or prefix.startswith(b"BZh")
            or prefix.startswith(b"\xfd7zXZ\x00")
            or prefix[257:262] == b"ustar"
        )
        if (
            declared_archive
            or archive_signature
            or secret_found
            or _contains_secret_like(prefix)
        ):
            return QUARANTINE_REASON_OPAQUE_RESTRICTED
        return None

    def stage_exact_file(
        self,
        source: Path | str,
        *,
        expected_sha256: str,
        expected_length: int,
        original_name: str,
        media_type: str = "application/octet-stream",
        encoding: str | None = None,
        quarantine_reason: str | None = None,
        fault_hook: Callable[[str, Path], None] | None = None,
    ) -> StagedBlob:
        """Stream one predeclared sealed file into the existing CAS staging path."""

        validate_quarantine_reason(quarantine_reason)
        self.initialize()
        digest_binding = _validate_digest(expected_sha256)
        if (
            isinstance(expected_length, bool)
            or not isinstance(expected_length, int)
            or expected_length < 0
        ):
            raise EvidenceStoreError(
                "exact_file_size_mismatch",
                "The exact source byte length must be a nonnegative integer.",
            )
        path = self._normalize_exact_source(source)
        if fault_hook is not None:
            fault_hook("before_source_streamed", path)
        logical_name = _validate_logical_name(original_name)
        suffixes = {suffix.lower() for suffix in PurePosixPath(logical_name).suffixes}
        declared_archive = (
            bool(suffixes & ARCHIVE_SUFFIXES)
            or media_type.lower() in ARCHIVE_MEDIA_TYPES
        )
        if declared_archive and quarantine_reason is None:
            raise EvidenceStoreError(
                "archive_payload_forbidden",
                "archive-shaped exact files require opaque quarantine custody",
            )
        ingestion_id = f"ingest-{uuid.uuid4().hex}"
        stage_directory = _contained(
            self.staging_root, self.staging_root / ingestion_id
        )
        stage_directory.mkdir(mode=0o700)
        payload = stage_directory / "payload"
        try:
            output_descriptor = os.open(
                payload,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(output_descriptor, "wb") as output_stream:
                    output_descriptor = -1
                    prefix, secret_found = self._stream_exact_source(
                        path,
                        expected_sha256=digest_binding,
                        expected_length=expected_length,
                        output=output_stream,
                        fault_hook=fault_hook,
                    )
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            finally:
                if output_descriptor >= 0:
                    os.close(output_descriptor)
            is_archive = (
                any(prefix.startswith(magic) for magic in ARCHIVE_MAGIC)
                or prefix.startswith(b"BZh")
                or prefix.startswith(b"\xfd7zXZ\x00")
                or prefix[257:262] == b"ustar"
            )
            has_secret = secret_found or _contains_secret_like(prefix)
            if is_archive and quarantine_reason is None:
                raise EvidenceStoreError(
                    "archive_payload_forbidden",
                    "archive-shaped exact files require opaque quarantine custody",
                )
            if has_secret and quarantine_reason is None:
                raise EvidenceStoreError(
                    "secret_payload_requires_quarantine",
                    "secret-like bytes require explicit quarantine",
                )
            _fsync_directory(stage_directory)
            staged = _issue_staged_blob(
                ingestion_id=ingestion_id,
                stage_directory=stage_directory,
                payload_path=payload,
                sha256=digest_binding,
                length=expected_length,
                media_type=media_type,
                encoding=encoding,
                original_name=logical_name,
                quarantine_reason=quarantine_reason,
            )
            self._observe_event(
                EventKind.CAS_STAGED,
                {
                    "digest_sha256": staged.sha256,
                    "byte_count": staged.length,
                    "status": "staged",
                },
            )
            return staged
        except OSError as exc:
            self._remove_failed_stage(stage_directory)
            if _is_capacity_error(exc):
                raise EvidenceStoreError(
                    "cas_capacity_exhausted",
                    "CAS staging capacity was exhausted before a complete blob existed",
                ) from exc
            raise EvidenceStoreError(
                "exact_file_unavailable",
                "The exact source could not be streamed into CAS staging.",
            ) from exc
        except Exception:
            self._remove_failed_stage(stage_directory)
            raise

    def ingest_exact_file(self, source: Path | str, **kwargs: Any) -> InstalledBlob:
        """Install one exact sealed file without materializing it in memory."""

        return self.install(self.stage_exact_file(source, **kwargs))

    def _verify_exact(
        self, path: Path, expected_digest: str, expected_length: int
    ) -> None:
        digest = hashlib.sha256()
        length = 0
        try:
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    length += len(chunk)
        except OSError as exc:
            raise EvidenceStoreError(
                "blob_missing", f"unable to read CAS object: {exc}"
            ) from exc
        if length != expected_length or not hmac.compare_digest(
            digest.hexdigest(), expected_digest
        ):
            raise IntegrityFreeze(
                "same_digest_conflict",
                "existing digest path does not contain the exact expected bytes",
            )

    def _contains_secret_like_file(self, path: Path) -> bool:
        tail = b""
        try:
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        return False
                    window = tail + chunk
                    if _contains_secret_like(window):
                        return True
                    tail = window[-4096:]
        except OSError as exc:
            raise EvidenceStoreError(
                "blob_missing",
                f"unable to classify CAS object: {exc}",
            ) from exc

    def install(
        self,
        staged: StagedBlob,
        *,
        fault_hook: Callable[[str, StagedBlob, Path], None] | None = None,
    ) -> InstalledBlob:
        if type(staged) is not StagedBlob:
            raise EvidenceStoreError(
                "staged_blob_authority_invalid",
                "install requires an exact module-issued StagedBlob",
            )
        staged.verify_issued()
        stage_dir = _contained(self.staging_root, staged.stage_directory)
        payload = _contained(stage_dir, staged.payload_path)
        _ensure_no_symlink(payload, self.root)
        if not payload.is_file():
            raise EvidenceStoreError(
                "staged_blob_missing", "staged Evidence payload is absent"
            )
        self._verify_exact(payload, staged.sha256, staged.length)
        if (
            self._contains_secret_like_file(payload)
            and staged.quarantine_reason is None
        ):
            raise EvidenceStoreError(
                "secret_payload_requires_quarantine",
                "secret-like staged bytes require explicit quarantine at install",
            )
        destination = self.path_for_digest(staged.sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ensure_no_symlink(destination.parent, self.root)
        if destination.is_symlink():
            raise IntegrityFreeze(
                "same_digest_conflict",
                "digest path is a symlink rather than an immutable CAS object",
            )
        if fault_hook is not None:
            fault_hook("before_install", staged, destination)
            staged.verify_issued()
        deduplicated = False
        linked_from_stage = False
        try:
            os.link(payload, destination)
            linked_from_stage = True
            _fsync_directory(destination.parent)
        except FileExistsError:
            self._verify_exact(destination, staged.sha256, staged.length)
            deduplicated = True
        except OSError as exc:
            if _is_capacity_error(exc):
                # A failed install is not visible Evidence.  Retain the exact
                # staged payload for a deliberate retry, and remove any link
                # that may have been created before a directory fsync failed.
                if linked_from_stage and destination.exists():
                    try:
                        destination.unlink()
                        _fsync_directory(destination.parent)
                    except OSError as cleanup_exc:
                        raise IntegrityFreeze(
                            "cas_capacity_cleanup_failed",
                            "capacity exhaustion left an ambiguous CAS destination",
                        ) from cleanup_exc
                raise EvidenceStoreError(
                    "cas_capacity_exhausted",
                    "CAS install capacity was exhausted; the verified staged blob remains retryable",
                ) from exc
            raise EvidenceStoreError(
                "atomic_install_failed", f"no-overwrite CAS install failed: {exc}"
            ) from exc
        self._verify_exact(destination, staged.sha256, staged.length)
        # On Windows chmod applies to every hard-link name for the inode.  Drop
        # the staging name before enforcing CAS read-only custody, otherwise the
        # subsequent staging cleanup cannot unlink its own payload.
        if linked_from_stage:
            payload.unlink()
            _fsync_directory(stage_dir)
        _make_read_only(destination)
        if fault_hook is not None:
            fault_hook("after_install", staged, destination)
            staged.verify_issued()
        quarantine_state = "quarantined" if staged.quarantine_reason else "clear"
        record = BlobRecord(
            sha256=staged.sha256,
            length=staged.length,
            media_type=staged.media_type,
            encoding=staged.encoding,
            logical_path=cas_relative_path(staged.sha256),
            quarantine_state=quarantine_state,
            quarantine_reason=staged.quarantine_reason,
        )
        shutil.rmtree(stage_dir)
        _fsync_directory(self.staging_root)
        installed = InstalledBlob(
            record=record,
            path=destination,
            deduplicated=deduplicated,
        )
        self._observe_event(
            EventKind.CAS_INSTALLED,
            {
                "digest_sha256": record.sha256,
                "byte_count": record.length,
                "status": "deduplicated" if deduplicated else "installed",
            },
        )
        self._observe_event(
            EventKind.CAS_VERIFIED,
            {
                "digest_sha256": record.sha256,
                "byte_count": record.length,
                "status": "passed",
            },
        )
        if quarantine_state == "quarantined":
            self._observe_event(
                EventKind.CAS_QUARANTINED,
                {
                    "digest_sha256": record.sha256,
                    "byte_count": record.length,
                    "status": "quarantined",
                },
                level=ObservationLevel.WARN,
            )
        return installed

    def ingest_bytes(self, data: bytes, **kwargs: Any) -> InstalledBlob:
        return self.install(self.stage_bytes(data, **kwargs))

    def verify_record(self, record: BlobRecord, *, ordinary_use: bool = False) -> None:
        if ordinary_use:
            raise EvidenceStoreError(
                "verified_handle_required",
                "ordinary Evidence verification requires a store-derived read handle",
            )
        path = self.path_for_digest(record.sha256)
        if path.is_symlink():
            raise EvidenceStoreError(
                "blob_corrupt", "CAS object is a forbidden symlink"
            )
        try:
            self._verify_exact(path, record.sha256, record.length)
        except IntegrityFreeze as exc:
            raise EvidenceStoreError("blob_corrupt", str(exc)) from exc
        if record.quarantine_state == "clear" and self._contains_secret_like_file(path):
            raise EvidenceStoreError(
                "secret_payload_requires_quarantine",
                "secret-like CAS bytes cannot be treated as clear Evidence",
            )
        self._observe_event(
            EventKind.CAS_VERIFIED,
            {
                "digest_sha256": record.sha256,
                "byte_count": record.length,
                "status": "passed",
            },
        )

    def read_bytes(
        self,
        handle: VerifiedEvidenceReadHandle,
        *,
        store: "WorkspaceStore",
        authorization: Mapping[str, Any] | None = None,
    ) -> bytes:
        """Read after issuer and current store authority are both reverified."""

        from .workspace_store import WorkspaceStore

        if type(handle) is not VerifiedEvidenceReadHandle:
            raise EvidenceStoreError(
                "verified_handle_required",
                "ordinary Evidence reads require VerifiedEvidenceReadHandle",
            )
        if type(store) is not WorkspaceStore:
            raise EvidenceStoreError(
                "workspace_store_required",
                "ordinary Evidence reads require the owning WorkspaceStore",
            )
        record = store.authorize_evidence_read(
            handle,
            cas=self,
            authorization=authorization,
        )
        path = self.path_for_digest(record.sha256)
        if path.is_symlink():
            raise EvidenceStoreError(
                "blob_corrupt", "CAS object is a forbidden symlink"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise EvidenceStoreError(
                "blob_missing", f"unable to read CAS object: {exc}"
            ) from exc
        if len(raw) != record.length or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(),
            record.sha256,
        ):
            raise EvidenceStoreError(
                "blob_corrupt",
                "CAS object changed after store authorization",
            )
        return raw

    def _read_verified_record_bytes(self, record: BlobRecord) -> bytes:
        """Return one full physical byte snapshot, not ordinary-read authority."""
        if type(record) is not BlobRecord:
            raise TypeError("physical CAS reads require one exact BlobRecord")
        return self._read_verified_record_range(
            record, offset_bytes=0, max_bytes=record.length,
        )

    def _read_verified_record_range(
        self, record: BlobRecord, *, offset_bytes: int, max_bytes: int,
    ) -> bytes:
        """Verify one full physical byte snapshot and return its selected range.

        Hashing, secret classification and returned bytes share one descriptor.
        Every call reads and materializes the whole Blob. The canonical whole-
        buffer classifier recognizes unbounded whitespace and NUL-stripped
        patterns; a fixed streaming overlap cannot preserve that behavior.
        No bytes escape before integrity and classification succeed. Only the
        Store's authorized readers consume this private physical primitive;
        it conveys no authorization or reusable verification capability.
        """
        if type(record) is not BlobRecord:
            raise TypeError("physical CAS reads require one exact BlobRecord")
        if (type(offset_bytes) is not int or offset_bytes < 0
            or type(max_bytes) is not int or max_bytes < 0):
            raise ValueError("physical CAS byte range must be nonnegative")
        path = self.root / cas_relative_path(record.sha256)

        def boundary() -> os.stat_result:
            try:
                # Reuse path/link mechanics, not the ingress owner's single-
                # link requirement or receiving-workspace exclusion.
                self._require_exact_source_boundaries(path)
                details = os.lstat(path)
            except EvidenceStoreError as exc:
                code = "blob_missing" if exc.code == "exact_file_unavailable" else "blob_corrupt"
                raise EvidenceStoreError(code, "CAS object path is unavailable or unsafe.") from exc
            except OSError as exc:
                raise EvidenceStoreError("blob_missing", "CAS object is unavailable.") from exc
            if not stat.S_ISREG(details.st_mode):
                raise EvidenceStoreError("blob_corrupt", "CAS object is not a regular file.")
            return details

        def unchanged(first: os.stat_result, second: os.stat_result) -> bool:
            return self._same_source_identity(first, second) and first.st_ctime_ns == second.st_ctime_ns

        before = boundary()
        if self.path_for_digest(record.sha256) != path:
            raise EvidenceStoreError("blob_corrupt", "CAS object path changed its physical binding.")
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or not unchanged(before, opened):
                    raise EvidenceStoreError("blob_corrupt", "CAS object changed while being opened.")
                if opened.st_size != record.length:
                    raise EvidenceStoreError("blob_corrupt", "CAS object differs from its committed bytes.")
                # Retain one immutable full buffer, not a bytearray plus a
                # second whole-Blob copy. The extra byte detects concurrent
                # growth without following an unbounded tail.
                raw = stream.read(record.length + 1)
                opened_after = os.fstat(stream.fileno())
        except OSError as exc:
            code = "blob_corrupt" if exc.errno == errno.ELOOP else "blob_missing"
            raise EvidenceStoreError(code, "CAS object could not be read safely.") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        after = boundary()
        if not unchanged(opened, opened_after) or not unchanged(opened_after, after):
            raise EvidenceStoreError("blob_corrupt", "CAS object changed while its bytes were read.")
        # Complete integrity has precedence over classification, including a
        # secret in corrupt bytes outside the selected range.
        if len(raw) != record.length or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), record.sha256):
            raise EvidenceStoreError("blob_corrupt", "CAS object differs from its committed bytes.")
        if record.quarantine_state == "clear" and _contains_secret_like(raw):
            raise EvidenceStoreError(
                "secret_payload_requires_quarantine",
                "secret-like CAS bytes cannot be treated as clear Evidence",
            )
        self._observe_event(EventKind.CAS_VERIFIED, {
            "digest_sha256": record.sha256, "byte_count": record.length, "status": "passed",
        })
        if offset_bytes == 0 and max_bytes >= record.length:
            return raw
        return raw[offset_bytes:offset_bytes + max_bytes]

    def scrub(self, records: Iterable[BlobRecord]) -> tuple[dict[str, Any], ...]:
        results: list[dict[str, Any]] = []
        for record in records:
            try:
                self.verify_record(record)
                status = (
                    "quarantined"
                    if record.quarantine_state == "quarantined"
                    else "verified"
                )
                detail = None
            except EvidenceStoreError as exc:
                status = "failed"
                detail = exc.code
            results.append(
                {"sha256": record.sha256, "status": status, "detail": detail}
            )
        self._observe_event(
            EventKind.CAS_SCRUBBED,
            {
                "count": len(results),
                "status": (
                    "failed"
                    if any(item["status"] == "failed" for item in results)
                    else "passed"
                ),
            },
            level=(
                ObservationLevel.WARN
                if any(item["status"] == "failed" for item in results)
                else ObservationLevel.INFO
            ),
        )
        return tuple(results)

    def discover_orphans(self, referenced_digests: Iterable[str]) -> tuple[str, ...]:
        referenced = {_validate_digest(item) for item in referenced_digests}
        if not self.cas_root.exists():
            return ()
        discovered: set[str] = set()
        for prefix in self.cas_root.iterdir():
            if (
                prefix.is_symlink()
                or not prefix.is_dir()
                or not re.fullmatch(r"[0-9a-f]{2}", prefix.name)
            ):
                continue
            for item in prefix.iterdir():
                digest = prefix.name + item.name
                if (
                    item.is_file()
                    and not item.is_symlink()
                    and SHA256_RE.fullmatch(digest)
                ):
                    discovered.add(digest)
        return tuple(sorted(discovered - referenced))

    def inspect_unreferenced_object(self, digest: str) -> int:
        """Verify one physical object and return its length without authorizing use."""

        path = self.path_for_digest(digest)
        if path.is_symlink() or not path.is_file():
            raise EvidenceStoreError(
                "blob_missing", "orphan CAS object is absent or unsafe"
            )
        length = path.stat().st_size
        self._verify_exact(path, digest, length)
        return length

    def discover_staging(self) -> tuple[Path, ...]:
        if not self.staging_root.exists():
            return ()
        return tuple(
            sorted(
                (
                    item
                    for item in self.staging_root.iterdir()
                    if item.is_dir() and not item.is_symlink()
                ),
                key=lambda item: item.name,
            )
        )

    def remove_unreferenced_staging(self, stage_directory: Path | str) -> None:
        candidate = _contained(self.staging_root, Path(stage_directory))
        if candidate.parent != self.staging_root or not candidate.name.startswith(
            "ingest-"
        ):
            raise EvidenceStoreError(
                "unsafe_staging_cleanup",
                "only one discovered ingestion directory may be removed",
            )
        if candidate.is_symlink():
            raise EvidenceStoreError(
                "symlink_forbidden", "staging symlink removal is forbidden"
            )
        shutil.rmtree(candidate)


def require_evidence_available(
    evidence: EvidenceItemRevision,
    handle: VerifiedEvidenceReadHandle,
    cas: EvidenceCAS,
    *,
    store: "WorkspaceStore",
    authorization: Mapping[str, Any],
) -> None:
    if evidence.availability_state != "verified_available":
        raise EvidenceStoreError(
            "evidence_unavailable", "Evidence revision is not verified available"
        )
    if (handle.evidence_id, handle.evidence_revision) != (
        evidence.evidence_id,
        evidence.revision,
    ):
        raise EvidenceStoreError(
            "evidence_handle_mismatch", "read handle names another Evidence revision"
        )
    role_map = dict(evidence.blob_roles)
    if role_map.get(handle.role) != handle.blob_sha256:
        raise EvidenceStoreError(
            "evidence_handle_mismatch", "read handle names another blob role"
        )
    cas.read_bytes(handle, store=store, authorization=authorization)


def file_mode_is_private(path: Path | str) -> bool:
    """Best-effort Unix permission readback used by security tests/operators."""

    mode = stat.S_IMODE(Path(path).stat().st_mode)
    return os.name == "nt" or mode & 0o077 == 0


def file_is_owner_read_only(path: Path | str) -> bool:
    """Return whether the CAS object lacks the owner-write bit."""

    return stat.S_IMODE(Path(path).stat().st_mode) & stat.S_IWUSR == 0
