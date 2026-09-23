"""Safe, proof-neutral observability primitives for the inactive workspace.

This module owns vocabulary and boundary validation only.  It does not install
a logger, metrics exporter, transport, background process, or persistent sink.
"""

from __future__ import annotations

import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol, runtime_checkable


OBSERVABILITY_CONTRACT_VERSION = 1
SYSTEM_NAME = "mathematical_research"

# Optional preparation progress is an observation, never migration authority.
# Counts are phase-local actual work items. Unknown totals stay unknown; these
# records neither estimate time nor claim a global remaining-node count.
PREPARATION_PROGRESS_PHASES = frozenset({
    "logical_digest_rows", "logical_digest_sort_rows", "database_copy", "index_source_ingest",
    "index_occurrence_build", "index_ordinary_build", "index_inverse_build",
    "index_directory_build", "index_audit_sources", "index_audit_occurrences",
    "index_audit_inverses", "source_seal", "source_backup", "source_verify",
    "migration_prepare", "migration_execute", "target_backup", "target_verify",
    "final_invariants", "transition_prepare", "preparation_complete",
})
_PREPARATION_PROGRESS_INTEGER_MAX = 2 ** 53 - 1
_preparation_progress_callback: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "research_core_preparation_progress_callback", default=None)


@contextmanager
def preparation_progress_sink(callback: Callable[[dict[str, Any]], None] | None) -> Iterator[None]:
    """Bind an optional observer only for the caller's synchronous/async scope.

    No transport, persistence, timer or cancellation behavior is installed.
    Nested scopes restore the previous observer, including exceptional exits.
    """
    token = _preparation_progress_callback.set(callback if callable(callback) else None)
    try:
        yield
    finally:
        _preparation_progress_callback.reset(token)


def report_preparation_progress(phase: str, completed: int = 0, total: int | None = None) -> None:
    """Best-effort closed numeric observation; invalid reports are omitted."""
    callback = _preparation_progress_callback.get()
    if callback is None:
        return
    if (type(phase) is not str or phase not in PREPARATION_PROGRESS_PHASES
            or type(completed) is not int or not 0 <= completed <= _PREPARATION_PROGRESS_INTEGER_MAX
            or (total is not None and (type(total) is not int
                or not completed <= total <= _PREPARATION_PROGRESS_INTEGER_MAX))):
        return
    try:
        callback({"phase": phase, "completed": completed, "total": total})
    except Exception:
        # An absent/broken telemetry consumer cannot alter the scientific or
        # migration result. Process cancellation remains caller-owned.
        pass

_TIMESTAMP_RE = re.compile(
    r"^(?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?Z$"
)
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{2,159}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")
_LOGICAL_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:/-]{0,191}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\d(). _-]{7,}\d)")
_SENSITIVE_VALUE_MARKERS = (
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "client_secret",
    "private_key",
    "credential",
    "bearer ",
    "begin private key",
    "begin rsa private key",
    "begin ec private key",
    "restricted_source",
)


