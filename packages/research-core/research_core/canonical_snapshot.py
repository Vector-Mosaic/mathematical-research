"""Exact-byte, validated, read-only canonical Research State snapshots."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from .json_support import (
    DuplicateKeyError,
    NonFiniteJSONError,
    canonical_json_bytes,
    loads_strict_json_bytes,
)
from .research_model import (
    AuthorityVector,
    CanonicalStateSnapshot,
    OperationFailure,
    OperationResult,
    SourceClass,
    deep_thaw,
)
from .validator import Finding, ValidationResult, validate_state


CANONICAL_STATE_REPO_PATH = (
    "projects/riemann_hypothesis/research_state.json"
)
_FULL_GIT_SHA = re.compile(r"[0-9a-f]{40}")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _coerce_source_class(value: SourceClass | str) -> SourceClass:
    return value if isinstance(value, SourceClass) else SourceClass(value)


def _failure(
    code: str,
    location: str,
    message: str,
    *,
    findings: tuple[Finding, ...] = (),
    metrics: Mapping[str, Any] | None = None,
) -> OperationResult[Any]:
    attached = findings or (Finding("ERROR", location, message),)
    return OperationResult(
        value=None,
        findings=attached,
        metrics=metrics or {},
        failure=OperationFailure(code, location, message),
    )


def _repo_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _resolve(root: Path, value: Path | str | None) -> Path:
    raw = Path(CANONICAL_STATE_REPO_PATH) if value is None else Path(value)
    return (root / raw).resolve() if not raw.is_absolute() else raw.resolve()


def _parse_validate_state(
    raw: bytes, location: str
) -> tuple[
    Mapping[str, Any] | None, ValidationResult | None, OperationResult[Any] | None
]:
    try:
        value = loads_strict_json_bytes(raw)
    except DuplicateKeyError as exc:
        return None, None, _failure("duplicate_json_key", location, str(exc))
    except (UnicodeDecodeError, NonFiniteJSONError, ValueError) as exc:
        return (
            None,
            None,
            _failure(
                "canonical_state_schema_invalid",
                location,
                f"unable to parse state: {exc}",
            ),
        )
    if not isinstance(value, Mapping):
        return (
            None,
            None,
            _failure(
                "canonical_state_schema_invalid",
                location,
                "top-level state must be an object",
            ),
        )
    validation = validate_state(value)
    if not validation.ok:
        code = (
            "canonical_state_schema_invalid"
            if any(
                "Research State schema:" in item.message for item in validation.errors
            )
            else "canonical_state_invalid"
        )
        return (
            None,
            validation,
            _failure(
                code,
                location,
                "canonical state failed structural or semantic validation",
                findings=validation.findings,
                metrics=validation.counts,
            ),
        )
    return value, validation, None


def normalized_state_digest(state: Mapping[str, Any]) -> str:
    """Hash the deterministic JSON meaning of an immutable state tree."""

    return _sha256(canonical_json_bytes(deep_thaw(state)))


def verify_snapshot_integrity(snapshot: CanonicalStateSnapshot) -> bool:
    """Confirm that an immutable snapshot still has its construction-time meaning."""

    return (
        type(snapshot) is CanonicalStateSnapshot
        and snapshot.raw_sha256 == snapshot.authority_vector.canonical_state_sha256
        and normalized_state_digest(snapshot.state) == snapshot.normalized_digest
    )


def verify_snapshot_current(
    snapshot: CanonicalStateSnapshot,
) -> OperationFailure | None:
    """Re-read and revalidate the exact live state bytes bound by a v2 snapshot."""

    if type(snapshot) is not CanonicalStateSnapshot:
        return OperationFailure(
            "canonical_source_not_authoritative",
            "snapshot",
            "operation requires an exact CanonicalStateSnapshot",
        )
    if not verify_snapshot_integrity(snapshot):
        return OperationFailure(
            "canonical_snapshot_mutated",
            str(snapshot.authorized_path),
            "snapshot meaning no longer matches its captured digest",
        )
    vector = snapshot.authority_vector
    if vector.source_class is not SourceClass.LIVE_CANONICAL:
        return None
    root = snapshot.repo_root
    if root is None:
        return OperationFailure(
            "canonical_source_not_authoritative",
            str(snapshot.authorized_path),
            "live snapshot has no repository root",
        )
    expected_path = (root / CANONICAL_STATE_REPO_PATH).resolve()
    if (
        vector.binding_version != 2
        or vector.canonical_state_path != CANONICAL_STATE_REPO_PATH
        or snapshot.authorized_path != expected_path
        or vector.source_commit is None
        or _FULL_GIT_SHA.fullmatch(vector.source_commit) is None
    ):
        return OperationFailure(
            "canonical_source_not_authoritative",
            str(snapshot.authorized_path),
            "live snapshot lacks the exact v2 canonical path and release binding",
        )
    try:
        raw = expected_path.read_bytes()
    except OSError as exc:
        return OperationFailure(
            "authority_surface_changed", str(expected_path), str(exc)
        )
    if _sha256(raw) != vector.canonical_state_sha256:
        return OperationFailure(
            "authority_surface_changed",
            str(expected_path),
            "canonical Research State bytes differ from the snapshot binding",
        )
    disk_state, validation, failed = _parse_validate_state(raw, str(expected_path))
    if failed is not None:
        return failed.failure
    assert disk_state is not None and validation is not None
    schema_version = disk_state.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != snapshot.schema_version
        or normalized_state_digest(disk_state) != snapshot.normalized_digest
        or deep_thaw(snapshot.state) != disk_state
        or validation != snapshot.validation
    ):
        return OperationFailure(
            "canonical_snapshot_mutated",
            str(expected_path),
            "snapshot meaning or validation differs from the bound canonical bytes",
        )
    return None


def load_canonical_snapshot(
    repo_root: Path | str,
    *,
    state_path: Path | str | None = None,
    source_class: SourceClass | str = SourceClass.LIVE_CANONICAL,
    source_commit: str | None = None,
) -> OperationResult[CanonicalStateSnapshot]:
    """Load one validated state; live authority requires an exact release SHA."""

    root = Path(repo_root).resolve()
    try:
        source = _coerce_source_class(source_class)
    except ValueError as exc:
        return _failure("canonical_source_not_authoritative", "source_class", str(exc))
    resolved_state = _resolve(root, state_path)
    expected_state = (root / CANONICAL_STATE_REPO_PATH).resolve()
    if source is SourceClass.LIVE_CANONICAL:
        if resolved_state != expected_state:
            return _failure(
                "canonical_source_not_authoritative",
                str(resolved_state),
                f"live_canonical source must resolve to {CANONICAL_STATE_REPO_PATH}",
            )
        if (
            type(source_commit) is not str
            or _FULL_GIT_SHA.fullmatch(source_commit) is None
        ):
            return _failure(
                "canonical_source_not_authoritative",
                "source_commit",
                "live canonical authority requires one full lowercase release SHA",
            )
    elif source_commit is not None and (
        type(source_commit) is not str or _FULL_GIT_SHA.fullmatch(source_commit) is None
    ):
        return _failure(
            "canonical_source_not_authoritative",
            "source_commit",
            "source_commit must be one full lowercase release SHA when supplied",
        )
    try:
        raw = resolved_state.read_bytes()
    except OSError as exc:
        return _failure("canonical_state_invalid", str(resolved_state), str(exc))
    state, validation, failed = _parse_validate_state(raw, str(resolved_state))
    if failed is not None or state is None or validation is None:
        return failed
    schema_version = state.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        return _failure(
            "canonical_state_schema_invalid",
            "schema_version",
            "canonical schema version must be an integer",
        )
    raw_sha256 = _sha256(raw)
    authority = AuthorityVector(
        source_class=source,
        canonical_state_path=_repo_path(root, resolved_state),
        canonical_state_sha256=raw_sha256,
        source_commit=source_commit,
    )
    snapshot = CanonicalStateSnapshot(
        authority_vector=authority,
        authorized_path=resolved_state,
        raw_sha256=raw_sha256,
        schema_version=schema_version,
        state=state,
        validation=validation,
        normalized_digest=normalized_state_digest(state),
        repo_root=root,
    )
    if source is SourceClass.LIVE_CANONICAL:
        current = verify_snapshot_current(snapshot)
        if current is not None:
            return _failure(current.code, current.location, current.message)
    return OperationResult(
        value=snapshot,
        findings=validation.findings,
        authority_vector=authority,
        metrics=validation.counts,
    )
