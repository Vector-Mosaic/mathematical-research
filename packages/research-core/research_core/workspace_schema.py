"""Closed persistence schema and read-only connection profile.

Mutable connections and migration are intentionally package-private.  Callers
receive snapshot-only connections; the owning :class:`WorkspaceStore` holds
the sole capability for genesis migration and later offline upgrades.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .json_support import canonical_json_bytes, loads_strict_json_object


LEGACY_MISSION_RUNTIME_APPLICATION_VERSION = (
    "research-workspace-mission-runtime-v1"
)
APPLICATION_VERSION = LEGACY_MISSION_RUNTIME_APPLICATION_VERSION
SCHEMA_VERSION = 12
ROOT_DIGEST_VERSION = 6
ACTIVE_BUSY_RETRY_POLICY = "surface_immediately_for_owner_operation_replay"
LEGACY_BOUNDED_BUSY_RETRY_POLICY = "legacy_bounded_sqlite_busy_timeout"
UNMANAGED_BUSY_RETRY_POLICY = "unmanaged"


class WorkspaceSchemaError(RuntimeError):
    """Raised when migration or schema verification cannot be trusted."""


def is_current_store_generation(
    schema_version: int, root_digest_version: int, operating_mode: str
) -> bool:
    """Whether an exact persisted envelope is the current Store generation."""

    return (schema_version, root_digest_version, operating_mode) == (
        SCHEMA_VERSION, ROOT_DIGEST_VERSION, "mission_runtime"
    )


def is_direct_mission_generation(
    schema_version: int, root_digest_version: int, operating_mode: str
) -> bool:
    """Direct-owner semantics shared by current and retained Store generations."""

    return operating_mode == "mission_runtime" and (
        schema_version, root_digest_version
    ) in {(9, 5), (10, 6), (SCHEMA_VERSION, ROOT_DIGEST_VERSION)}


class IdentityKind(str, Enum):
    MISSION = "mission"
    BRANCH = "branch"
    STRATEGY = "strategy"
    CONTEXT = "context"
    SESSION = "session"
    CANDIDATE = "candidate"


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


@dataclass(frozen=True, slots=True)
class TypedWorkspaceId:
    kind: IdentityKind
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", IdentityKind(self.kind))
        if not isinstance(self.value, str) or not _IDENTIFIER_RE.fullmatch(self.value):
            raise ValueError("workspace ID must be 1-192 safe visible characters")

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.value}"

    @classmethod
    def parse(cls, value: str) -> "TypedWorkspaceId":
        kind, separator, identifier = value.partition(":")
        if not separator:
            raise ValueError("typed workspace ID must use kind:value syntax")
        return cls(IdentityKind(kind), identifier)


@dataclass(frozen=True, slots=True)
class RevisionRef:
    object_id: TypedWorkspaceId
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision <= 0:
            raise ValueError("revision must be a positive integer")

    @property
    def key(self) -> str:
        return f"{self.object_id.key}@{self.revision}"


@dataclass(frozen=True, slots=True)
class CanonicalPayload:
    raw: bytes
    sha256: str

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8")

    def as_object(self) -> Mapping[str, Any]:
        return loads_strict_json_object(self.raw)


def canonical_payload(value: Any) -> CanonicalPayload:
    raw = canonical_json_bytes(value)
    return CanonicalPayload(raw=raw, sha256=hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    digest_sha256: str


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    foreign_keys: bool
    journal_mode: str
    synchronous: int
    busy_timeout_ms: int
    busy_retry_policy: str
    trusted_schema: bool


@dataclass(frozen=True, slots=True)
class SchemaContractVerification:
    schema_version: int
    migration_digest: str
    schema_object_digest: str
    connection_profile: ConnectionProfile


@dataclass(frozen=True, slots=True)
class SchemaVerification:
    schema_version: int
    migration_digest: str
    schema_object_digest: str
    integrity_check: str
    foreign_key_violations: tuple[tuple[Any, ...], ...]
    connection_profile: ConnectionProfile


@dataclass(frozen=True, slots=True)
class _MigrationCapability:
    database: Path
    root_identity: str
    purpose: str
    offline_proof: bool


@dataclass(frozen=True, slots=True)
class _OfflineMigrationExecution:
    """Closed values persisted by one verified side-by-side upgrade."""

    execution_id: str
    attempt_id: str
    source_schema_version: int
    target_schema_version: int
    verified_backup_manifest_sha256: str
    applied_history_sha256: str
    plan_sha256: str
    started_at: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class _AppliedOfflineMigration:
    """One precommit transition result, usable only on its unchanged connection."""

    connection: sqlite3.Connection
    execution: _OfflineMigrationExecution
    transition: Any
    total_changes: int
    data_version: int
    schema_versions: tuple[int, int, int]

    def validated_transition(
        self, connection: sqlite3.Connection, execution: _OfflineMigrationExecution
    ) -> Any:
        if (
            connection is not self.connection
            or execution != self.execution
            or connection.total_changes != self.total_changes
            or int(connection.execute("PRAGMA data_version").fetchone()[0]) != self.data_version
            or tuple(
                int(connection.execute(f"PRAGMA {name}").fetchone()[0])
                for name in ("user_version", "schema_version", "temp.schema_version")
            ) != self.schema_versions
        ):
            raise WorkspaceSchemaError("migration changed after its precommit validation")
        return self.transition


def _genesis_migration_capability(database: Path, *, root_identity: str) -> _MigrationCapability:
    if database.exists():
        raise WorkspaceSchemaError("genesis migration requires an absent database")
    return _MigrationCapability(
        database=database.resolve(strict=False),
        root_identity=root_identity,
        purpose="genesis",
        offline_proof=True,
    )


def _offline_migration_capability(
    database: Path,
    *,
    root_identity: str,
) -> _MigrationCapability:
    """Issue a package-private capability for an existing side-by-side image."""

    resolved = database.resolve(strict=True)
    if not resolved.is_file():
        raise WorkspaceSchemaError("offline migration requires an existing database image")
    if not root_identity:
        raise WorkspaceSchemaError("offline migration requires its recovery-root identity")
    return _MigrationCapability(
        database=resolved,
        root_identity=root_identity,
        purpose="offline_upgrade",
        offline_proof=True,
    )


MIGRATION_DIRECTORY = Path(__file__).with_name("migrations")
_MIGRATION_NAME_RE = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")


# Tables required by the live direct-owner route. Retired aggregate/Attempt/
# Admission/Card11 tables remain physically present for offline history and are
# still covered by the applied-migration and full schema-object digests below;
# their presence is not a live owner prerequisite.
REQUIRED_TABLES = frozenset(
    {
        "schema_migration",
        "migration_execution",
        "workspace_metadata",
        "writer_epoch",
        "project_commit",
        "executive_epoch_event",
        "continuation_checkpoint",
        "raw_capture",
        "raw_capture_artifact",
        "evidence_capture_source",
        "capture_scope_annotation_revision",
        "capture_scope_annotation_head",
        "transition_journal",
        "command_result",
        "mission_revision",
        "mission_head",
        "branch_revision",
        "branch_head",
        "strategy_revision",
        "strategy_head",
        "context_revision",
        "context_head",
        "session_revision",
        "session_head",
        "candidate_revision",
        "candidate_head",
        "blob",
        "evidence_item_revision",
        "evidence_item_head",
        "evidence_blob",
        "provenance_event",
        "independence_disclosure",
        "evidence_relation",
        "closure_manifest",
        "deletion_directive",
        "evidence_tombstone",
        "alert_event",
        "alert_scope",
        "alert_delivery_intent",
        "alert_acknowledgement",
        "hold",
        "hold_resolution",
    }
)

_SCHEMA10_REQUIRED_TABLES = frozenset(
    {
        "current_dependency_node",
        "current_dependency_projection",
        "capture_artifact_coverage_node",
        "root_hook_index_node",
    }
)

_SCHEMA11_REQUIRED_TABLES = frozenset({"owner_content_index_node"})


@lru_cache(maxsize=1)
def load_migrations() -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    if not MIGRATION_DIRECTORY.is_dir():
        raise WorkspaceSchemaError(f"migration directory is missing: {MIGRATION_DIRECTORY}")
    for path in sorted(MIGRATION_DIRECTORY.glob("*.sql"), key=lambda item: item.name):
        match = _MIGRATION_NAME_RE.fullmatch(path.name)
        if match is None:
            raise WorkspaceSchemaError(f"invalid migration filename: {path.name}")
        raw = path.read_bytes()
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                path=path,
                sql=raw.decode("utf-8", errors="strict"),
                digest_sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    if not migrations:
        raise WorkspaceSchemaError("at least one workspace migration is required")
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise WorkspaceSchemaError("migration versions must be contiguous from 0001")
    if versions[-1] != SCHEMA_VERSION:
        raise WorkspaceSchemaError("SCHEMA_VERSION does not match the migration set")
    return tuple(migrations)


def migration_set_digest(migrations: Iterable[Migration] | None = None) -> str:
    selected = tuple(migrations) if migrations is not None else load_migrations()
    return hashlib.sha256(
        canonical_json_bytes(
            [
                {
                    "version": migration.version,
                    "name": migration.name,
                    "sha256": migration.digest_sha256,
                }
                for migration in selected
            ]
        )
    ).hexdigest()


class _WorkspaceConnection(sqlite3.Connection):
    """Marker for connections that surface contention to the operation owner."""


def _connect_workspace(
    database: Path,
    *,
    read_only: bool = False,
    immutable: bool = False,
) -> sqlite3.Connection:
    """Open one profiled connection without a SQLite busy deadline."""

    if immutable and not read_only:
        raise WorkspaceSchemaError("immutable workspace connections must be read-only")
    if read_only:
        query = "mode=ro&immutable=1" if immutable else "mode=ro"
        uri = f"{database.resolve(strict=False).as_uri()}?{query}"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=0,
            isolation_level=None,
            factory=_WorkspaceConnection,
        )
    else:
        connection = sqlite3.connect(
            database,
            timeout=0,
            isolation_level=None,
            factory=_WorkspaceConnection,
        )
    connection._workspace_read_only_snapshot = read_only
    connection._workspace_immutable_snapshot = immutable
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 0")
    connection.execute("PRAGMA trusted_schema = OFF")
    if not read_only:
        mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).casefold()
        if mode != "wal":
            connection.close()
            raise WorkspaceSchemaError(f"workspace requires WAL journal mode, received {mode}")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


def open_snapshot_connection(database: Path) -> sqlite3.Connection:
    """Open a read-only SQLite snapshot; no public mutable connection exists."""

    return _connect_workspace(database, read_only=True)


def open_immutable_snapshot_connection(database: Path) -> sqlite3.Connection:
    """Open one sidecar-free immutable snapshot after an owner authenticates it."""

    return _connect_workspace(database, read_only=True, immutable=True)


def connection_profile(connection: sqlite3.Connection) -> ConnectionProfile:
    busy_timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    if isinstance(connection, _WorkspaceConnection) and busy_timeout_ms == 0:
        busy_retry_policy = ACTIVE_BUSY_RETRY_POLICY
    elif busy_timeout_ms > 0:
        busy_retry_policy = LEGACY_BOUNDED_BUSY_RETRY_POLICY
    else:
        busy_retry_policy = UNMANAGED_BUSY_RETRY_POLICY
    return ConnectionProfile(
        foreign_keys=bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
        journal_mode=str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold(),
        synchronous=int(connection.execute("PRAGMA synchronous").fetchone()[0]),
        busy_timeout_ms=busy_timeout_ms,
        busy_retry_policy=busy_retry_policy,
        trusted_schema=bool(connection.execute("PRAGMA trusted_schema").fetchone()[0]),
    )


def _is_read_only_workspace_snapshot(connection: sqlite3.Connection) -> bool:
    return isinstance(connection, _WorkspaceConnection) and bool(
        getattr(connection, "_workspace_read_only_snapshot", False)
    )


def _has_active_busy_retry_profile(profile: ConnectionProfile) -> bool:
    return (
        profile.busy_timeout_ms == 0
        and profile.busy_retry_policy == ACTIVE_BUSY_RETRY_POLICY
    )


def _has_sealed_compatible_busy_retry_profile(profile: ConnectionProfile) -> bool:
    return _has_active_busy_retry_profile(profile) or (
        profile.busy_timeout_ms > 0
        and profile.busy_retry_policy == LEGACY_BOUNDED_BUSY_RETRY_POLICY
    )


def verify_connection_profile(connection: sqlite3.Connection) -> ConnectionProfile:
    profile = connection_profile(connection)
    if not profile.foreign_keys:
        raise WorkspaceSchemaError("foreign key enforcement is disabled")
    if profile.journal_mode != "wal":
        raise WorkspaceSchemaError("workspace database is not in WAL mode")
    if profile.synchronous != 2 and not _is_read_only_workspace_snapshot(connection):
        raise WorkspaceSchemaError("workspace connection is not synchronous=FULL")
    if not _has_active_busy_retry_profile(profile):
        raise WorkspaceSchemaError(
            "workspace connection does not surface BUSY for owner-operation replay"
        )
    if profile.trusted_schema:
        raise WorkspaceSchemaError("workspace connection must disable trusted_schema")
    return profile


def _iter_statements(sql: str) -> Iterable[str]:
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    if buffer.strip():
        raise WorkspaceSchemaError("migration contains an incomplete SQL statement")


def _applied_migrations(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
    ).fetchone()
    if exists is None:
        return {}
    return {
        int(row["version"]): (str(row["name"]), str(row["digest_sha256"]))
        for row in connection.execute(
            "SELECT version, name, digest_sha256 FROM schema_migration ORDER BY version"
        )
    }


def _apply_migrations(
    connection: sqlite3.Connection,
    *,
    applied_at: str,
    capability: _MigrationCapability,
) -> None:
    """Apply the exact migration set under one explicit exclusive boundary."""

    if type(capability) is not _MigrationCapability or not capability.offline_proof:
        raise WorkspaceSchemaError("migration requires an offline store capability")
    database_row = connection.execute("PRAGMA database_list").fetchone()
    database = Path(str(database_row[2])).resolve(strict=False)
    if database != capability.database:
        raise WorkspaceSchemaError("migration capability is bound to another database")
    if capability.purpose == "genesis" and _applied_migrations(connection):
        raise WorkspaceSchemaError("genesis migration cannot update an existing schema")

    migrations = load_migrations()
    rebuilds_runtime_parents = any(
        migration.version in {5, 7, 8, 9, 10, 11, 12} for migration in migrations
    )
    if rebuilds_runtime_parents:
        # Migrations 0005, 0007, 0008, 0009, and 0010 rebuild parent tables while
        # preserving every historical row.
        # SQLite requires FK enforcement to be suspended before the exclusive
        # transaction; the complete graph is checked before commit and the
        # connection is restored on every exit path.
        connection.execute("PRAGMA foreign_keys = OFF")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
            raise WorkspaceSchemaError(
                "offline Mission runtime migration could not suspend FK enforcement"
            )
    connection.execute("BEGIN EXCLUSIVE")
    try:
        applied = _applied_migrations(connection)
        for migration in migrations:
            existing = applied.get(migration.version)
            expected = (migration.name, migration.digest_sha256)
            if existing is not None:
                if existing != expected:
                    raise WorkspaceSchemaError(
                        f"migration {migration.version} digest or name mismatch"
                    )
                continue
            if any(version > migration.version for version in applied):
                raise WorkspaceSchemaError("workspace migration history has a gap")
            for statement in _iter_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migration(version, name, digest_sha256, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.digest_sha256, applied_at),
            )
            applied[migration.version] = expected
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if rebuilds_runtime_parents:
            violations = tuple(connection.execute("PRAGMA foreign_key_check"))
            if violations:
                raise WorkspaceSchemaError(
                    "Mission runtime genesis produced a foreign-key violation"
                )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        if rebuilds_runtime_parents:
            connection.execute("PRAGMA foreign_keys = ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise WorkspaceSchemaError(
                    "Mission runtime genesis did not restore FK enforcement"
                )


def _apply_offline_migration_plan(
    connection: sqlite3.Connection,
    *,
    pending_migrations: tuple[tuple[int, str, str], ...],
    execution: _OfflineMigrationExecution,
    capability: _MigrationCapability,
    fault_hook: Any | None = None,
) -> _AppliedOfflineMigration | None:
    """Apply one exact registered suffix under a single exclusive transaction.

    The executor intentionally accepts only the closed suffix already bound by
    an ``OfflineMigrationPlan``.  Every DDL statement, the applied-history row,
    workspace metadata, user version, and successful execution marker commit
    together.  A fault raised before ``COMMIT`` therefore restores the exact
    pre-upgrade logical schema.  A post-commit fault is classified from a fresh
    connection by the migration recovery owner before exact replay.
    """

    if type(capability) is not _MigrationCapability or not capability.offline_proof:
        raise WorkspaceSchemaError("migration requires an offline store capability")
    if type(execution) is not _OfflineMigrationExecution:
        raise WorkspaceSchemaError("migration requires exact offline execution authority")
    if capability.purpose != "offline_upgrade":
        raise WorkspaceSchemaError("offline migration requires an upgrade capability")
    database_row = connection.execute("PRAGMA database_list").fetchone()
    database = Path(str(database_row[2])).resolve(strict=False)
    if database != capability.database:
        raise WorkspaceSchemaError("migration capability is bound to another database")
    if execution.target_schema_version > SCHEMA_VERSION:
        raise WorkspaceSchemaError("offline migration target exceeds package schema authority")
    if execution.target_schema_version <= execution.source_schema_version:
        raise WorkspaceSchemaError("offline migration must advance the schema")
    if execution.source_schema_version == 11:
        raise WorkspaceSchemaError("schema 11 is not a retained runtime source")
    if execution.target_schema_version == 9:
        raise WorkspaceSchemaError(
            "schema 9 requires the specialized local dot6 successor upgrader"
        )
    if execution.target_schema_version == 11:
        raise WorkspaceSchemaError("schema 11 is not a runtime target")

    migrations = load_migrations()
    registered = {
        item.version: (item.name, item.digest_sha256, item)
        for item in migrations
    }
    expected_versions = tuple(
        range(execution.source_schema_version + 1, execution.target_schema_version + 1)
    )
    if tuple(item[0] for item in pending_migrations) != expected_versions:
        raise WorkspaceSchemaError("offline migration suffix is not contiguous")
    for version, name, digest_sha256 in pending_migrations:
        registered_item = registered.get(version)
        if registered_item is None or registered_item[:2] != (name, digest_sha256):
            raise WorkspaceSchemaError("offline migration suffix differs from package authority")
    if execution.target_schema_version == 7 and (
        execution.source_schema_version != 6
        or expected_versions != (7,)
    ):
        raise WorkspaceSchemaError(
            "schema 7 offline migration requires the exact schema-6 suffix"
        )
    if connection.in_transaction:
        raise WorkspaceSchemaError(
            "offline migration requires an unowned autocommit connection"
        )

    if execution.target_schema_version == 8 and (
        execution.source_schema_version != 7
        or expected_versions != (8,)
    ):
        raise WorkspaceSchemaError(
            "schema 8 offline migration requires the exact schema-7 suffix"
        )
    if execution.target_schema_version == 10 and (
        execution.source_schema_version != 9 or expected_versions != (10,)
    ):
        raise WorkspaceSchemaError(
            "schema 10 offline migration requires the exact schema-9 suffix"
        )
    if execution.target_schema_version == 12 and (
        execution.source_schema_version != 10 or expected_versions != (11, 12)
    ):
        raise WorkspaceSchemaError(
            "schema 12 offline migration requires the exact schema-10 suffix"
        )

    rebuilds_runtime_parents = any(
        version in {5, 7, 8, 10, 11, 12}
        for version, _name, _digest_sha256 in pending_migrations
    )
    root_transition = None
    root_refresh = None
    validated_transition = None
    applied = None
    try:
        if rebuilds_runtime_parents:
            connection.execute("PRAGMA foreign_keys = OFF")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
                raise WorkspaceSchemaError(
                    "offline Mission runtime migration could not suspend FK enforcement"
                )
        connection.execute("BEGIN EXCLUSIVE")
        if execution.target_schema_version == 7:
            from .workspace_store import _issue_schema_v7_root_transition

            root_transition = _issue_schema_v7_root_transition(
                connection,
                execution=execution,
                capability=capability,
            )
        elif execution.target_schema_version == 8:
            from .workspace_store import _issue_schema_v8_root_refresh

            root_refresh = _issue_schema_v8_root_refresh(
                connection,
                execution=execution,
                capability=capability,
            )
        elif execution.target_schema_version == 10:
            from .workspace_store import _issue_schema_v10_root_transition

            root_transition = _issue_schema_v10_root_transition(
                connection, execution=execution, capability=capability,
            )
        elif execution.target_schema_version == 12:
            from .workspace_store import _issue_schema_v12_root_transition

            root_transition = _issue_schema_v12_root_transition(
                connection, execution=execution, capability=capability,
            )
        applied = _applied_migrations(connection)
        if tuple(sorted(applied)) != tuple(range(1, execution.source_schema_version + 1)):
            raise WorkspaceSchemaError("offline migration source history is not contiguous")
        for version, name, digest_sha256 in pending_migrations:
            migration = registered[version][2]
            if version in applied:
                raise WorkspaceSchemaError("offline migration suffix is already applied")
            for statement_index, statement in enumerate(_iter_statements(migration.sql), start=1):
                connection.execute(statement)
                if fault_hook is not None:
                    fault_hook(
                        "after_migration_statement",
                        {
                            "version": version,
                            "name": name,
                            "statement_index": statement_index,
                        },
                    )
            connection.execute(
                "INSERT INTO schema_migration(version, name, digest_sha256, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (version, name, digest_sha256, execution.completed_at),
            )
            applied[version] = (name, digest_sha256)
        if execution.target_schema_version == 7 and fault_hook is not None:
            fault_hook(
                "after_schema_v7_history",
                {
                    "execution_id": execution.execution_id,
                    "attempt_id": execution.attempt_id,
                },
            )

        if execution.target_schema_version not in {7, 8, 10, 12}:
            connection.execute(
                "UPDATE workspace_metadata SET schema_version = ?, application_version = ? "
                "WHERE singleton = 1",
                (
                    execution.target_schema_version,
                    LEGACY_MISSION_RUNTIME_APPLICATION_VERSION,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise WorkspaceSchemaError("offline migration workspace metadata is absent")
        connection.execute(
            "INSERT INTO migration_execution("
            "execution_id, attempt_id, source_schema_version, target_schema_version, "
            "verified_backup_manifest_sha256, applied_history_sha256, plan_sha256, "
            "started_at, completed_at, status, canonical_effect"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', 'none')",
            (
                execution.execution_id,
                execution.attempt_id,
                execution.source_schema_version,
                execution.target_schema_version,
                execution.verified_backup_manifest_sha256,
                execution.applied_history_sha256,
                execution.plan_sha256,
                execution.started_at,
                execution.completed_at,
            ),
        )
        if execution.target_schema_version == 7 and fault_hook is not None:
            fault_hook(
                "after_schema_v7_migration_execution",
                {
                    "execution_id": execution.execution_id,
                    "attempt_id": execution.attempt_id,
                },
            )
        connection.execute(f"PRAGMA user_version = {execution.target_schema_version}")
        if execution.target_schema_version == 7 and fault_hook is not None:
            fault_hook(
                "after_schema_v7_user_version",
                {
                    "execution_id": execution.execution_id,
                    "attempt_id": execution.attempt_id,
                },
            )
        if execution.target_schema_version == 7:
            from .workspace_store import _apply_schema_v7_root_transition

            if root_transition is None:
                raise WorkspaceSchemaError("schema7 root transition capture is absent")
            validated_transition = _apply_schema_v7_root_transition(
                connection,
                transition=root_transition,
                fault_hook=fault_hook,
            )
        elif execution.target_schema_version == 8:
            from .workspace_store import _apply_schema_v8_root_refresh

            if root_refresh is None:
                raise WorkspaceSchemaError("schema8 root refresh capture is absent")
            _apply_schema_v8_root_refresh(
                connection,
                refresh=root_refresh,
                fault_hook=fault_hook,
            )
        elif execution.target_schema_version == 10:
            from .workspace_store import _apply_schema_v10_root_transition

            if root_transition is None:
                raise WorkspaceSchemaError("schema10 root transition capture is absent")
            validated_transition = _apply_schema_v10_root_transition(
                connection, transition=root_transition, fault_hook=fault_hook,
            )
        elif execution.target_schema_version == 12:
            from .workspace_store import _apply_schema_v12_root_transition

            if root_transition is None:
                raise WorkspaceSchemaError("schema12 root transition capture is absent")
            validated_transition = _apply_schema_v12_root_transition(
                connection, transition=root_transition, fault_hook=fault_hook,
            )
        if validated_transition is not None:
            applied = _AppliedOfflineMigration(
                connection=connection, execution=execution, transition=validated_transition,
                total_changes=connection.total_changes,
                data_version=int(connection.execute("PRAGMA data_version").fetchone()[0]),
                schema_versions=tuple(
                    int(connection.execute(f"PRAGMA {name}").fetchone()[0])
                    for name in ("user_version", "schema_version", "temp.schema_version")
                ),
            )
        if fault_hook is not None:
            fault_hook(
                "before_migration_commit",
                {
                    "source_schema_version": execution.source_schema_version,
                    "target_schema_version": execution.target_schema_version,
                },
            )
        if rebuilds_runtime_parents:
            violations = tuple(connection.execute("PRAGMA foreign_key_check"))
            if violations:
                raise WorkspaceSchemaError(
                    "offline Mission runtime migration produced a foreign-key violation"
                )
        if applied is not None:
            applied.validated_transition(connection, execution)
        connection.execute("COMMIT")
        if fault_hook is not None:
            fault_hook(
                "after_migration_commit_before_return",
                {
                    "execution_id": execution.execution_id,
                    "attempt_id": execution.attempt_id,
                    "source_schema_version": execution.source_schema_version,
                    "target_schema_version": execution.target_schema_version,
                },
            )
        if applied is not None:
            applied.validated_transition(connection, execution)
        return applied
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        if rebuilds_runtime_parents:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.execute("PRAGMA foreign_keys = ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise WorkspaceSchemaError(
                    "offline Mission runtime migration did not restore FK enforcement"
                )


def _schema_object_payload(connection: sqlite3.Connection) -> list[dict[str, str]]:
    return [
        {
            "type": str(row["type"]),
            "name": str(row["name"]),
            "table": str(row["tbl_name"]),
            "sql": " ".join(str(row["sql"]).split()),
        }
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
            "ORDER BY type, name"
        )
    ]


def schema_object_digest(connection: sqlite3.Connection) -> str:
    return hashlib.sha256(canonical_json_bytes(_schema_object_payload(connection))).hexdigest()


@lru_cache(maxsize=None)
def _expected_schema_object_digest_for_version(schema_version: int) -> str:
    migrations = tuple(
        item for item in load_migrations() if item.version <= schema_version
    )
    if not migrations or migrations[-1].version != schema_version:
        raise WorkspaceSchemaError("schema-object target is not registered")
    connection = sqlite3.connect(":memory:", isolation_level=None, timeout=0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for migration in migrations:
            for statement in _iter_statements(migration.sql):
                connection.execute(statement)
        return schema_object_digest(connection)
    finally:
        connection.close()


def expected_schema_object_digest() -> str:
    return _expected_schema_object_digest_for_version(SCHEMA_VERSION)


def registered_schema_contract(schema_version: int) -> dict[str, Any]:
    """Return immutable registered digests, not a physical-schema verification.

    Historical root verification must select the generation at its authenticated
    commit boundary. A later physical schema is checked separately and must not
    replace the schema contract committed by an earlier root.
    """

    migrations = tuple(
        item for item in load_migrations() if item.version <= schema_version
    )
    if (
        type(schema_version) is not int
        or not migrations
        or migrations[-1].version != schema_version
    ):
        raise WorkspaceSchemaError("schema-contract target is not registered")
    return {
        "schema_version": schema_version,
        "migration_set_digest": migration_set_digest(migrations),
        "schema_object_digest": _expected_schema_object_digest_for_version(schema_version),
    }


def verify_schema_contract(
    connection: sqlite3.Connection,
    *,
    allow_sealed_delete: bool = False,
    schema_version: int = SCHEMA_VERSION,
) -> SchemaContractVerification:
    """Check the fixed registered schema contract without scanning Store rows."""

    databases = tuple(connection.execute("PRAGMA database_list"))
    if any(str(row[1]) not in {"main", "temp"} for row in databases):
        raise WorkspaceSchemaError("workspace connection has an attached database")
    if connection.execute("SELECT 1 FROM sqlite_temp_master LIMIT 1").fetchone():
        raise WorkspaceSchemaError("workspace connection has temporary schema objects")
    profile = connection_profile(connection)
    if allow_sealed_delete:
        if (
            not profile.foreign_keys
            or profile.journal_mode != "delete"
            or (
                profile.synchronous != 2
                and not _is_read_only_workspace_snapshot(connection)
            )
            or not _has_sealed_compatible_busy_retry_profile(profile)
            or profile.trusted_schema
        ):
            raise WorkspaceSchemaError("sealed backup connection profile is invalid")
    else:
        profile = verify_connection_profile(connection)
    applied = _applied_migrations(connection)
    migrations = tuple(
        migration for migration in load_migrations()
        if migration.version <= schema_version
    )
    if not migrations or migrations[-1].version != schema_version:
        raise WorkspaceSchemaError("schema-contract target is not registered")
    expected = {
        migration.version: (migration.name, migration.digest_sha256)
        for migration in migrations
    }
    if applied != expected:
        raise WorkspaceSchemaError("applied migration set does not match package authority")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != schema_version:
        raise WorkspaceSchemaError("workspace user_version does not match package schema")
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    # The exact object digest is authoritative for historical generations;
    # current direct-owner tables are required only where they exist.
    required_tables = REQUIRED_TABLES | (
        _SCHEMA10_REQUIRED_TABLES if schema_version >= 10 else frozenset()
    ) | (
        _SCHEMA11_REQUIRED_TABLES if schema_version >= 11 else frozenset()
    )
    missing = sorted(required_tables - tables) if schema_version >= 9 else []
    if missing:
        raise WorkspaceSchemaError(f"workspace schema is missing tables: {', '.join(missing)}")
    actual_object_digest = schema_object_digest(connection)
    if actual_object_digest != _expected_schema_object_digest_for_version(schema_version):
        raise WorkspaceSchemaError("workspace schema object digest mismatch")
    return SchemaContractVerification(
        schema_version=version,
        migration_digest=migration_set_digest(migrations),
        schema_object_digest=actual_object_digest,
        connection_profile=profile,
    )


def verify_schema(
    connection: sqlite3.Connection,
    *,
    allow_sealed_delete: bool = False,
    schema_version: int = SCHEMA_VERSION,
) -> SchemaVerification:
    """Verify the schema contract and the complete SQLite/FK contents."""

    contract = verify_schema_contract(
        connection,
        allow_sealed_delete=allow_sealed_delete,
        schema_version=schema_version,
    )
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise WorkspaceSchemaError(f"SQLite integrity_check failed: {integrity}")
    violations = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise WorkspaceSchemaError("workspace contains foreign key violations")
    return SchemaVerification(
        schema_version=contract.schema_version,
        migration_digest=contract.migration_digest,
        schema_object_digest=contract.schema_object_digest,
        integrity_check=integrity,
        foreign_key_violations=violations,
        connection_profile=contract.connection_profile,
    )