class ObservationSafetyError(ValueError):
    """Raised when an observation could disclose non-observable material."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EventFamily(str, Enum):
    COMMAND = "command"
    PROJECT_COMMIT = "project_commit"
    WRITER = "writer"
    SQLITE = "sqlite"
    CAS = "cas"
    EVIDENCE_CLOSURE = "evidence_closure"
    RESERVATION_SETTLEMENT = "reservation_settlement"
    ATTEMPT = "attempt"
    CONTEXT_STALENESS = "context_staleness"
    ALERT_HOLD_DELIVERY = "alert_hold_delivery"
    BACKUP = "backup"
    RESTORE = "restore"
    MIGRATION = "migration"
    COMPATIBILITY = "compatibility"


class EventKind(str, Enum):
    COMMAND_ACCEPTED = "workspace.command.accepted"
    COMMAND_REJECTED = "workspace.command.rejected"
    COMMAND_REPLAYED = "workspace.command.replayed"
    PROJECT_COMMITTED = "workspace.project.committed"
    WRITER_EPOCH_CHANGED = "workspace.writer.epoch_changed"
    WRITER_LIFECYCLE_CHANGED = "workspace.writer.lifecycle_changed"
    SQLITE_BUSY = "workspace.sqlite.busy"
    SQLITE_CHECKPOINTED = "workspace.sqlite.checkpointed"
    SQLITE_INTEGRITY_CHECKED = "workspace.sqlite.integrity_checked"
    CAS_STAGED = "workspace.cas.staged"
    CAS_INSTALLED = "workspace.cas.installed"
    CAS_VERIFIED = "workspace.cas.verified"
    CAS_QUARANTINED = "workspace.cas.quarantined"
    CAS_SCRUBBED = "workspace.cas.scrubbed"
    EVIDENCE_CLOSURE_CHECKED = "workspace.evidence.closure_checked"
    RESERVATION_CHANGED = "workspace.reservation.changed"
    SETTLEMENT_CHANGED = "workspace.settlement.changed"
    ATTEMPT_REFERENCE_CHANGED = "workspace.attempt.reference_changed"
    ATTEMPT_RECONCILED = "workspace.attempt.reconciled"
    CONTEXT_STALENESS_CHANGED = "workspace.context.staleness_changed"
    ALERT_CHANGED = "workspace.alert.changed"
    HOLD_CHANGED = "workspace.hold.changed"
    DELIVERY_INTENT_CHANGED = "workspace.delivery_intent.changed"
    BACKUP_CHANGED = "workspace.backup.changed"
    RESTORE_CHANGED = "workspace.restore.changed"
    MIGRATION_CHANGED = "workspace.migration.changed"
    COMPATIBILITY_DIVERGENCE_DETECTED = "workspace.compatibility.divergence_detected"


EVENT_KINDS_BY_FAMILY: Mapping[EventFamily, frozenset[EventKind]] = MappingProxyType(
    {
        EventFamily.COMMAND: frozenset(
            {EventKind.COMMAND_ACCEPTED, EventKind.COMMAND_REJECTED, EventKind.COMMAND_REPLAYED}
        ),
        EventFamily.PROJECT_COMMIT: frozenset({EventKind.PROJECT_COMMITTED}),
        EventFamily.WRITER: frozenset(
            {EventKind.WRITER_EPOCH_CHANGED, EventKind.WRITER_LIFECYCLE_CHANGED}
        ),
        EventFamily.SQLITE: frozenset(
            {EventKind.SQLITE_BUSY, EventKind.SQLITE_CHECKPOINTED, EventKind.SQLITE_INTEGRITY_CHECKED}
        ),
        EventFamily.CAS: frozenset(
            {
                EventKind.CAS_STAGED,
                EventKind.CAS_INSTALLED,
                EventKind.CAS_VERIFIED,
                EventKind.CAS_QUARANTINED,
                EventKind.CAS_SCRUBBED,
            }
        ),
        EventFamily.EVIDENCE_CLOSURE: frozenset({EventKind.EVIDENCE_CLOSURE_CHECKED}),
        EventFamily.RESERVATION_SETTLEMENT: frozenset(
            {EventKind.RESERVATION_CHANGED, EventKind.SETTLEMENT_CHANGED}
        ),
        EventFamily.ATTEMPT: frozenset(
            {EventKind.ATTEMPT_REFERENCE_CHANGED, EventKind.ATTEMPT_RECONCILED}
        ),
        EventFamily.CONTEXT_STALENESS: frozenset({EventKind.CONTEXT_STALENESS_CHANGED}),
        EventFamily.ALERT_HOLD_DELIVERY: frozenset(
            {EventKind.ALERT_CHANGED, EventKind.HOLD_CHANGED, EventKind.DELIVERY_INTENT_CHANGED}
        ),
        EventFamily.BACKUP: frozenset({EventKind.BACKUP_CHANGED}),
        EventFamily.RESTORE: frozenset({EventKind.RESTORE_CHANGED}),
        EventFamily.MIGRATION: frozenset({EventKind.MIGRATION_CHANGED}),
        EventFamily.COMPATIBILITY: frozenset({EventKind.COMPATIBILITY_DIVERGENCE_DETECTED}),
    }
)


class MetricFamily(str, Enum):
    TRANSACTION = "transaction"
    WAL = "wal"
    CAS = "cas"
    EVIDENCE_CLOSURE = "evidence_closure"
    RESERVATION = "reservation"
    ATTEMPT = "attempt"
    CONTEXT = "context"
    HOLD = "hold"
    RECOVERY = "recovery"
    BACKUP = "backup"
    PROJECTION = "projection"


class MetricKind(str, Enum):
    TRANSACTION_LATENCY_SECONDS = "workspace_transaction_latency_seconds"
    TRANSACTION_CONFLICT_TOTAL = "workspace_transaction_conflict_total"
    TRANSACTION_BUSY_TOTAL = "workspace_transaction_busy_total"
    WAL_BYTES = "workspace_sqlite_wal_bytes"
    OLDEST_READER_AGE_SECONDS = "workspace_sqlite_oldest_reader_age_seconds"
    CAS_BYTES = "workspace_cas_bytes"
    CAS_ORPHAN_TOTAL = "workspace_cas_orphan_total"
    CAS_CORRUPTION_TOTAL = "workspace_cas_corruption_total"
    EVIDENCE_CLOSURE_FAILURE_TOTAL = "workspace_evidence_closure_failure_total"
    RESERVATION_UTILIZATION_RATIO = "workspace_reservation_utilization_ratio"
    ATTEMPT_UNSETTLED_TOTAL = "workspace_attempt_unsettled_total"
    ATTEMPT_UNKNOWN_TOTAL = "workspace_attempt_unknown_total"
    CONTEXT_STALE_TOTAL = "workspace_context_stale_total"
    URGENT_HOLD_OPEN_TOTAL = "workspace_urgent_hold_open_total"
    RECOVERY_DURATION_SECONDS = "workspace_recovery_duration_seconds"
    BACKUP_AGE_SECONDS = "workspace_backup_age_seconds"
    BACKUP_COVERAGE_RATIO = "workspace_backup_coverage_ratio"
    PROJECTION_FRESHNESS_SECONDS = "workspace_projection_freshness_seconds"


METRIC_KINDS_BY_FAMILY: Mapping[MetricFamily, frozenset[MetricKind]] = MappingProxyType(
    {
        MetricFamily.TRANSACTION: frozenset(
            {
                MetricKind.TRANSACTION_LATENCY_SECONDS,
                MetricKind.TRANSACTION_CONFLICT_TOTAL,
                MetricKind.TRANSACTION_BUSY_TOTAL,
            }
        ),
        MetricFamily.WAL: frozenset(
            {MetricKind.WAL_BYTES, MetricKind.OLDEST_READER_AGE_SECONDS}
        ),
        MetricFamily.CAS: frozenset(
            {MetricKind.CAS_BYTES, MetricKind.CAS_ORPHAN_TOTAL, MetricKind.CAS_CORRUPTION_TOTAL}
        ),
        MetricFamily.EVIDENCE_CLOSURE: frozenset({MetricKind.EVIDENCE_CLOSURE_FAILURE_TOTAL}),
        MetricFamily.RESERVATION: frozenset({MetricKind.RESERVATION_UTILIZATION_RATIO}),
        MetricFamily.ATTEMPT: frozenset(
            {MetricKind.ATTEMPT_UNSETTLED_TOTAL, MetricKind.ATTEMPT_UNKNOWN_TOTAL}
        ),
        MetricFamily.CONTEXT: frozenset({MetricKind.CONTEXT_STALE_TOTAL}),
        MetricFamily.HOLD: frozenset({MetricKind.URGENT_HOLD_OPEN_TOTAL}),
        MetricFamily.RECOVERY: frozenset({MetricKind.RECOVERY_DURATION_SECONDS}),
        MetricFamily.BACKUP: frozenset(
            {MetricKind.BACKUP_AGE_SECONDS, MetricKind.BACKUP_COVERAGE_RATIO}
        ),
        MetricFamily.PROJECTION: frozenset({MetricKind.PROJECTION_FRESHNESS_SECONDS}),
    }
)


class MetricUnit(str, Enum):
    SECONDS = "seconds"
    COUNT = "count"
    BYTES = "bytes"
    RATIO = "ratio"


METRIC_UNIT_BY_KIND: Mapping[MetricKind, MetricUnit] = MappingProxyType(
    {
        MetricKind.TRANSACTION_LATENCY_SECONDS: MetricUnit.SECONDS,
        MetricKind.TRANSACTION_CONFLICT_TOTAL: MetricUnit.COUNT,
        MetricKind.TRANSACTION_BUSY_TOTAL: MetricUnit.COUNT,
        MetricKind.WAL_BYTES: MetricUnit.BYTES,
        MetricKind.OLDEST_READER_AGE_SECONDS: MetricUnit.SECONDS,
        MetricKind.CAS_BYTES: MetricUnit.BYTES,
        MetricKind.CAS_ORPHAN_TOTAL: MetricUnit.COUNT,
        MetricKind.CAS_CORRUPTION_TOTAL: MetricUnit.COUNT,
        MetricKind.EVIDENCE_CLOSURE_FAILURE_TOTAL: MetricUnit.COUNT,
        MetricKind.RESERVATION_UTILIZATION_RATIO: MetricUnit.RATIO,
        MetricKind.ATTEMPT_UNSETTLED_TOTAL: MetricUnit.COUNT,
        MetricKind.ATTEMPT_UNKNOWN_TOTAL: MetricUnit.COUNT,
        MetricKind.CONTEXT_STALE_TOTAL: MetricUnit.COUNT,
        MetricKind.URGENT_HOLD_OPEN_TOTAL: MetricUnit.COUNT,
        MetricKind.RECOVERY_DURATION_SECONDS: MetricUnit.SECONDS,
        MetricKind.BACKUP_AGE_SECONDS: MetricUnit.SECONDS,
        MetricKind.BACKUP_COVERAGE_RATIO: MetricUnit.RATIO,
        MetricKind.PROJECTION_FRESHNESS_SECONDS: MetricUnit.SECONDS,
    }
)


class ObservationLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"


class ObservationEnvironment(str, Enum):
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"
    PROD = "prod"


_ID_KEYS = frozenset(
    {
        "project_id",
        "mission_id",
        "session_id",
        "branch_id",
        "strategy_id",
        "candidate_id",
        "context_id",
        "evidence_id",
        "attempt_id",
        "command_id",
        "alert_id",
        "hold_id",
        "backup_id",
        "migration_id",
        "correlation_id",
    }
)
_DIGEST_KEYS = frozenset(
    {"digest_sha256", "root_digest", "closure_digest", "authority_digest"}
)
_CODE_KEYS = frozenset(
    {"reason_code", "classification", "status", "outcome", "stage", "record_kind"}
)
_INTEGER_KEYS = frozenset(
    {
        "writer_epoch",
        "project_commit",
        "source_revision",
        "count",
        "byte_count",
        "retry_count",
        "conflict_count",
        "busy_count",
    }
)
_NUMBER_KEYS = frozenset({"duration_seconds", "latency_seconds", "age_seconds"})
_RATIO_KEYS = frozenset({"ratio", "coverage_ratio", "utilization_ratio"})
_SAFE_ATTRIBUTE_KEYS = _ID_KEYS | _DIGEST_KEYS | _CODE_KEYS | _INTEGER_KEYS | _NUMBER_KEYS | _RATIO_KEYS | {
    "logical_path"
}


def _safety_error(code: str, message: str) -> None:
    raise ObservationSafetyError(code, message)


def _validate_safe_string(value: str, *, key: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _safety_error("unsafe_attribute_value", f"{key} is not safe structured metadata")
    lowered = value.casefold()
    if (
        any(marker in lowered for marker in _SENSITIVE_VALUE_MARKERS)
        or _EMAIL_RE.search(value)
        or _PHONE_RE.search(value)
        or "restricted" in lowered
    ):
        _safety_error("sensitive_attribute_value", f"{key} contains non-observable material")
    return value


def validate_observation_attributes(attributes: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return an immutable, key-sorted safe metadata mapping.

    The boundary is intentionally allowlist-only.  It records correlation and
    operational state, never arbitrary prose or source content.
    """

    if attributes is None:
        return MappingProxyType({})
    if not isinstance(attributes, Mapping):
        _safety_error("attributes_not_mapping", "observation attributes must be a mapping")
    keys = tuple(attributes)
    if any(not isinstance(key, str) for key in keys):
        _safety_error("unsafe_attribute_key", "observation attribute keys must be strings")
    normalized: dict[str, Any] = {}
    for key in sorted(keys):
        if key not in _SAFE_ATTRIBUTE_KEYS:
            _safety_error("unsafe_attribute_key", "observation attributes use a closed safe-key set")
        value = attributes[key]
        if isinstance(value, (bytes, bytearray, memoryview, Mapping, list, tuple, set, frozenset)):
            _safety_error("unsafe_attribute_type", f"{key} cannot contain bytes or nested material")
        if key in _ID_KEYS:
            normalized[key] = _validate_safe_string(value, key=key, pattern=_OPAQUE_ID_RE)
        elif key in _DIGEST_KEYS:
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                _safety_error("unsafe_digest", f"{key} must be a lowercase SHA-256 digest")
            normalized[key] = value
        elif key in _CODE_KEYS:
            normalized[key] = _validate_safe_string(value, key=key, pattern=_SAFE_CODE_RE)
        elif key == "logical_path":
            normalized[key] = _validate_safe_string(value, key=key, pattern=_LOGICAL_PATH_RE)
            if value.startswith(("/", "\\")) or ".." in value or "//" in value or "\\" in value:
                _safety_error("unsafe_logical_path", "logical_path must be relative and normalized")
        elif key in _INTEGER_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                _safety_error("unsafe_numeric_value", f"{key} must be a non-negative integer")
            normalized[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _safety_error("unsafe_numeric_value", f"{key} must be a finite non-negative number")
            number = float(value)
            if not math.isfinite(number) or number < 0:
                _safety_error("unsafe_numeric_value", f"{key} must be a finite non-negative number")
            if key in _RATIO_KEYS and number > 1:
                _safety_error("unsafe_ratio", f"{key} must be between zero and one")
            normalized[key] = value
    return MappingProxyType(normalized)


def _timestamp(value: str) -> None:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise ValueError("occurred_at must be an explicit UTC RFC3339 timestamp")


@dataclass(frozen=True, slots=True)
class StructuredEvent:
    kind: EventKind
    occurred_at: str
    level: ObservationLevel = ObservationLevel.INFO
    environment: ObservationEnvironment = ObservationEnvironment.LOCAL
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EventKind):
            raise TypeError("kind must be EventKind")
        if not isinstance(self.level, ObservationLevel):
            raise TypeError("level must be ObservationLevel")
        if not isinstance(self.environment, ObservationEnvironment):
            raise TypeError("environment must be ObservationEnvironment")
        _timestamp(self.occurred_at)
        object.__setattr__(self, "attributes", validate_observation_attributes(self.attributes))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "ts": self.occurred_at,
            "level": self.level.value,
            "system": SYSTEM_NAME,
            "component": "research_workspace",
            "env": self.environment.value,
            "msg": self.kind.value,
            "event": self.kind.value,
            "contract_version": OBSERVABILITY_CONTRACT_VERSION,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class MetricSample:
    kind: MetricKind
    value: float | int
    occurred_at: str
    environment: ObservationEnvironment = ObservationEnvironment.LOCAL
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MetricKind):
            raise TypeError("kind must be MetricKind")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("metric value must be a finite non-negative number")
        number = float(self.value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("metric value must be a finite non-negative number")
        unit = METRIC_UNIT_BY_KIND[self.kind]
        if unit in {MetricUnit.COUNT, MetricUnit.BYTES} and not isinstance(self.value, int):
            raise ValueError("count and byte metric values must be non-negative integers")
        if unit is MetricUnit.RATIO and number > 1:
            raise ValueError("ratio metric value must be between zero and one")
        if not isinstance(self.environment, ObservationEnvironment):
            raise TypeError("environment must be ObservationEnvironment")
        _timestamp(self.occurred_at)
        object.__setattr__(self, "attributes", validate_observation_attributes(self.attributes))

    @property
    def unit(self) -> MetricUnit:
        return METRIC_UNIT_BY_KIND[self.kind]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "ts": self.occurred_at,
            "system": SYSTEM_NAME,
            "component": "research_workspace",
            "env": self.environment.value,
            "metric": self.kind.value,
            "value": self.value,
            "unit": self.unit.value,
            "contract_version": OBSERVABILITY_CONTRACT_VERSION,
            "attributes": dict(self.attributes),
        }


