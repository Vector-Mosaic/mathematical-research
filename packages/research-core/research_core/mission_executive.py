"""Direct Executive Epoch authority and continuation for RH research.

The coordinator authors these proof-neutral facts. Deterministic code validates
exact owner revisions, transitive dependency closure, causal and unresolved
pointers, capture custody, lifecycle ordering, and content identity. Store owns
persistence and physical ordering; this module owns no aggregate coordinator,
research budget, retry quota, acceptance ceremony, or mathematical judgment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .json_delta import resolve_json_pointer
from .json_support import canonical_json_bytes
from .research_model import deep_freeze, deep_thaw


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


# Successor direct-checkpoint domain. Store integration selects current roots,
# persists the cut, and validates predecessor and capture custody.

LEGACY_DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION = 1
DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION = 2
_DIRECT_OWNER_KINDS = frozenset(
    {"mission", "strategy", "branch", "context", "candidate", "evidence"}
)
_DIRECT_REF_KEYS = {"kind", "identity", "revision", "payload_sha256"}
_DIRECT_CHECKPOINT_V1_KEYS = {
    "schema_version",
    "kind",
    "checkpoint_id",
    "mission_root",
    "strategy_root",
    "transitive_owner_refs",
    "causal_pointers",
    "unresolved_pointers",
    "pending_capture_locators",
    "predecessor_checkpoint",
    "authoring_epoch_id",
    "project_commit",
}
_DIRECT_CHECKPOINT_V2_KEYS = {
    "schema_version",
    "kind",
    "checkpoint_id",
    "mission_root",
    "strategy_root",
    "unresolved_pointers",
    "predecessor_checkpoint",
    "authoring_epoch_id",
    "project_commit",
}


class DirectCheckpointError(ValueError):
    def __init__(self, code: str, location: str, message: str) -> None:
        self.code = code
        self.location = location
        super().__init__(f"{code} at {location}: {message}")


def _direct_sequence(value: Any, location: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{location} must be an array")
    return tuple(value)


def _direct_closed(
    value: Any,
    keys: set[str],
    location: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{location} has the wrong closed shape")
    return value


@dataclass(frozen=True, slots=True, order=True)
class OwnerRevisionRef:
    """One exact immutable direct-owner revision."""

    kind: str
    identity: str
    revision: int
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.kind not in _DIRECT_OWNER_KINDS:
            raise DirectCheckpointError(
                "checkpoint_owner_kind_unknown",
                "owner_ref.kind",
                f"unsupported owner kind {self.kind!r}",
            )
        try:
            _require_text(self.identity, "owner_ref.identity")
            _require_positive_int(self.revision, "owner_ref.revision")
            _require_digest(self.payload_sha256, "owner_ref.payload_sha256")
        except ValueError as exc:
            raise DirectCheckpointError(
                "checkpoint_owner_ref_malformed",
                "owner_ref",
                str(exc),
            ) from exc

    @property
    def revision_identity(self) -> tuple[str, str, int]:
        return self.kind, self.identity, self.revision

    def to_mapping(self) -> Mapping[str, Any]:
        return deep_freeze(
            {
                "kind": self.kind,
                "identity": self.identity,
                "revision": self.revision,
                "payload_sha256": self.payload_sha256,
            }
        )


def owner_revision_ref_from_mapping(value: Mapping[str, Any]) -> OwnerRevisionRef:
    try:
        item = _direct_closed(value, _DIRECT_REF_KEYS, "owner_ref")
        return OwnerRevisionRef(
            item["kind"],
            item["identity"],
            item["revision"],
            item["payload_sha256"],
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, DirectCheckpointError):
            raise
        raise DirectCheckpointError(
            "checkpoint_owner_ref_malformed",
            "owner_ref",
            str(exc),
        ) from exc


def _owner_ref(value: Any, location: str) -> OwnerRevisionRef:
    if type(value) is OwnerRevisionRef:
        return value
    try:
        return owner_revision_ref_from_mapping(value)
    except (TypeError, ValueError) as exc:
        code = (
            exc.code
            if isinstance(exc, DirectCheckpointError)
            else "checkpoint_owner_ref_malformed"
        )
        raise DirectCheckpointError(code, location, str(exc)) from exc


def _canonical_refs(
    values: Sequence[Any],
    location: str,
) -> tuple[OwnerRevisionRef, ...]:
    unique: dict[tuple[str, str, int], OwnerRevisionRef] = {}
    for index, value in enumerate(_direct_sequence(values, location)):
        reference = _owner_ref(value, f"{location}[{index}]")
        prior = unique.get(reference.revision_identity)
        if prior is not None and prior.payload_sha256 != reference.payload_sha256:
            raise DirectCheckpointError(
                "checkpoint_reference_digest_conflict",
                location,
                f"{reference.revision_identity} has two expected digests",
            )
        unique[reference.revision_identity] = reference
    return tuple(sorted(unique.values()))


@dataclass(frozen=True, slots=True)
class ResolvedOwnerRevision:
    """Canonical owner-reader result; the pure cut does not parse owner schemas."""

    reference: OwnerRevisionRef
    mission_id: str
    validated_document: Mapping[str, Any]
    outgoing_refs: tuple[OwnerRevisionRef, ...] = ()
    is_current_head: bool = False

    def __post_init__(self) -> None:
        if type(self.reference) is not OwnerRevisionRef:
            raise TypeError("resolved owner requires an exact OwnerRevisionRef")
        mission_id = _require_text(self.mission_id, "resolved owner mission_id")
        if not isinstance(self.validated_document, Mapping):
            raise TypeError("resolved owner validated_document must be an object")
        document = deep_freeze(self.validated_document)
        if _sha256(document) != self.reference.payload_sha256:
            raise DirectCheckpointError(
                "checkpoint_reference_digest_mismatch",
                str(self.reference.revision_identity),
                "validated owner document does not match its reference",
            )
        if any(type(item) is not OwnerRevisionRef for item in self.outgoing_refs):
            raise TypeError("resolved outgoing refs require OwnerRevisionRef values")
        if not isinstance(self.is_current_head, bool):
            raise TypeError("resolved current-head fact must be boolean")
        object.__setattr__(self, "mission_id", mission_id)
        object.__setattr__(self, "validated_document", document)
        object.__setattr__(
            self,
            "outgoing_refs",
            _canonical_refs(self.outgoing_refs, "resolved outgoing_refs"),
        )


def _owner_closure(
    roots: Sequence[OwnerRevisionRef],
    resolver: Any,
    *,
    mission_id: str,
    require_current_roots: bool = True,
) -> tuple[
    tuple[OwnerRevisionRef, ...],
    Mapping[tuple[str, str, int], ResolvedOwnerRevision],
]:
    if not callable(resolver):
        raise TypeError("direct checkpoint resolver must be callable")
    pending = list(_canonical_refs(roots, "checkpoint roots"))
    root_keys = {item.revision_identity for item in pending}
    expected = {item.revision_identity: item for item in pending}
    resolved_rows: dict[tuple[str, str, int], ResolvedOwnerRevision] = {}
    cursor = 0
    while cursor < len(pending):
        requested = pending[cursor]
        cursor += 1
        key = requested.revision_identity
        if key in resolved_rows:
            continue
        location = f"{requested.kind}:{requested.identity}@{requested.revision}"
        try:
            resolved = resolver(requested)
        except LookupError as exc:
            raise DirectCheckpointError(
                "checkpoint_reference_missing",
                location,
                "resolver has no exact owner revision",
            ) from exc
        if type(resolved) is not ResolvedOwnerRevision:
            raise DirectCheckpointError(
                "checkpoint_resolver_malformed",
                location,
                "resolver must return ResolvedOwnerRevision",
            )
        if resolved.reference.revision_identity != key:
            raise DirectCheckpointError(
                "checkpoint_resolver_malformed",
                location,
                "resolver returned another owner revision",
            )
        if resolved.reference.payload_sha256 != requested.payload_sha256:
            raise DirectCheckpointError(
                "checkpoint_reference_digest_mismatch",
                location,
                "resolver returned another digest",
            )
        if resolved.mission_id != mission_id:
            raise DirectCheckpointError(
                "checkpoint_reference_foreign_mission",
                location,
                f"does not belong to Mission {mission_id}",
            )
        if require_current_roots and key in root_keys and not resolved.is_current_head:
            raise DirectCheckpointError(
                "checkpoint_root_historical",
                location,
                "checkpoint roots must be current owner heads",
            )
        resolved_rows[key] = resolved
        for child in resolved.outgoing_refs:
            prior = expected.get(child.revision_identity)
            if prior is not None and prior.payload_sha256 != child.payload_sha256:
                raise DirectCheckpointError(
                    "checkpoint_reference_digest_conflict",
                    str(child.revision_identity),
                    "owner graph has two expected digests",
                )
            if prior is None:
                expected[child.revision_identity] = child
                pending.append(child)
    return tuple(sorted(expected.values())), resolved_rows


def _checkpoint_pointers(
    values: Sequence[Mapping[str, Any]],
    location: str,
) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    for index, value in enumerate(_direct_sequence(values, location)):
        item = _direct_closed(
            value,
            {"owner_ref", "json_pointer"},
            f"{location}[{index}]",
        )
        pointer = item["json_pointer"]
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            raise DirectCheckpointError(
                "checkpoint_json_pointer_invalid",
                f"{location}[{index}]",
                "pointer must be empty or start with /",
            )
        result.append(
            deep_freeze(
                {
                    "owner_ref": _owner_ref(
                        item["owner_ref"],
                        f"{location}[{index}].owner_ref",
                    ).to_mapping(),
                    "json_pointer": pointer,
                }
            )
        )
    encoded = [canonical_json_bytes(item) for item in result]
    if len(encoded) != len(set(encoded)):
        raise ValueError(f"{location} contains duplicate pointers")
    return tuple(item for _, item in sorted(zip(encoded, result)))


def _pending_captures(
    values: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    result: dict[tuple[str, int], Mapping[str, Any]] = {}
    for index, value in enumerate(_direct_sequence(values, "pending_capture_locators")):
        item = _direct_closed(
            value,
            {"capture_id", "artifact_ordinal"},
            f"pending_capture_locators[{index}]",
        )
        locator = (
            _require_text(item["capture_id"], "capture_id"),
            _require_nonnegative_int(item["artifact_ordinal"], "artifact_ordinal"),
        )
        if locator in result:
            raise ValueError("pending_capture_locators contains duplicates")
        result[locator] = deep_freeze(
            {"capture_id": locator[0], "artifact_ordinal": locator[1]}
        )
    return tuple(result[key] for key in sorted(result))


def _predecessor(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    item = _direct_closed(
        value,
        {"checkpoint_id", "payload_sha256"},
        "predecessor_checkpoint",
    )
    return deep_freeze(
        {
            "checkpoint_id": _require_text(item["checkpoint_id"], "checkpoint_id"),
            "payload_sha256": _require_digest(
                item["payload_sha256"],
                "payload_sha256",
            ),
        }
    )


def _checkpoint_id(mission_id: str, epoch_id: str) -> str:
    return f"checkpoint:{_sha256({'mission_id': mission_id, 'epoch_id': epoch_id})}"


def _normalize_direct_continuation_checkpoint(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("direct checkpoint must be an object")
    schema_version = value.get("schema_version")
    if schema_version == DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION:
        document = _direct_closed(
            value,
            _DIRECT_CHECKPOINT_V2_KEYS,
            "direct checkpoint",
        )
        if document["kind"] != "direct_continuation_checkpoint":
            raise ValueError("direct checkpoint identity is invalid")
        mission = _owner_ref(document["mission_root"], "mission_root")
        strategy = _owner_ref(document["strategy_root"], "strategy_root")
        if (mission.kind, strategy.kind) != ("mission", "strategy"):
            raise DirectCheckpointError(
                "checkpoint_root_kind_invalid",
                "checkpoint roots",
                "require Mission and Strategy owner revisions",
            )
        epoch = _require_text(document["authoring_epoch_id"], "authoring_epoch_id")
        checkpoint_id = _require_text(document["checkpoint_id"], "checkpoint_id")
        if checkpoint_id != _checkpoint_id(mission.identity, epoch):
            raise ValueError("direct checkpoint identifier is stale")
        return deep_freeze(
            {
                "schema_version": DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION,
                "kind": "direct_continuation_checkpoint",
                "checkpoint_id": checkpoint_id,
                "mission_root": mission.to_mapping(),
                "strategy_root": strategy.to_mapping(),
                "unresolved_pointers": _checkpoint_pointers(
                    document["unresolved_pointers"],
                    "unresolved_pointers",
                ),
                "predecessor_checkpoint": _predecessor(
                    document["predecessor_checkpoint"]
                ),
                "authoring_epoch_id": epoch,
                "project_commit": _require_nonnegative_int(
                    document["project_commit"],
                    "project_commit",
                ),
            }
        )
    if schema_version != LEGACY_DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("direct checkpoint identity is invalid")
    document = _direct_closed(
        value,
        _DIRECT_CHECKPOINT_V1_KEYS,
        "direct checkpoint",
    )
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"]
        != LEGACY_DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION
        or document["kind"] != "direct_continuation_checkpoint"
    ):
        raise ValueError("direct checkpoint identity is invalid")

    mission = _owner_ref(document["mission_root"], "mission_root")
    strategy = _owner_ref(document["strategy_root"], "strategy_root")
    if (mission.kind, strategy.kind) != ("mission", "strategy"):
        raise DirectCheckpointError(
            "checkpoint_root_kind_invalid",
            "checkpoint roots",
            "require Mission and Strategy owner revisions",
        )

    transitive_values = _direct_sequence(
        document["transitive_owner_refs"],
        "transitive_owner_refs",
    )
    transitive = _canonical_refs(
        transitive_values,
        "transitive_owner_refs",
    )
    if len(transitive) != len(transitive_values):
        raise ValueError("transitive_owner_refs contains duplicate revisions")
    roots = {
        mission.revision_identity: mission,
        strategy.revision_identity: strategy,
    }
    included = dict(roots)
    for reference in transitive:
        root = roots.get(reference.revision_identity)
        if root is not None:
            if root.payload_sha256 != reference.payload_sha256:
                raise DirectCheckpointError(
                    "checkpoint_reference_digest_conflict",
                    "transitive_owner_refs",
                    f"{reference.revision_identity} has two expected digests",
                )
            raise ValueError("transitive_owner_refs repeats a checkpoint root")
        included[reference.revision_identity] = reference

    causal = _checkpoint_pointers(
        document["causal_pointers"],
        "causal_pointers",
    )
    unresolved = _checkpoint_pointers(
        document["unresolved_pointers"],
        "unresolved_pointers",
    )
    for location, pointers in (
        ("causal_pointers", causal),
        ("unresolved_pointers", unresolved),
    ):
        for index, pointer in enumerate(pointers):
            reference = owner_revision_ref_from_mapping(pointer["owner_ref"])
            if included.get(reference.revision_identity) != reference:
                raise DirectCheckpointError(
                    "checkpoint_pointer_outside_closure",
                    f"{location}[{index}]",
                    "pointer owner is not present in the declared owner cut",
                )

    epoch = _require_text(document["authoring_epoch_id"], "authoring_epoch_id")
    checkpoint_id = _require_text(document["checkpoint_id"], "checkpoint_id")
    if checkpoint_id != _checkpoint_id(mission.identity, epoch):
        raise ValueError("direct checkpoint identifier is stale")
    return deep_freeze(
        {
            "schema_version": LEGACY_DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION,
            "kind": "direct_continuation_checkpoint",
            "checkpoint_id": checkpoint_id,
            "mission_root": mission.to_mapping(),
            "strategy_root": strategy.to_mapping(),
            "transitive_owner_refs": tuple(
                reference.to_mapping() for reference in transitive
            ),
            "causal_pointers": causal,
            "unresolved_pointers": unresolved,
            "pending_capture_locators": _pending_captures(
                document["pending_capture_locators"]
            ),
            "predecessor_checkpoint": _predecessor(document["predecessor_checkpoint"]),
            "authoring_epoch_id": epoch,
            "project_commit": _require_nonnegative_int(
                document["project_commit"],
                "project_commit",
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class DirectContinuationCheckpoint:
    """Immutable digest-checked proof-neutral owner cut."""

    document: Mapping[str, Any]
    digest_sha256: str

    def __post_init__(self) -> None:
        document = _normalize_direct_continuation_checkpoint(self.document)
        if self.digest_sha256 != _sha256(document):
            raise ValueError("direct checkpoint digest is stale")
        object.__setattr__(self, "document", document)

    def to_reference(self) -> Mapping[str, Any]:
        self.verify_integrity()
        return deep_freeze(
            {
                "checkpoint_id": self.document["checkpoint_id"],
                "payload_sha256": self.digest_sha256,
            }
        )

    def verify_integrity(self) -> None:
        if type(self) is not DirectContinuationCheckpoint:
            raise TypeError("direct checkpoint subclasses are not exact values")
        self.__post_init__()


def prepare_direct_continuation_checkpoint(
    *,
    mission_root: OwnerRevisionRef | Mapping[str, Any],
    strategy_root: OwnerRevisionRef | Mapping[str, Any],
    resolver: Any = None,
    causal_pointers: Sequence[Mapping[str, Any]] = (),
    unresolved_pointers: Sequence[Mapping[str, Any]] = (),
    pending_capture_locators: Sequence[Mapping[str, Any]] = (),
    predecessor_checkpoint: Mapping[str, Any] | None,
    authoring_epoch_id: str,
    project_commit: int,
    schema_version: int = DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION,
) -> DirectContinuationCheckpoint:
    """Build one versioned current Mission + Strategy continuation cut.

    Version 2 stores only the exact roots, current unresolved attention, and
    factual Epoch/cut links.  Its transitive closure, Strategy causal pointers,
    and pending Capture set are deterministic views materialized only by an
    explicit history/reconstruction read.  Version 1 remains constructible for
    immutable legacy fixtures and compatibility qualification.
    """

    mission = _owner_ref(mission_root, "mission_root")
    strategy = _owner_ref(strategy_root, "strategy_root")
    if (mission.kind, strategy.kind) != ("mission", "strategy"):
        raise DirectCheckpointError(
            "checkpoint_root_kind_invalid",
            "checkpoint roots",
            "require Mission and Strategy owner revisions",
        )
    unresolved = _checkpoint_pointers(
        unresolved_pointers,
        "unresolved_pointers",
    )
    epoch = _require_text(authoring_epoch_id, "authoring_epoch_id")
    common = {
        "kind": "direct_continuation_checkpoint",
        "checkpoint_id": _checkpoint_id(mission.identity, epoch),
        "mission_root": mission.to_mapping(),
        "strategy_root": strategy.to_mapping(),
        "unresolved_pointers": unresolved,
        "predecessor_checkpoint": _predecessor(predecessor_checkpoint),
        "authoring_epoch_id": epoch,
        "project_commit": _require_nonnegative_int(
            project_commit,
            "project_commit",
        ),
    }
    if schema_version == DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION:
        if resolver is not None or causal_pointers or pending_capture_locators:
            raise ValueError(
                "compact checkpoint derives closure, causal pointers, and pending "
                "Capture custody only during explicit reconstruction"
            )
        document = deep_freeze(
            {
                "schema_version": DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION,
                **common,
            }
        )
        return DirectContinuationCheckpoint(document, _sha256(document))
    if schema_version != LEGACY_DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("unsupported direct checkpoint schema version")
    if resolver is None:
        raise ValueError("legacy checkpoint construction requires an owner resolver")
    closure, resolved = _owner_closure(
        (mission, strategy),
        resolver,
        mission_id=mission.identity,
    )
    causal = _checkpoint_pointers(causal_pointers, "causal_pointers")
    included = {item.revision_identity: item for item in closure}
    for location, pointers in (
        ("causal_pointers", causal),
        ("unresolved_pointers", unresolved),
    ):
        for index, pointer in enumerate(pointers):
            reference = owner_revision_ref_from_mapping(pointer["owner_ref"])
            expected = included.get(reference.revision_identity)
            if expected != reference:
                raise DirectCheckpointError(
                    "checkpoint_pointer_outside_closure",
                    f"{location}[{index}]",
                    "pointer owner is not reachable from the two roots",
                )
            try:
                resolve_json_pointer(
                    resolved[reference.revision_identity].validated_document,
                    pointer["json_pointer"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DirectCheckpointError(
                    "checkpoint_json_pointer_invalid",
                    f"{location}[{index}]",
                    str(exc),
                ) from exc

    root_keys = {mission.revision_identity, strategy.revision_identity}
    transitive = [
        item.to_mapping() for item in closure if item.revision_identity not in root_keys
    ]
    document = deep_freeze(
        {
            "schema_version": LEGACY_DIRECT_CONTINUATION_CHECKPOINT_SCHEMA_VERSION,
            **common,
            "transitive_owner_refs": transitive,
            "causal_pointers": causal,
            "pending_capture_locators": _pending_captures(pending_capture_locators),
        }
    )
    digest = _sha256(document)
    return DirectContinuationCheckpoint(document, digest)


# Pure direct Executive Epoch facts. Physical ordering and persistence remain
# Store-owned; these values contain no research meaning or lifecycle policy.

DIRECT_EXECUTIVE_EPOCH_EVENT_SCHEMA_VERSION = 1
_DIRECT_EPOCH_EVENT_KINDS = frozenset(
    {"authorized", "bound", "checkpointed", "failed_before_checkpoint"}
)
_DIRECT_EPOCH_EVENT_COMMON_KEYS = {
    "schema_version",
    "kind",
    "executive_epoch_id",
    "mission_id",
}
_DIRECT_EPOCH_EVENT_KEYS = {
    "authorized": _DIRECT_EPOCH_EVENT_COMMON_KEYS
    | {"mission_root", "predecessor_checkpoint"},
    "bound": _DIRECT_EPOCH_EVENT_COMMON_KEYS | {"goal_thread_id", "workspace_root"},
    "checkpointed": _DIRECT_EPOCH_EVENT_COMMON_KEYS | {"checkpoint_ref"},
    "failed_before_checkpoint": _DIRECT_EPOCH_EVENT_COMMON_KEYS | {"reconciliation"},
}
_DIRECT_FAILURE_RECONCILIATION_KEYS = {"stage", "failure_reason"}
_DIRECT_FAILURE_STAGES = {"authorization_only", "goal_runtime"}
_DIRECT_EVENT_READBACK_KEYS = {
    "executive_epoch_id",
    "event_ordinal",
    "mission_id",
    "project_commit_no",
    "event_kind",
    "event",
    "event_digest",
    "predecessor_event_ordinal",
    "predecessor_event_digest",
    "created_actor",
    "created_at",
    "row_digest",
}
_DIRECT_EPOCH_AUTHORITY_KEYS = {
    "project_id",
    "root_identity",
    "canonical_authority_digest",
    "executive_epoch_id",
    "mission_id",
    "mission_root",
    "predecessor_checkpoint",
    "bound_event_ordinal",
    "bound_event_digest",
    "goal_thread_id",
    "workspace_root",
}


def _checkpoint_ref(value: Any, location: str) -> Mapping[str, Any]:
    item = _direct_closed(
        value,
        {"checkpoint_id", "payload_sha256"},
        location,
    )
    return deep_freeze(
        {
            "checkpoint_id": _require_text(
                item["checkpoint_id"],
                f"{location}.checkpoint_id",
            ),
            "payload_sha256": _require_digest(
                item["payload_sha256"],
                f"{location}.payload_sha256",
            ),
        }
    )


def _normalize_direct_epoch_event(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("direct Executive Epoch event must be an object")
    kind = value.get("kind")
    if kind not in _DIRECT_EPOCH_EVENT_KINDS:
        raise ValueError("direct Executive Epoch event kind is unsupported")
    document = _direct_closed(
        value,
        _DIRECT_EPOCH_EVENT_KEYS[str(kind)],
        f"{kind} event",
    )
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != DIRECT_EXECUTIVE_EPOCH_EVENT_SCHEMA_VERSION
    ):
        raise ValueError("direct Executive Epoch event identity is invalid")
    epoch_id = _require_text(
        document["executive_epoch_id"],
        "executive_epoch_id",
    )
    mission_id = _require_text(document["mission_id"], "mission_id")
    normalized = {key: document[key] for key in _DIRECT_EPOCH_EVENT_COMMON_KEYS}
    if kind == "authorized":
        mission_root = _owner_ref(document["mission_root"], "mission_root")
        if mission_root.kind != "mission" or mission_root.identity != mission_id:
            raise ValueError("authorized event requires its exact Mission root")
        normalized.update(
            {
                "mission_root": mission_root.to_mapping(),
                "predecessor_checkpoint": _predecessor(
                    document["predecessor_checkpoint"]
                ),
            }
        )
    elif kind == "bound":
        normalized.update(
            {
                "goal_thread_id": _require_text(
                    document["goal_thread_id"],
                    "goal_thread_id",
                ),
                "workspace_root": _require_text(
                    document["workspace_root"],
                    "workspace_root",
                ),
            }
        )
    elif kind == "checkpointed":
        normalized["checkpoint_ref"] = _checkpoint_ref(
            document["checkpoint_ref"],
            "checkpoint_ref",
        )
    else:
        reconciliation = _direct_closed(
            document["reconciliation"],
            _DIRECT_FAILURE_RECONCILIATION_KEYS,
            "reconciliation",
        )
        stage = _require_text(reconciliation["stage"], "reconciliation.stage")
        if stage not in _DIRECT_FAILURE_STAGES:
            raise ValueError("reconciliation.stage is not a factual failure stage")
        normalized["reconciliation"] = deep_freeze(
            {
                "stage": stage,
                "failure_reason": _require_text(
                    reconciliation["failure_reason"],
                    "reconciliation.failure_reason",
                ),
            }
        )
    normalized["executive_epoch_id"] = epoch_id
    normalized["mission_id"] = mission_id
    return deep_freeze(normalized)


@dataclass(frozen=True, slots=True)
class DirectExecutiveEpochEvent:
    """Immutable digest-checked fact in one direct epoch lifecycle."""

    document: Mapping[str, Any]
    digest_sha256: str

    def __post_init__(self) -> None:
        document = _normalize_direct_epoch_event(self.document)
        if self.digest_sha256 != _sha256(document):
            raise ValueError("direct Executive Epoch event digest is stale")
        object.__setattr__(self, "document", document)

    @property
    def event_kind(self) -> str:
        return str(self.document["kind"])

    @property
    def executive_epoch_id(self) -> str:
        return str(self.document["executive_epoch_id"])

    @property
    def mission_id(self) -> str:
        return str(self.document["mission_id"])

    def verify_integrity(self) -> None:
        if type(self) is not DirectExecutiveEpochEvent:
            raise TypeError(
                "direct Executive Epoch event subclasses are not exact values"
            )
        self.__post_init__()


def parse_direct_executive_epoch_event(
    document: Mapping[str, Any],
) -> DirectExecutiveEpochEvent:
    normalized = _normalize_direct_epoch_event(deep_thaw(document))
    digest = _sha256(normalized)
    return DirectExecutiveEpochEvent(normalized, digest)


def _direct_epoch_event_document(
    *,
    kind: str,
    executive_epoch_id: str,
    mission_id: str,
    specific: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {
        "schema_version": DIRECT_EXECUTIVE_EPOCH_EVENT_SCHEMA_VERSION,
        "kind": kind,
        "executive_epoch_id": _require_text(
            executive_epoch_id,
            "executive_epoch_id",
        ),
        "mission_id": _require_text(mission_id, "mission_id"),
        **specific,
    }


def prepare_direct_executive_epoch_authorized_event(
    *,
    executive_epoch_id: str,
    mission_root: OwnerRevisionRef,
    predecessor_checkpoint: Mapping[str, Any] | None,
) -> DirectExecutiveEpochEvent:
    if type(mission_root) is not OwnerRevisionRef or mission_root.kind != "mission":
        raise TypeError("authorized event requires an exact Mission OwnerRevisionRef")
    return parse_direct_executive_epoch_event(
        _direct_epoch_event_document(
            kind="authorized",
            executive_epoch_id=executive_epoch_id,
            mission_id=mission_root.identity,
            specific={
                "mission_root": mission_root.to_mapping(),
                "predecessor_checkpoint": _predecessor(predecessor_checkpoint),
            },
        )
    )


def prepare_direct_executive_epoch_bound_event(
    *,
    executive_epoch_id: str,
    mission_id: str,
    goal_thread_id: str,
    workspace_root: str,
) -> DirectExecutiveEpochEvent:
    return parse_direct_executive_epoch_event(
        _direct_epoch_event_document(
            kind="bound",
            executive_epoch_id=executive_epoch_id,
            mission_id=mission_id,
            specific={
                "goal_thread_id": _require_text(
                    goal_thread_id,
                    "goal_thread_id",
                ),
                "workspace_root": _require_text(
                    workspace_root,
                    "workspace_root",
                ),
            },
        )
    )


def prepare_direct_executive_epoch_checkpointed_event(
    checkpoint: DirectContinuationCheckpoint,
) -> DirectExecutiveEpochEvent:
    if type(checkpoint) is not DirectContinuationCheckpoint:
        raise TypeError("checkpointed event requires DirectContinuationCheckpoint")
    checkpoint.verify_integrity()
    mission_root = owner_revision_ref_from_mapping(checkpoint.document["mission_root"])
    return parse_direct_executive_epoch_event(
        _direct_epoch_event_document(
            kind="checkpointed",
            executive_epoch_id=str(checkpoint.document["authoring_epoch_id"]),
            mission_id=mission_root.identity,
            specific={"checkpoint_ref": checkpoint.to_reference()},
        )
    )


def prepare_direct_executive_epoch_failed_before_checkpoint_event(
    *,
    executive_epoch_id: str,
    mission_id: str,
    reconciliation: Mapping[str, Any],
) -> DirectExecutiveEpochEvent:
    return parse_direct_executive_epoch_event(
        _direct_epoch_event_document(
            kind="failed_before_checkpoint",
            executive_epoch_id=executive_epoch_id,
            mission_id=mission_id,
            specific={"reconciliation": reconciliation},
        )
    )


def _direct_event_readback(
    value: Mapping[str, Any],
    location: str,
) -> Mapping[str, Any]:
    item = _direct_closed(value, _DIRECT_EVENT_READBACK_KEYS, location)
    event = parse_direct_executive_epoch_event(item["event"])
    epoch_id = _require_text(
        item["executive_epoch_id"],
        f"{location}.executive_epoch_id",
    )
    mission_id = _require_text(item["mission_id"], f"{location}.mission_id")
    event_kind = _require_text(
        item["event_kind"],
        f"{location}.event_kind",
    )
    if (
        epoch_id != event.executive_epoch_id
        or mission_id != event.mission_id
        or event_kind != event.event_kind
    ):
        raise ValueError(f"{location} identity or kind differs from its document")
    digest = _require_digest(item["event_digest"], f"{location}.event_digest")
    if digest != event.digest_sha256:
        raise ValueError(f"{location} event digest differs from its document")
    ordinal = _require_positive_int(
        item["event_ordinal"],
        f"{location}.event_ordinal",
    )
    predecessor_ordinal = item["predecessor_event_ordinal"]
    if predecessor_ordinal is not None:
        predecessor_ordinal = _require_positive_int(
            predecessor_ordinal,
            f"{location}.predecessor_event_ordinal",
        )
    predecessor_digest = item["predecessor_event_digest"]
    if predecessor_digest is not None:
        predecessor_digest = _require_digest(
            predecessor_digest,
            f"{location}.predecessor_event_digest",
        )
    project_commit_no = _require_nonnegative_int(
        item["project_commit_no"],
        f"{location}.project_commit_no",
    )
    created_actor = _require_text(
        item["created_actor"],
        f"{location}.created_actor",
    )
    created_at = _require_text(item["created_at"], f"{location}.created_at")
    row_digest = _require_digest(item["row_digest"], f"{location}.row_digest")
    return deep_freeze(
        {
            "executive_epoch_id": epoch_id,
            "event_ordinal": ordinal,
            "mission_id": mission_id,
            "project_commit_no": project_commit_no,
            "event_kind": event_kind,
            "event": event,
            "event_digest": digest,
            "predecessor_event_ordinal": predecessor_ordinal,
            "predecessor_event_digest": predecessor_digest,
            "created_actor": created_actor,
            "created_at": created_at,
            "row_digest": row_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class DirectExecutiveEpochAuthority:
    """Immutable digest-checked projection of one exact bound event head."""

    material: Mapping[str, Any]
    authority_sha256: str

    def __post_init__(self) -> None:
        material = _direct_closed(
            self.material,
            _DIRECT_EPOCH_AUTHORITY_KEYS,
            "direct Executive Epoch authority",
        )
        for key in (
            "project_id",
            "root_identity",
            "executive_epoch_id",
            "mission_id",
            "goal_thread_id",
            "workspace_root",
        ):
            _require_text(material[key], key)
        _require_digest(
            material["canonical_authority_digest"],
            "canonical_authority_digest",
        )
        _require_digest(material["bound_event_digest"], "bound_event_digest")
        _require_positive_int(
            material["bound_event_ordinal"],
            "bound_event_ordinal",
        )
        mission_root = _owner_ref(material["mission_root"], "mission_root")
        if (
            mission_root.kind != "mission"
            or mission_root.identity != material["mission_id"]
        ):
            raise ValueError("authority Mission root is invalid")
        _predecessor(material["predecessor_checkpoint"])
        frozen = deep_freeze(material)
        if self.authority_sha256 != _sha256(frozen):
            raise ValueError("direct Executive Epoch authority digest is stale")
        object.__setattr__(self, "material", frozen)

    @property
    def mission_id(self) -> str:
        return str(self.material["mission_id"])

    @property
    def executive_epoch_id(self) -> str:
        return str(self.material["executive_epoch_id"])

    @property
    def project_id(self) -> str:
        return str(self.material["project_id"])

    @property
    def root_identity(self) -> str:
        return str(self.material["root_identity"])

    @property
    def canonical_authority_digest(self) -> str:
        return str(self.material["canonical_authority_digest"])

    @property
    def mission_root(self) -> Mapping[str, Any]:
        return self.material["mission_root"]

    @property
    def predecessor_checkpoint(self) -> Mapping[str, Any] | None:
        return self.material["predecessor_checkpoint"]

    @property
    def goal_thread_id(self) -> str:
        return str(self.material["goal_thread_id"])

    @property
    def workspace_root(self) -> str:
        return str(self.material["workspace_root"])

    @property
    def bound_event_ordinal(self) -> int:
        return int(self.material["bound_event_ordinal"])

    @property
    def bound_event_digest(self) -> str:
        return str(self.material["bound_event_digest"])

    def verify_integrity(self) -> None:
        if type(self) is not DirectExecutiveEpochAuthority:
            raise TypeError(
                "direct Executive Epoch authority subclasses are not exact values"
            )
        self.__post_init__()


def reissue_direct_executive_epoch_authority(
    *,
    event_readbacks: Sequence[Mapping[str, Any]],
    project_id: str,
    root_identity: str,
    canonical_authority_digest: str,
) -> DirectExecutiveEpochAuthority:
    readbacks = _direct_sequence(event_readbacks, "event_readbacks")
    if len(readbacks) != 2:
        raise ValueError("authority requires exactly authorized then bound readbacks")
    authorized = _direct_event_readback(readbacks[0], "event_readbacks[0]")
    bound = _direct_event_readback(readbacks[1], "event_readbacks[1]")
    authorized_event = authorized["event"]
    bound_event = bound["event"]
    assert type(authorized_event) is DirectExecutiveEpochEvent
    assert type(bound_event) is DirectExecutiveEpochEvent
    if (
        authorized_event.event_kind != "authorized"
        or bound_event.event_kind != "bound"
        or authorized["event_ordinal"] != 1
        or authorized["predecessor_event_ordinal"] is not None
        or authorized["predecessor_event_digest"] is not None
        or bound["event_ordinal"] != 2
        or bound["predecessor_event_ordinal"] != 1
        or bound["predecessor_event_digest"] != authorized["event_digest"]
    ):
        raise ValueError(
            "event readbacks are not the exact current authorized-bound chain"
        )
    if (
        authorized_event.executive_epoch_id != bound_event.executive_epoch_id
        or authorized_event.mission_id != bound_event.mission_id
    ):
        raise ValueError("event readbacks cross an Executive Epoch or Mission")
    material = deep_freeze(
        {
            "project_id": _require_text(project_id, "project_id"),
            "root_identity": _require_text(root_identity, "root_identity"),
            "canonical_authority_digest": _require_digest(
                canonical_authority_digest,
                "canonical_authority_digest",
            ),
            "executive_epoch_id": bound_event.executive_epoch_id,
            "mission_id": bound_event.mission_id,
            "mission_root": authorized_event.document["mission_root"],
            "predecessor_checkpoint": authorized_event.document[
                "predecessor_checkpoint"
            ],
            "bound_event_ordinal": bound["event_ordinal"],
            "bound_event_digest": bound["event_digest"],
            "goal_thread_id": bound_event.document["goal_thread_id"],
            "workspace_root": bound_event.document["workspace_root"],
        }
    )
    return DirectExecutiveEpochAuthority(
        material,
        _sha256(material),
    )