@runtime_checkable
class WorkspaceObserver(Protocol):
    """Minimal injection boundary; implementations receive validated records."""

    def emit_event(self, event: StructuredEvent) -> None: ...

    def observe_metric(self, sample: MetricSample) -> None: ...


class NoOpWorkspaceObserver:
    """Default observer for the inactive package; performs no external effect."""

    __slots__ = ()

    def emit_event(self, event: StructuredEvent) -> None:
        if not isinstance(event, StructuredEvent):
            raise TypeError("event must be a validated StructuredEvent")

    def observe_metric(self, sample: MetricSample) -> None:
        if not isinstance(sample, MetricSample):
            raise TypeError("sample must be a validated MetricSample")


class CaptureWorkspaceObserver:
    """Deterministic in-memory sink for tests and disposable qualification."""

    __slots__ = ("_events", "_metrics")

    def __init__(self) -> None:
        self._events: list[StructuredEvent] = []
        self._metrics: list[MetricSample] = []

    @property
    def events(self) -> tuple[StructuredEvent, ...]:
        return tuple(self._events)

    @property
    def metrics(self) -> tuple[MetricSample, ...]:
        return tuple(self._metrics)

    def emit_event(self, event: StructuredEvent) -> None:
        if not isinstance(event, StructuredEvent):
            raise TypeError("event must be a validated StructuredEvent")
        self._events.append(event)

    def observe_metric(self, sample: MetricSample) -> None:
        if not isinstance(sample, MetricSample):
            raise TypeError("sample must be a validated MetricSample")
        self._metrics.append(sample)

    def clear(self) -> None:
        self._events.clear()
        self._metrics.clear()


NOOP_WORKSPACE_OBSERVER: WorkspaceObserver = NoOpWorkspaceObserver()


def emit_event_safely(
    observer: WorkspaceObserver,
    *,
    kind: EventKind,
    occurred_at: str,
    level: ObservationLevel = ObservationLevel.INFO,
    environment: ObservationEnvironment = ObservationEnvironment.LOCAL,
    attributes: Mapping[str, Any] | None = None,
) -> bool:
    """Validate and deliver one event without influencing its owning operation.

    Observability is deliberately downstream of workspace authority.  A broken
    injected sink, or even a programming error that attempts to construct an
    unsafe record, is therefore reported only through the boolean return.  It
    must never change the authoritative operation's outcome.
    """

    try:
        observer.emit_event(
            StructuredEvent(
                kind=kind,
                occurred_at=occurred_at,
                level=level,
                environment=environment,
                attributes=attributes or {},
            )
        )
    except Exception:
        return False
    return True


def observe_metric_safely(
    observer: WorkspaceObserver,
    *,
    kind: MetricKind,
    value: float | int,
    occurred_at: str,
    environment: ObservationEnvironment = ObservationEnvironment.LOCAL,
    attributes: Mapping[str, Any] | None = None,
) -> bool:
    """Validate and deliver one metric without influencing workspace authority."""

    try:
        observer.observe_metric(
            MetricSample(
                kind=kind,
                value=value,
                occurred_at=occurred_at,
                environment=environment,
                attributes=attributes or {},
            )
        )
    except Exception:
        return False
    return True


def verify_observability_catalog() -> None:
    """Fail closed if a closed enum is missing or duplicates catalog ownership."""

    event_members = [item for values in EVENT_KINDS_BY_FAMILY.values() for item in values]
    metric_members = [item for values in METRIC_KINDS_BY_FAMILY.values() for item in values]
    if set(EVENT_KINDS_BY_FAMILY) != set(EventFamily):
        raise RuntimeError("event family catalog is incomplete")
    if len(event_members) != len(set(event_members)) or set(event_members) != set(EventKind):
        raise RuntimeError("event kind catalog is incomplete or overlapping")
    if set(METRIC_KINDS_BY_FAMILY) != set(MetricFamily):
        raise RuntimeError("metric family catalog is incomplete")
    if len(metric_members) != len(set(metric_members)) or set(metric_members) != set(MetricKind):
        raise RuntimeError("metric kind catalog is incomplete or overlapping")
    if set(METRIC_UNIT_BY_KIND) != set(MetricKind):
        raise RuntimeError("metric unit catalog is incomplete")


verify_observability_catalog()
