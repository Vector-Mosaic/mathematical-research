"""Exclusive, side-by-side execution for a verified offline migration plan.

This module owns no startup hook and cannot select a live workspace root.  It
copies the SQLite image from an already verified :class:`BackupSet` into the
owning workspace's recovery directory, applies only the suffix named by the
``OfflineMigrationPlan``, and leaves an operator-inspectable recovery artifact.
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping

from .json_support import canonical_json_bytes, loads_strict_json_bytes
from .observability import (
    NOOP_WORKSPACE_OBSERVER,
    EventKind,
    ObservationEnvironment,
    ObservationLevel,
    WorkspaceObserver,
    emit_event_safely,
    report_preparation_progress,
)
from .workspace_paths import WorkspacePathError, WorkspacePaths
from .workspace_operation_lease import (
    HeldKernelLease,
    KernelLeaseError,
    acquire_kernel_lease,
)
from .workspace_recovery import (
    BackupSet,
    OfflineMigrationPlan,
    RecoveryError,
    RecoveryLifecycle,
    _COMMITTED_BLOB_CAS_INVENTORY,
)
from .workspace_schema import (
    APPLICATION_VERSION,
    SCHEMA_VERSION,
    SchemaVerification,
    WorkspaceSchemaError,
    _AppliedOfflineMigration,
    _OfflineMigrationExecution,
    _applied_migrations,
    _apply_offline_migration_plan,
    _connect_workspace,
    _has_sealed_compatible_busy_retry_profile,
    connection_profile,
    _expected_schema_object_digest_for_version,
    _offline_migration_capability,
    load_migrations,
    migration_set_digest,
    open_snapshot_connection,
    schema_object_digest,
    verify_connection_profile,
    verify_schema,
)


_MIGRATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ATTEMPT_FILE_RE = re.compile(r"^attempt-(?P<number>[0-9]{4})\.started\.json$")
_OUTCOME_FILE_RE = re.compile(r"^attempt-(?P<number>[0-9]{4})\.outcome\.json$")
_ATOMIC_WRITE_TEMP_RE = re.compile(
    r"^\.(?:migration_state\.json|attempt-[0-9]{4}\.(?:started|outcome)\.json)"
    r"\.pending$"
)
_LEGACY_STATE_TEMP_RE = re.compile(r"^\.migration_state\.json\.[0-9]+\.tmp$")
_MAX_ATTEMPT_NUMBER = 9999
_MIGRATION_RESULT_ISSUER_TOKEN = object()
_MIGRATION_RESULT_ISSUER_SECRET = os.urandom(32)
# Private working-set controls, not limits on a Store or an individual row.
_LOGICAL_DIGEST_SORT_BYTES = 8 * 1024 * 1024
_LOGICAL_DIGEST_MERGE_FAN_IN = 16


class MigrationExecutionError(RuntimeError):
    """Closed failure from one inactive migration execution attempt."""

    def __init__(
        self, code: str, message: str, *, artifact: Path | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.artifact = artifact


@dataclass(frozen=True, slots=True)
class OfflineMigrationResult:
    migration_id: str
    artifact: Path
    database: Path
    attempt_id: str
    source_schema_version: int
    target_schema_version: int
    plan_sha256: str
    backup_manifest_sha256: str
    pre_logical_sha256: str
    post_logical_sha256: str
    migration_history_count: int
    lifecycle: RecoveryLifecycle = RecoveryLifecycle.VERIFIED_READ_ONLY
    selected_live_root: bool = False
    mission_auto_resume: bool = False
    canonical_effect: str = "none"
    _issuer_token: object = field(default=None, repr=False, compare=False)
    _issuer_seal: str = field(default="", repr=False, compare=False)
    _artifact_files: tuple[tuple[str, str, int], ...] = field(
        default=(), repr=False, compare=False
    )

    def __post_init__(self) -> None:
        artifact = Path(self.artifact).resolve(strict=False)
        database = Path(self.database).resolve(strict=False)
        object.__setattr__(self, "artifact", artifact)
        object.__setattr__(self, "database", database)
        if (
            not _MIGRATION_ID_RE.fullmatch(self.migration_id)
            or database.parent != artifact
            or database.name != "workspace.sqlite3"
            or re.fullmatch(
                re.escape(self.migration_id) + r"\.attempt\.[0-9]{4}",
                self.attempt_id,
            )
            is None
        ):
            raise ValueError("offline migration result identity or path is invalid")
        integers = (
            self.source_schema_version,
            self.target_schema_version,
            self.migration_history_count,
        )
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in integers)
            or self.source_schema_version < 1
            or self.target_schema_version <= self.source_schema_version
            or self.migration_history_count != self.target_schema_version
        ):
            raise ValueError("offline migration result schema history is invalid")
        for value in (
            self.plan_sha256,
            self.backup_manifest_sha256,
            self.pre_logical_sha256,
            self.post_logical_sha256,
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("offline migration result digest is invalid")
        if (
            self.lifecycle is not RecoveryLifecycle.VERIFIED_READ_ONLY
            or self.selected_live_root
            or self.mission_auto_resume
            or self.canonical_effect != "none"
        ):
            raise ValueError("offline migration result is not verified read-only")

    def to_payload(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "artifact": str(self.artifact),
            "database": str(self.database),
            "attempt_id": self.attempt_id,
            "source_schema_version": self.source_schema_version,
            "target_schema_version": self.target_schema_version,
            "plan_sha256": self.plan_sha256,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "pre_logical_sha256": self.pre_logical_sha256,
            "post_logical_sha256": self.post_logical_sha256,
            "migration_history_count": self.migration_history_count,
            "lifecycle": self.lifecycle.value,
            "selected_live_root": self.selected_live_root,
            "mission_auto_resume": self.mission_auto_resume,
            "canonical_effect": self.canonical_effect,
        }

    def verify_issued(self) -> None:
        if (
            type(self) is not OfflineMigrationResult
            or self._issuer_token is not _MIGRATION_RESULT_ISSUER_TOKEN
            or not hmac.compare_digest(
                self._issuer_seal,
                _offline_migration_result_seal(self.to_payload(), self._artifact_files),
            )
        ):
            raise MigrationExecutionError(
                "migration_result_not_issued",
                "offline migration result was not issued by the migration executor",
                artifact=self.artifact,
            )


def _offline_migration_result_seal(
    payload: Mapping[str, Any], artifact_files: tuple[tuple[str, str, int], ...]
) -> str:
    return hmac.new(
        _MIGRATION_RESULT_ISSUER_SECRET,
        canonical_json_bytes(
            {
                "domain": "research_workspace_offline_migration_result.v1",
                "payload": payload,
                "artifact_files": artifact_files,
            }
        ),
        hashlib.sha256,
    ).hexdigest()


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def _fsync_directory(path: Path) -> None:
    """Persist directory entry changes where the platform exposes that primitive."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _plan_payload(plan: OfflineMigrationPlan) -> dict[str, Any]:
    return {
        "current_schema_version": plan.current_schema_version,
        "target_schema_version": plan.target_schema_version,
        "verified_backup_manifest_sha256": plan.verified_backup_manifest_sha256,
        "applied_history_sha256": plan.applied_history_sha256,
        "pending_migrations": [list(item) for item in plan.pending_migrations],
        "lifecycle": RecoveryLifecycle(plan.lifecycle).value,
        "automatic": plan.automatic,
        "downgrade": plan.downgrade,
    }


def _atomic_write_temporary(path: Path) -> Path:
    return path.with_name(f".{path.name}.pending")


def _stage_atomic_write(path: Path, raw: bytes) -> Path:
    temporary = _atomic_write_temporary(path)
    created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        if created:
            temporary.unlink(missing_ok=True)
        raise


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish one complete JSON file without replacement."""

    temporary = _stage_atomic_write(path, canonical_json_bytes(payload))
    try:
        os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent)


def _replace_state(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = _stage_atomic_write(path, canonical_json_bytes(payload))
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_atomic_write_temporaries(artifact: Path) -> tuple[Path, ...]:
    """Identify only plain unpublished files owned by this artifact."""

    candidates: list[Path] = []
    try:
        for candidate in artifact.iterdir():
            if not (
                _ATOMIC_WRITE_TEMP_RE.fullmatch(candidate.name)
                or _LEGACY_STATE_TEMP_RE.fullmatch(candidate.name)
            ):
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise MigrationExecutionError(
                    "migration_artifact_invalid",
                    "migration atomic-write temporary is not a plain file",
                    artifact=artifact,
                )
            candidates.append(candidate)
    except MigrationExecutionError:
        raise
    except OSError as exc:
        raise MigrationExecutionError(
            "migration_artifact_invalid",
            "migration atomic-write temporary could not be inspected",
            artifact=artifact,
        ) from exc
    return tuple(candidates)


def _reconcile_atomic_write_temporaries(artifact: Path) -> None:
    """Discard only unpublished files owned by this artifact's write protocol."""

    candidates = _validated_atomic_write_temporaries(artifact)
    try:
        for candidate in candidates:
            candidate.unlink()
        if candidates:
            _fsync_directory(artifact)
    except OSError as exc:
        raise MigrationExecutionError(
            "migration_artifact_invalid",
            "migration atomic-write temporary could not be reconciled",
            artifact=artifact,
        ) from exc


def _read_object(path: Path, *, code: str) -> dict[str, Any]:
    try:
        value = loads_strict_json_bytes(path.read_bytes())
    except Exception as exc:
        raise MigrationExecutionError(
            code, f"migration state is unreadable: {exc}", artifact=path.parent
        ) from exc
    if not isinstance(value, dict):
        raise MigrationExecutionError(
            code, "migration state is not an object", artifact=path.parent
        )
    return value


@contextmanager
def _acquire_migration_lease(
    paths: WorkspacePaths,
    *,
    migration_id: str,
    actor: str,
    fault_hook: Callable[[str, Mapping[str, Any]], None] | None,
) -> Iterator[HeldKernelLease]:
    """Hold one kernel-released lease without treating file existence as authority."""

    lock_path = paths.recovery / f".migration-{migration_id}.executor.lock"
    try:
        with acquire_kernel_lease(
            lock_path,
            format_name="research-workspace-migration-lock-v1",
            scope={"migration_id": migration_id, "actor": actor},
        ) as lease:
            if fault_hook is not None:
                fault_hook("after_lock_acquired", {"lease_id": lease.lease_id})
            yield lease
    except KernelLeaseError as exc:
        if exc.code == "kernel_lease_unsafe":
            raise MigrationExecutionError(
                "migration_lock_unsafe",
                "migration lease path is a symlink",
                artifact=lock_path,
            ) from exc
        if exc.code == "kernel_lease_busy":
            raise MigrationExecutionError(
                "migration_executor_busy",
                "migration artifact is already owned by another executor",
                artifact=paths.recovery / f"migration-{migration_id}",
            ) from exc
        raise


def _logical_digest_run_rows(stream: BinaryIO) -> Iterator[tuple[str, ...]]:
    for encoded in stream:
        yield tuple(loads_strict_json_bytes(encoded))


def _write_logical_digest_run(
    path: Path, rows: Iterable[tuple[str, ...]],
) -> None:
    # Canonical JSON escapes embedded newlines; the trailing newline belongs
    # only to private sort framing, never to the database digest.
    with path.open("xb") as stream:
        for row in rows:
            stream.write(canonical_json_bytes(row))
            stream.write(b"\n")


def _merge_logical_digest_runs(paths: tuple[Path, ...], target: Path) -> None:
    with ExitStack() as stack:
        rows = tuple(
            _logical_digest_run_rows(stack.enter_context(path.open("rb")))
            for path in paths
        )
        # Compare repr strings, not SQL values or JSON-escaped encodings.
        _write_logical_digest_run(target, heapq.merge(*rows))


def _sorted_logical_digest_rows(
    connection: sqlite3.Connection, table: str, scratch: Path,
) -> Iterator[tuple[str, ...]]:
    """Sort one table with bounded chunks/fan-in, retaining row multiplicity."""

    chunk: list[tuple[str, ...]] = []
    chunk_bytes = 0
    run_count = 0
    input_rows = 0
    report_preparation_progress("logical_digest_sort_rows")
    with closing(connection.execute(f'SELECT * FROM "{table}"')) as cursor:
        for record in cursor:
            row = tuple(repr(value) for value in tuple(record))
            row_bytes = sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row)
            if chunk and chunk_bytes + sys.getsizeof(chunk) + row_bytes > _LOGICAL_DIGEST_SORT_BYTES:
                chunk.sort()
                _write_logical_digest_run(scratch / f"0-{run_count}.jsonl", chunk)
                report_preparation_progress("logical_digest_sort_rows", input_rows)
                run_count += 1
                chunk.clear()
                chunk_bytes = 0
            chunk.append(row)
            chunk_bytes += row_bytes
            input_rows += 1
    if not run_count:
        chunk.sort()
        report_preparation_progress("logical_digest_sort_rows", input_rows, input_rows)
        yield from chunk
        return
    if chunk:
        chunk.sort()
        _write_logical_digest_run(scratch / f"0-{run_count}.jsonl", chunk)
        report_preparation_progress("logical_digest_sort_rows", input_rows)
        run_count += 1
        chunk.clear()

    level = 0
    # Counts and fixed names avoid retaining a path for every initial run.
    while run_count > _LOGICAL_DIGEST_MERGE_FAN_IN:
        merged_count = 0
        for start in range(0, run_count, _LOGICAL_DIGEST_MERGE_FAN_IN):
            paths = tuple(
                scratch / f"{level}-{ordinal}.jsonl"
                for ordinal in range(start, min(start + _LOGICAL_DIGEST_MERGE_FAN_IN, run_count))
            )
            target = scratch / f"{level + 1}-{merged_count}.jsonl"
            _merge_logical_digest_runs(paths, target)
            report_preparation_progress("logical_digest_sort_rows", input_rows)
            for path in paths:
                path.unlink()
            merged_count += 1
        run_count = merged_count
        level += 1
    paths = tuple(scratch / f"{level}-{ordinal}.jsonl" for ordinal in range(run_count))
    # Hash the final merge directly: do not write and reread another whole
    # table merely to feed its sorted rows to the caller.
    with ExitStack() as stack:
        rows = tuple(
            _logical_digest_run_rows(stack.enter_context(path.open("rb")))
            for path in paths
        )
        yield from heapq.merge(*rows)
    report_preparation_progress("logical_digest_sort_rows", input_rows, input_rows)
    for path in paths:
        path.unlink()


def _logical_database_digest(connection: sqlite3.Connection) -> str:
    """Bind the unchanged logical JSON contract without retaining the database.

    Memory depends on one sort chunk, bounded merge fan-in and the largest row
    (including sort/JSON scratch), not all application rows. Disk scratch is
    private and outside protected migration artifacts. Python exceptions and
    Python-level interruption close streams before cleanup; caller transaction
    ownership remains unchanged. This is not a hard process-RSS ceiling.
    """

    completed_rows = 0
    report_preparation_progress("logical_digest_rows")
    tables = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    if _LOGICAL_DIGEST_SORT_BYTES < 1 or _LOGICAL_DIGEST_MERGE_FAN_IN < 2:
        raise ValueError("logical digest sort controls are invalid")
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    hasher = hashlib.sha256()
    hasher.update(b'{"schema_object_sha256":')
    hasher.update(canonical_json_bytes(schema_object_digest(connection)))
    hasher.update(b',"tables":[')
    with tempfile.TemporaryDirectory(prefix="rh-logical-digest-") as temporary:
        scratch = Path(temporary)
        for table_ordinal, table in enumerate(tables):
            if '"' in table:
                raise MigrationExecutionError(
                    "migration_source_invalid", "unsafe table identity"
                )
            if table_ordinal:
                hasher.update(b",")
            hasher.update(b'{"rows":[')
            with closing(_sorted_logical_digest_rows(connection, table, scratch)) as rows:
                for row_ordinal, row in enumerate(rows):
                    if row_ordinal:
                        hasher.update(b",")
                    hasher.update(canonical_json_bytes(row))
                    completed_rows += 1
                    if completed_rows % 256 == 0:
                        report_preparation_progress("logical_digest_rows", completed_rows)
            hasher.update(b'],"table":')
            hasher.update(canonical_json_bytes(table))
            hasher.update(b"}")
    hasher.update(b'],"user_version":')
    hasher.update(canonical_json_bytes(user_version))
    hasher.update(b"}")
    report_preparation_progress("logical_digest_rows", completed_rows, completed_rows)
    return hasher.hexdigest()


def _read_only_logical_digest(database: Path) -> str:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0)
    try:
        connection.row_factory = sqlite3.Row
        return _logical_database_digest(connection)
    finally:
        connection.close()


def _validate_plan(
    plan: OfflineMigrationPlan, backup: BackupSet
) -> tuple[BackupSet, str]:
    """Compare the plan with its accepted BackupSet, without rebuilding its proof.

    The effectful owner checks current source bytes/custody under its lease.
    Nested result/copy readers consume that same exact manifest binding.
    """
    if not isinstance(plan, OfflineMigrationPlan):
        raise MigrationExecutionError(
            "migration_plan_required", "executor requires OfflineMigrationPlan"
        )
    if not isinstance(backup, BackupSet):
        raise MigrationExecutionError(
            "verified_backup_required", "executor requires BackupSet"
        )
    if plan.target_schema_version == 9:
        raise MigrationExecutionError(
            "specialized_dot6_successor_upgrader_required",
            "schema 9 requires the specialized local dot6 successor upgrader",
        )
    verified = backup
    if plan.automatic or plan.downgrade:
        raise MigrationExecutionError(
            "automatic_migration_forbidden",
            "migration must be explicit and forward-only",
        )
    if RecoveryLifecycle(plan.lifecycle) not in {
        RecoveryLifecycle.OFFLINE,
        RecoveryLifecycle.QUIESCED,
    }:
        raise MigrationExecutionError(
            "migration_requires_offline", "migration requires exclusive pre-start state"
        )
    store = verified.manifest.store
    if store.writer_lifecycle not in {
        RecoveryLifecycle.OFFLINE.value,
        RecoveryLifecycle.QUIESCED.value,
    }:
        raise MigrationExecutionError(
            "migration_requires_offline", "verified source was not quiescent"
        )
    if store.current_writer_epoch is not None:
        raise MigrationExecutionError(
            "migration_requires_offline", "verified source retains a writer epoch"
        )
    if (
        plan.verified_backup_manifest_sha256 != verified.manifest.manifest_sha256
        or plan.current_schema_version != store.schema_version
        or plan.applied_history_sha256 != store.migration_history_sha256
    ):
        raise MigrationExecutionError(
            "migration_plan_stale", "plan differs from its verified backup"
        )
    if (
        plan.target_schema_version > SCHEMA_VERSION
        or plan.target_schema_version <= plan.current_schema_version
        or plan.target_schema_version == 11
        or plan.current_schema_version == 11
    ):
        raise MigrationExecutionError(
            "migration_target_invalid",
            "plan target is outside registered forward authority",
        )
    if plan.target_schema_version == 7 and (
        plan.current_schema_version != 6
        or store.operating_mode != "mission_runtime"
        or store.root_digest_version != 3
        or store.current_writer_epoch is not None
        or store.writer_lifecycle
        not in {
            RecoveryLifecycle.OFFLINE.value,
            RecoveryLifecycle.QUIESCED.value,
        }
    ):
        raise MigrationExecutionError(
            "migration_target_invalid",
            "schema 7 requires an exact quiesced schema-6 Mission/root3 source",
        )
    if plan.target_schema_version == 8 and (
        plan.current_schema_version != 7
        or store.operating_mode != "mission_runtime"
        or store.root_digest_version != 4
        or store.current_writer_epoch is not None
        or store.writer_lifecycle
        not in {
            RecoveryLifecycle.OFFLINE.value,
            RecoveryLifecycle.QUIESCED.value,
        }
    ):
        raise MigrationExecutionError(
            "migration_target_invalid",
            "schema 8 requires an exact quiesced schema-7 Mission/root4 source",
        )
    if plan.target_schema_version == 10 and (
        plan.current_schema_version != 9
        or store.operating_mode != "mission_runtime"
        or store.root_digest_version != 5
    ):
        raise MigrationExecutionError(
            "migration_target_invalid",
            "schema10 requires an exact quiesced schema9 Mission/root5 source",
        )
    if plan.target_schema_version == 12 and (
        plan.current_schema_version != 10
        or store.operating_mode != "mission_runtime"
        or store.root_digest_version != 6
    ):
        raise MigrationExecutionError(
            "migration_target_invalid",
            "schema12 requires an exact quiesced schema10 Mission/root6 source",
        )
    registered = {item.version: item for item in load_migrations()}
    expected_pending = tuple(
        (version, registered[version].name, registered[version].digest_sha256)
        for version in range(
            plan.current_schema_version + 1, plan.target_schema_version + 1
        )
        if version in registered
    )
    if plan.pending_migrations != expected_pending:
        raise MigrationExecutionError(
            "migration_plan_stale", "pending suffix differs from package authority"
        )
    plan_sha256 = _digest(canonical_json_bytes(_plan_payload(plan)))
    return verified, plan_sha256


def _artifact_paths(
    verified: BackupSet, migration_id: str
) -> tuple[WorkspacePaths, Path]:
    if not _MIGRATION_ID_RE.fullmatch(migration_id):
        raise MigrationExecutionError(
            "migration_id_invalid", "migration ID is not portable"
        )
    workspace_root = verified.path.resolve(strict=True).parent.parent
    try:
        paths = WorkspacePaths.from_root(workspace_root)
        paths.revalidate_physical(require_root=True)
    except WorkspacePathError as exc:
        raise MigrationExecutionError("migration_path_invalid", str(exc)) from exc
    if paths.backups.resolve(strict=True) != verified.path.resolve(strict=True).parent:
        raise MigrationExecutionError(
            "migration_path_invalid", "backup is outside the owning workspace"
        )
    artifact = (paths.recovery / f"migration-{migration_id}").resolve(strict=False)
    if artifact.parent != paths.recovery.resolve(strict=False):
        raise MigrationExecutionError(
            "migration_path_invalid", "migration artifact escaped recovery"
        )
    return paths, artifact


def _prepare_artifact(
    *,
    paths: WorkspacePaths,
    artifact: Path,
    verified: BackupSet,
    plan: OfflineMigrationPlan,
    plan_sha256: str,
    lease_id: str,
    fault_hook: Callable[[str, Mapping[str, Any]], None] | None,
) -> tuple[Path, dict[str, Any]]:
    database = artifact / "workspace.sqlite3"
    state_path = artifact / "migration_state.json"
    source = verified.path / "workspace.sqlite3"
    source_sha256 = verified.manifest.sqlite_sha256
    source_length = verified.manifest.sqlite_length
    if not artifact.exists():
        source_logical_sha256 = _read_only_logical_digest(source)
        stage_prefix = f".stage-{artifact.name}-"
        for candidate in paths.recovery.iterdir():
            if not candidate.name.startswith(stage_prefix):
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                raise MigrationExecutionError(
                    "migration_artifact_invalid",
                    "staged migration artifact is not a plain directory",
                    artifact=candidate,
                )
            shutil.rmtree(candidate)
        stage = paths.recovery / f"{stage_prefix}{lease_id}"
        stage_database = stage / database.name
        stage_state = stage / state_path.name
        published = False
        try:
            stage.mkdir(mode=0o700)
            if fault_hook is not None:
                fault_hook("after_artifact_stage_created", {"stage": stage.name})
            report_preparation_progress("database_copy", 0, 1)
            shutil.copyfile(source, stage_database)
            os.chmod(
                stage_database,
                stat.S_IREAD | stat.S_IWRITE if os.name == "nt" else 0o600,
            )
            with stage_database.open("r+b") as handle:
                os.fsync(handle.fileno())
            report_preparation_progress("database_copy", 1, 1)
            if _sha256_file(stage_database) != (source_sha256, source_length):
                raise MigrationExecutionError(
                    "migration_artifact_invalid",
                    "staged database differs from the verified source bytes",
                    artifact=stage,
                )
            state = {
                "format": "research-workspace-offline-migration-v2",
                "migration_id": artifact.name.removeprefix("migration-"),
                "plan_sha256": plan_sha256,
                "backup_manifest_sha256": verified.manifest.manifest_sha256,
                "source_sqlite_sha256": source_sha256,
                "source_sqlite_length": source_length,
                "source_logical_sha256": source_logical_sha256,
                "source_schema_version": plan.current_schema_version,
                "target_schema_version": plan.target_schema_version,
                "attempt_count": 0,
                "active_attempt_id": None,
                "status": "ready",
                "selected_live_root": False,
                "mission_auto_resume": False,
                "canonical_effect": "none",
            }
            _write_exclusive(stage_state, state)
            _fsync_directory(stage)
            if fault_hook is not None:
                fault_hook(
                    "after_initial_state_fsynced",
                    {
                        "stage": stage.name,
                        "source_logical_sha256": source_logical_sha256,
                    },
                )
            os.rename(stage, artifact)
            published = True
            _fsync_directory(paths.recovery)
            if fault_hook is not None:
                fault_hook("after_artifact_published", {"artifact": artifact.name})
            return database, state
        finally:
            if not published and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    if (
        artifact.is_symlink()
        or not artifact.is_dir()
        or database.is_symlink()
        or not database.is_file()
    ):
        raise MigrationExecutionError(
            "migration_artifact_invalid",
            "migration artifact is not a plain recovery tree",
            artifact=artifact,
        )
    state = _read_object(state_path, code="migration_artifact_invalid")
    expected = {
        "format": "research-workspace-offline-migration-v2",
        "migration_id": artifact.name.removeprefix("migration-"),
        "plan_sha256": plan_sha256,
        "backup_manifest_sha256": verified.manifest.manifest_sha256,
        "source_sqlite_sha256": source_sha256,
        "source_sqlite_length": source_length,
        "source_schema_version": plan.current_schema_version,
        "target_schema_version": plan.target_schema_version,
        "selected_live_root": False,
        "mission_auto_resume": False,
        "canonical_effect": "none",
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise MigrationExecutionError(
            "migration_artifact_stale",
            "artifact differs from plan or backup",
            artifact=artifact,
        )
    if re.fullmatch(r"[0-9a-f]{64}", str(state.get("source_logical_sha256", ""))) is None:
        raise MigrationExecutionError(
            "migration_artifact_invalid",
            "artifact lacks its exact source logical digest",
            artifact=artifact,
        )
    if state.get("status") not in {
        "ready",
        "running",
        "retryable",
        "reconciliation_required",
        "passed",
    }:
        raise MigrationExecutionError(
            "migration_reconciliation_required",
            "migration artifact is not safely retryable",
            artifact=artifact,
        )
    _validated_atomic_write_temporaries(artifact)
    return database, state


def _expected_histories(
    verified: BackupSet,
    plan: OfflineMigrationPlan,
) -> tuple[dict[int, tuple[str, str]], dict[int, tuple[str, str]]]:
    source = {
        item.version: (item.name, item.digest_sha256)
        for item in verified.manifest.store.migration_history
    }
    target = dict(source)
    target.update(
        {
            version: (name, digest_sha256)
            for version, name, digest_sha256 in plan.pending_migrations
        }
    )
    return source, target


def _attempt_receipts(
    *,
    artifact: Path,
    migration_id: str,
    state: Mapping[str, Any],
    plan: OfflineMigrationPlan,
    verified: BackupSet,
    plan_sha256: str,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    started: dict[int, dict[str, Any]] = {}
    outcomes: dict[int, dict[str, Any]] = {}
    for path in artifact.glob("attempt-*.json"):
        started_match = _ATTEMPT_FILE_RE.fullmatch(path.name)
        outcome_match = _OUTCOME_FILE_RE.fullmatch(path.name)
        if started_match is None and outcome_match is None:
            raise MigrationExecutionError(
                "migration_reconciliation_required",
                "migration artifact contains an unknown attempt receipt",
                artifact=artifact,
            )
        number = int((started_match or outcome_match).group("number"))
        payload = _read_object(path, code="migration_reconciliation_required")
        expected = {
            "format": "research-workspace-migration-attempt-v1",
            "attempt_id": f"{migration_id}.attempt.{number:04d}",
            "plan_sha256": plan_sha256,
            "backup_manifest_sha256": verified.manifest.manifest_sha256,
            "source_schema_version": plan.current_schema_version,
            "target_schema_version": plan.target_schema_version,
            "selected_live_root": False,
            "mission_auto_resume": False,
            "canonical_effect": "none",
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise MigrationExecutionError(
                "migration_reconciliation_required",
                "attempt receipt differs from the exact plan or no-effect boundary",
                artifact=artifact,
            )
        started_keys = {
            *expected,
            "actor",
            "started_at",
            "status",
        }
        if started_match is not None:
            if (
                set(payload) != started_keys
                or payload.get("status") != "started"
                or not isinstance(payload.get("actor"), str)
                or not payload.get("actor")
                or not isinstance(payload.get("started_at"), str)
                or not payload.get("started_at")
            ):
                raise MigrationExecutionError(
                    "migration_reconciliation_required",
                    "started receipt is not a complete immutable attempt witness",
                    artifact=artifact,
                )
        else:
            status = payload.get("status")
            common_outcome_keys = {
                *started_keys,
                "completed_at",
                "pre_logical_sha256",
                "post_logical_sha256",
                "schema_version",
            }
            expected_outcome_keys = (
                {
                    *common_outcome_keys,
                    "schema_object_sha256",
                    "migration_history_count",
                }
                if status == "passed"
                else {
                    *common_outcome_keys,
                    "error_code",
                    "error_type",
                    "rollback_exact",
                }
            )
            if (
                status not in {"passed", "failed", "interrupted_before_commit"}
                or set(payload) != expected_outcome_keys
                or not isinstance(payload.get("completed_at"), str)
                or not payload.get("completed_at")
                or any(
                    re.fullmatch(r"[0-9a-f]{64}", str(payload.get(key, "")))
                    is None
                    for key in ("pre_logical_sha256", "post_logical_sha256")
                )
                or isinstance(payload.get("schema_version"), bool)
                or not isinstance(payload.get("schema_version"), int)
                or (
                    status == "passed"
                    and (
                        re.fullmatch(
                            r"[0-9a-f]{64}",
                            str(payload.get("schema_object_sha256", "")),
                        )
                        is None
                        or isinstance(payload.get("migration_history_count"), bool)
                        or not isinstance(payload.get("migration_history_count"), int)
                    )
                )
                or (
                    status != "passed"
                    and (
                        not isinstance(payload.get("error_code"), str)
                        or not payload.get("error_code")
                        or not isinstance(payload.get("error_type"), str)
                        or not payload.get("error_type")
                        or payload.get("rollback_exact") is not True
                    )
                )
            ):
                raise MigrationExecutionError(
                    "migration_reconciliation_required",
                    "outcome receipt is not one exact closed attempt witness",
                    artifact=artifact,
                )
        target = started if started_match is not None else outcomes
        target[number] = payload
    if tuple(sorted(started)) != tuple(range(1, len(started) + 1)):
        raise MigrationExecutionError(
            "migration_reconciliation_required",
            "migration attempt sequence is not contiguous",
            artifact=artifact,
        )
    if any(number not in started for number in outcomes):
        raise MigrationExecutionError(
            "migration_reconciliation_required",
            "migration outcome lacks its started receipt",
            artifact=artifact,
        )
    attempt_count = state.get("attempt_count")
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 0
        or attempt_count > len(started)
    ):
        raise MigrationExecutionError(
            "migration_artifact_invalid",
            "migration state attempt count is invalid",
            artifact=artifact,
        )
    return started, outcomes


def _source_attempt_receipt_status(
    *,
    started_receipts: Mapping[int, Mapping[str, Any]],
    outcome_receipts: Mapping[int, Mapping[str, Any]],
    source_logical_sha256: str,
    source_schema_version: int,
) -> tuple[bool, tuple[int, ...]]:
    invalid_outcome = any(
        not (
            outcome.get("status") in {"failed", "interrupted_before_commit"}
            and outcome.get("rollback_exact") is True
            and outcome.get("pre_logical_sha256") == source_logical_sha256
            and outcome.get("post_logical_sha256") == source_logical_sha256
            and outcome.get("schema_version") == source_schema_version
        )
        for outcome in outcome_receipts.values()
    )
    missing_outcomes = tuple(
        number for number in started_receipts if number not in outcome_receipts
    )
    return invalid_outcome, missing_outcomes


@dataclass(frozen=True, slots=True)
class _DatabaseReconciliation:
    classification: str
    logical_sha256: str
    history_count: int
    verification: Any | None = None
    execution: Mapping[str, Any] | None = None


def _expected_target_schema_object_digest(target_schema_version: int) -> str:
    """Use the schema owner's exact registered historical object contract."""

    return _expected_schema_object_digest_for_version(target_schema_version)


def _verify_target_schema(
    connection: sqlite3.Connection,
    *,
    target_schema_version: int,
    allow_sealed_delete: bool = False,
) -> SchemaVerification:
    """Verify an exact registered migration target, including historical ones."""

    if target_schema_version == SCHEMA_VERSION:
        return verify_schema(
            connection,
            allow_sealed_delete=allow_sealed_delete,
        )
    migrations = tuple(
        item for item in load_migrations() if item.version <= target_schema_version
    )
    if not migrations or migrations[-1].version != target_schema_version:
        raise WorkspaceSchemaError("migration target is not registered")
    expected_history = {
        item.version: (item.name, item.digest_sha256) for item in migrations
    }
    if _applied_migrations(connection) != expected_history:
        raise WorkspaceSchemaError(
            "applied migration set does not match target package authority"
        )
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != target_schema_version:
        raise WorkspaceSchemaError("workspace user_version does not match migration target")
    if allow_sealed_delete:
        profile = connection_profile(connection)
        if (
            not profile.foreign_keys
            or profile.journal_mode != "delete"
            or profile.synchronous != 2
            or not _has_sealed_compatible_busy_retry_profile(profile)
            or profile.trusted_schema
        ):
            raise WorkspaceSchemaError(
                "sealed migration target connection profile is invalid"
            )
    else:
        profile = verify_connection_profile(connection)
    actual_object_digest = schema_object_digest(connection)
    if actual_object_digest != _expected_target_schema_object_digest(
        target_schema_version
    ):
        raise WorkspaceSchemaError("workspace target schema object digest mismatch")
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise WorkspaceSchemaError(f"SQLite integrity_check failed: {integrity}")
    violations = tuple(
        tuple(row) for row in connection.execute("PRAGMA foreign_key_check")
    )
    if violations:
        raise WorkspaceSchemaError("workspace contains foreign key violations")
    return SchemaVerification(
        schema_version=version,
        migration_digest=migration_set_digest(migrations),
        schema_object_digest=actual_object_digest,
        integrity_check=integrity,
        foreign_key_violations=violations,
        connection_profile=profile,
    )


def _inspect_database(
    connection: sqlite3.Connection,
    *,
    state: Mapping[str, Any],
    plan: OfflineMigrationPlan,
    verified: BackupSet,
    plan_sha256: str,
    allow_sealed_delete: bool = False,
    applied: _AppliedOfflineMigration | None = None,
) -> _DatabaseReconciliation:
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    violations = tuple(connection.execute("PRAGMA foreign_key_check"))
    if integrity != "ok" or violations:
        return _DatabaseReconciliation("ambiguous", "", 0)
    logical_sha256 = _logical_database_digest(connection)
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    metadata_row = connection.execute(
        "SELECT * FROM workspace_metadata WHERE singleton = 1"
    ).fetchone()
    metadata_version = (
        int(metadata_row["schema_version"]) if metadata_row is not None else -1
    )
    history = _applied_migrations(connection)
    source_history, target_history = _expected_histories(verified, plan)
    execution_rows: tuple[sqlite3.Row, ...] = ()
    marker_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_execution'"
    ).fetchone()
    if marker_table is not None:
        execution_rows = tuple(
            connection.execute(
                "SELECT * FROM migration_execution WHERE target_schema_version = ?",
                (plan.target_schema_version,),
            )
        )
    if (
        user_version == plan.current_schema_version
        and metadata_version == plan.current_schema_version
        and history == source_history
        and not execution_rows
        and logical_sha256 == state["source_logical_sha256"]
    ):
        return _DatabaseReconciliation("exact_source", logical_sha256, len(history))
    if (
        user_version == plan.target_schema_version
        and metadata_version == plan.target_schema_version
        and history == target_history
        and len(execution_rows) == 1
    ):
        row = dict(execution_rows[0])
        expected = {
            "execution_id": f"migration.{state['migration_id']}",
            "source_schema_version": plan.current_schema_version,
            "target_schema_version": plan.target_schema_version,
            "verified_backup_manifest_sha256": verified.manifest.manifest_sha256,
            "applied_history_sha256": plan.applied_history_sha256,
            "plan_sha256": plan_sha256,
            "status": "applied",
            "canonical_effect": "none",
        }
        if any(row.get(key) != value for key, value in expected.items()):
            return _DatabaseReconciliation("ambiguous", logical_sha256, len(history))
        try:
            verification = _verify_target_schema(
                connection,
                target_schema_version=plan.target_schema_version,
                allow_sealed_delete=allow_sealed_delete,
            )
            if plan.target_schema_version in {7, 10, 12}:
                from .workspace_store import (
                    _validated_schema_v7_root_transition,
                    _validated_schema_v10_root_transition,
                    _validated_schema_v12_root_transition,
                )
                persisted_execution = _OfflineMigrationExecution(
                    execution_id=str(row["execution_id"]),
                    attempt_id=str(row["attempt_id"]),
                    source_schema_version=int(row["source_schema_version"]),
                    target_schema_version=int(row["target_schema_version"]),
                    verified_backup_manifest_sha256=str(
                        row["verified_backup_manifest_sha256"]
                    ),
                    applied_history_sha256=str(row["applied_history_sha256"]),
                    plan_sha256=str(row["plan_sha256"]),
                    started_at=str(row["started_at"]),
                    completed_at=str(row["completed_at"]),
                )
                if applied is not None:
                    # The transaction owner already audited this exact target.
                    # Reject any intervening write rather than reconstruct it.
                    transition = applied.validated_transition(connection, persisted_execution)
                else:
                    transition = (
                        _validated_schema_v7_root_transition(
                            connection,
                            execution=persisted_execution,
                        )
                        if plan.target_schema_version == 7
                        else {
                            10: _validated_schema_v10_root_transition,
                            12: _validated_schema_v12_root_transition,
                        }[plan.target_schema_version](
                            connection,
                            execution=persisted_execution,
                            _verified_schema=verification,
                        )
                    )
                source_store = verified.manifest.store
                if metadata_row is None or (
                    transition.migration_execution_id
                    != persisted_execution.execution_id
                    or transition.migration_attempt_id
                    != persisted_execution.attempt_id
                    or transition.project_commit_no
                    != int(metadata_row["current_project_commit"])
                    or transition.root_digest
                    != str(metadata_row["current_root_digest"])
                    or str(metadata_row["project_id"]) != source_store.project_id
                    or str(metadata_row["root_identity"])
                    != source_store.root_identity
                    or str(metadata_row["application_version"])
                    != APPLICATION_VERSION
                    or str(metadata_row["lifecycle"])
                    != source_store.writer_lifecycle
                    or str(metadata_row["lifecycle"])
                    not in {
                        RecoveryLifecycle.OFFLINE.value,
                        RecoveryLifecycle.QUIESCED.value,
                    }
                    or metadata_row["current_writer_epoch"] is not None
                    or str(metadata_row["canonical_authority_digest"])
                    != source_store.canonical_authority_digest
                    or str(metadata_row["operating_mode"]) != "mission_runtime"
                    or int(metadata_row["root_digest_version"])
                    != (4 if plan.target_schema_version == 7 else 6)
                    or str(metadata_row["transition_head_digest"])
                    != transition.transition_digest
                    or str(metadata_row["updated_at"])
                    != persisted_execution.completed_at
                    or transition.source_project_commit
                    != source_store.project_commit_id
                    or transition.source_root_digest
                    != source_store.project_root_digest
                    or transition.source_transition_head_digest
                    != source_store.transition_head
                    or transition.canonical_authority_digest
                    != source_store.canonical_authority_digest
                    or transition.writer_epoch
                    != source_store.writer_epoch_high_watermark + 1
                ):
                    raise WorkspaceSchemaError(
                        "root-transition witness differs from the committed Store"
                    )
            elif plan.target_schema_version == 8:
                from .workspace_store import _validated_schema_v8_root_refresh

                persisted_execution = _OfflineMigrationExecution(
                    execution_id=str(row["execution_id"]),
                    attempt_id=str(row["attempt_id"]),
                    source_schema_version=int(row["source_schema_version"]),
                    target_schema_version=int(row["target_schema_version"]),
                    verified_backup_manifest_sha256=str(
                        row["verified_backup_manifest_sha256"]
                    ),
                    applied_history_sha256=str(row["applied_history_sha256"]),
                    plan_sha256=str(row["plan_sha256"]),
                    started_at=str(row["started_at"]),
                    completed_at=str(row["completed_at"]),
                )
                _validated_schema_v8_root_refresh(
                    connection,
                    execution=persisted_execution,
                )
        except Exception:
            return _DatabaseReconciliation("ambiguous", logical_sha256, len(history))
        return _DatabaseReconciliation(
            "exact_commit",
            logical_sha256,
            len(history),
            verification,
            row,
        )
    return _DatabaseReconciliation("ambiguous", logical_sha256, len(history))


def _validated_exact_commit_receipt(
    *,
    migration_id: str,
    state: Mapping[str, Any],
    plan: OfflineMigrationPlan,
    started_receipts: Mapping[int, Mapping[str, Any]],
    outcome_receipts: Mapping[int, Mapping[str, Any]],
    inspection: _DatabaseReconciliation,
    require_outcome: bool,
) -> tuple[int, str, Mapping[str, Any], Mapping[str, Any] | None]:
    return _validated_commit_receipt_values(
        migration_id=migration_id, state=state, plan=plan,
        started_receipts=started_receipts, outcome_receipts=outcome_receipts,
        marker=dict(inspection.execution or {}),
        logical_sha256=inspection.logical_sha256,
        schema_object_sha256=(
            None if inspection.verification is None
            else inspection.verification.schema_object_digest
        ),
        history_count=inspection.history_count,
        require_outcome=require_outcome,
    )


def _validated_commit_receipt_values(
    *,
    migration_id: str,
    state: Mapping[str, Any],
    plan: OfflineMigrationPlan,
    started_receipts: Mapping[int, Mapping[str, Any]],
    outcome_receipts: Mapping[int, Mapping[str, Any]],
    marker: Mapping[str, Any],
    logical_sha256: str,
    schema_object_sha256: str | None,
    history_count: int,
    require_outcome: bool,
) -> tuple[int, str, Mapping[str, Any], Mapping[str, Any] | None]:
    attempt_id = str(marker.get("attempt_id", ""))
    attempt_match = re.fullmatch(
        re.escape(migration_id) + r"\.attempt\.(?P<number>[0-9]{4})",
        attempt_id,
    )
    attempt_number = int(attempt_match.group("number")) if attempt_match else -1
    started = started_receipts.get(attempt_number)
    exact_receipt = bool(
        started is not None
        and attempt_number == len(started_receipts)
        and started.get("started_at") == marker.get("started_at")
    )
    exact_receipt = exact_receipt and all(
        number in outcome_receipts
        and outcome_receipts[number].get("status")
        in {"failed", "interrupted_before_commit"}
        and outcome_receipts[number].get("rollback_exact") is True
        and outcome_receipts[number].get("pre_logical_sha256")
        == state["source_logical_sha256"]
        and outcome_receipts[number].get("post_logical_sha256")
        == state["source_logical_sha256"]
        and outcome_receipts[number].get("schema_version")
        == plan.current_schema_version
        for number in range(1, attempt_number)
    )
    outcome = outcome_receipts.get(attempt_number)
    if outcome is not None:
        exact_receipt = exact_receipt and all(
            (
                outcome.get("status") == "passed",
                outcome.get("completed_at") == marker.get("completed_at"),
                outcome.get("pre_logical_sha256")
                == state["source_logical_sha256"],
                outcome.get("post_logical_sha256") == logical_sha256,
                outcome.get("schema_version") == plan.target_schema_version,
                schema_object_sha256 is not None,
                outcome.get("schema_object_sha256") == schema_object_sha256,
                outcome.get("migration_history_count") == history_count,
            )
        )
    if require_outcome and outcome is None:
        exact_receipt = False
    if not exact_receipt or started is None:
        raise MigrationExecutionError(
            "migration_reconciliation_required",
            "committed database lacks one exact matching attempt witness",
        )
    return attempt_number, attempt_id, started, outcome


def _seal_database(connection: sqlite3.Connection, *, artifact: Path) -> None:
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    mode = str(
        connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    ).casefold()
    if mode != "delete":
        raise MigrationExecutionError(
            "migration_verification_failed",
            "side-by-side image did not seal",
            artifact=artifact,
        )


def _result_from_values(
    *,
    migration_id: str,
    artifact: Path,
    database: Path,
    attempt_id: str,
    plan: OfflineMigrationPlan,
    plan_sha256: str,
    verified: BackupSet,
    pre_logical_sha256: str,
    post_logical_sha256: str,
    history_count: int,
    artifact_files: tuple[tuple[str, str, int], ...],
) -> OfflineMigrationResult:
    values = {
        "migration_id": migration_id,
        "artifact": artifact,
        "database": database,
        "attempt_id": attempt_id,
        "source_schema_version": plan.current_schema_version,
        "target_schema_version": plan.target_schema_version,
        "plan_sha256": plan_sha256,
        "backup_manifest_sha256": verified.manifest.manifest_sha256,
        "pre_logical_sha256": pre_logical_sha256,
        "post_logical_sha256": post_logical_sha256,
        "migration_history_count": history_count,
    }
    provisional = OfflineMigrationResult(
        **values,
        _issuer_token=_MIGRATION_RESULT_ISSUER_TOKEN,
        _artifact_files=artifact_files,
    )
    return OfflineMigrationResult(
        **values,
        _issuer_token=_MIGRATION_RESULT_ISSUER_TOKEN,
        _issuer_seal=_offline_migration_result_seal(provisional.to_payload(), artifact_files),
        _artifact_files=artifact_files,
    )


def _inspect_offline_migration_source_binding(
    *,
    migration_id: str,
    plan: OfflineMigrationPlan,
    backup: BackupSet,
) -> tuple[BackupSet, str, Path, Path, dict[str, Any]]:
    """Authenticate saved passed-state/source correspondence, not the target.

    The caller supplies its physically inspected source BackupSet. This read
    compares the retained source byte identity; it does not reconstruct source
    semantics, issue a migration result or establish target integrity.
    """

    verified, plan_sha256 = _validate_plan(plan, backup)
    _paths, expected_artifact = _artifact_paths(verified, migration_id)
    artifact = expected_artifact.resolve(strict=True)
    database = (artifact / "workspace.sqlite3").resolve(strict=True)
    if (
        artifact != expected_artifact
        or database != artifact / "workspace.sqlite3"
        or artifact.is_symlink()
        or not artifact.is_dir()
        or database.is_symlink()
        or not database.is_file()
    ):
        raise MigrationExecutionError(
            "migration_result_stale",
            "issued migration result no longer names its exact closed artifact",
            artifact=artifact,
        )
    state_path = artifact / "migration_state.json"
    state = _read_object(state_path, code="migration_result_stale")
    expected_state = {
        "format": "research-workspace-offline-migration-v2",
        "migration_id": migration_id,
        "plan_sha256": plan_sha256,
        "backup_manifest_sha256": verified.manifest.manifest_sha256,
        "source_sqlite_sha256": verified.manifest.sqlite_sha256,
        "source_sqlite_length": verified.manifest.sqlite_length,
        "source_schema_version": plan.current_schema_version,
        "target_schema_version": plan.target_schema_version,
        "active_attempt_id": None,
        "status": "passed",
        "selected_live_root": False,
        "mission_auto_resume": False,
        "canonical_effect": "none",
    }
    allowed_state_keys = {
        *expected_state,
        "attempt_count",
        "last_attempt_id",
        "successful_attempt_id",
        "pre_logical_sha256",
        "post_logical_sha256",
        "source_logical_sha256",
    }
    attempt_count = state.get("attempt_count")
    if (
        set(state) - allowed_state_keys
        or any(state.get(key) != value for key, value in expected_state.items())
        or isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 1
        or re.fullmatch(r"[0-9a-f]{64}", str(state.get("source_logical_sha256", ""))) is None
    ):
        raise MigrationExecutionError(
            "migration_result_stale",
            "issued migration state differs from its exact passed result",
            artifact=artifact,
        )
    return verified, plan_sha256, artifact, database, state


def _reissue_validated_offline_migration_result(
    *,
    migration_id: str,
    plan: OfflineMigrationPlan,
    backup: BackupSet,
    require_read_only: bool = True,
) -> OfflineMigrationResult:
    """Reissue one exact closed passed artifact without creating an Attempt."""

    verified, plan_sha256, artifact, database, state = _inspect_offline_migration_source_binding(
        migration_id=migration_id, plan=plan, backup=backup,
    )
    database_bytes = _sha256_file(database)
    connection = open_snapshot_connection(database)
    try:
        inspection = _inspect_database(
            connection,
            state=state,
            plan=plan,
            verified=verified,
            plan_sha256=plan_sha256,
            allow_sealed_delete=True,
        )
    finally:
        connection.close()
    return _issue_result_from_inspection(
        migration_id=migration_id,
        plan=plan,
        backup=verified,
        inspection=inspection,
        database_bytes=database_bytes,
        require_read_only=require_read_only,
    )


def _passed_artifact_files(
    artifact: Path, *, require_read_only: bool
) -> tuple[tuple[str, str, int], ...]:
    """Bind the existing result to its exact flat files, without a new record."""

    if artifact.is_symlink() or not artifact.is_dir() or (
        require_read_only and stat.S_IMODE(artifact.stat().st_mode) & stat.S_IWUSR
    ):
        raise MigrationExecutionError(
            "migration_result_stale", "migration artifact custody changed", artifact=artifact
        )
    files = []
    for path in sorted(artifact.iterdir()):
        if path.is_symlink() or not path.is_file() or (
            require_read_only and stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR
        ):
            raise MigrationExecutionError(
                "migration_result_stale", "migration artifact file custody changed", artifact=artifact
            )
        files.append((path.name, *_sha256_file(path)))
    return tuple(files)


def _issue_result_from_inspection(
    *,
    migration_id: str,
    plan: OfflineMigrationPlan,
    backup: BackupSet,
    inspection: _DatabaseReconciliation,
    database_bytes: tuple[str, int],
    require_read_only: bool,
) -> OfflineMigrationResult:
    """Finish one earned database validation through receipt/protection changes."""

    verified, plan_sha256, artifact, database, state = _inspect_offline_migration_source_binding(
        migration_id=migration_id, plan=plan, backup=backup,
    )
    attempt_count = state["attempt_count"]
    started_receipts, outcome_receipts = _attempt_receipts(
        artifact=artifact, migration_id=migration_id, state=state,
        plan=plan, verified=verified, plan_sha256=plan_sha256,
    )
    if inspection.classification != "exact_commit":
        raise MigrationExecutionError(
            "migration_result_stale",
            "issued migration database is no longer the exact committed target",
            artifact=artifact,
        )
    attempt_number, attempt_id, _started, _outcome = _validated_exact_commit_receipt(
        migration_id=migration_id,
        state=state,
        plan=plan,
        started_receipts=started_receipts,
        outcome_receipts=outcome_receipts,
        inspection=inspection,
        require_outcome=True,
    )
    expected_files = {
        "workspace.sqlite3",
        "migration_state.json",
        *(f"attempt-{number:04d}.started.json" for number in started_receipts),
        *(f"attempt-{number:04d}.outcome.json" for number in outcome_receipts),
    }
    artifact_files = _passed_artifact_files(artifact, require_read_only=require_read_only)
    actual_files = {item[0] for item in artifact_files}
    expected_previous_attempt = (
        None
        if int(attempt_count) == 1
        else f"{migration_id}.attempt.{int(attempt_count) - 1:04d}"
    )
    actual_previous_attempt = state.get("last_attempt_id")
    if (
        actual_files != expected_files
        or ("workspace.sqlite3", *database_bytes) not in artifact_files
        or int(attempt_count) != len(started_receipts)
        or attempt_number != int(attempt_count)
        or state.get("successful_attempt_id") != attempt_id
        or actual_previous_attempt != expected_previous_attempt
        or state.get("pre_logical_sha256") != state["source_logical_sha256"]
        or state.get("post_logical_sha256") != inspection.logical_sha256
    ):
        raise MigrationExecutionError(
            "migration_result_stale",
            "passed migration artifact differs from its exact result witness",
            artifact=artifact,
        )
    return _result_from_values(
        migration_id=migration_id,
        artifact=artifact,
        database=database,
        attempt_id=attempt_id,
        plan=plan,
        plan_sha256=plan_sha256,
        verified=verified,
        pre_logical_sha256=str(state["source_logical_sha256"]),
        post_logical_sha256=inspection.logical_sha256,
        history_count=inspection.history_count,
        artifact_files=artifact_files,
    )


def _verify_issued_offline_migration_result(
    result: OfflineMigrationResult,
    *,
    plan: OfflineMigrationPlan,
    backup: BackupSet,
) -> OfflineMigrationResult:
    """Check that the already audited result still names the same protected bytes."""

    if type(result) is not OfflineMigrationResult:
        raise MigrationExecutionError(
            "migration_result_not_issued",
            "offline migration result must be the exact executor result type",
        )
    result.verify_issued()
    verified, plan_sha256 = _validate_plan(plan, backup)
    _paths, artifact = _artifact_paths(verified, result.migration_id)
    if (
        result.plan_sha256 != plan_sha256
        or result.backup_manifest_sha256 != verified.manifest.manifest_sha256
        or result.source_schema_version != plan.current_schema_version
        or result.target_schema_version != plan.target_schema_version
        or result.artifact != artifact
        or not result._artifact_files
        or _passed_artifact_files(artifact, require_read_only=True) != result._artifact_files
    ):
        raise MigrationExecutionError(
            "migration_result_stale",
            "issued migration result differs from its current artifact witness",
            artifact=result.artifact,
        )
    return result


def _inspect_passed_offline_migration(
    *,
    migration_id: str,
    plan: OfflineMigrationPlan,
    backup: BackupSet,
    target_backup: BackupSet,
) -> dict[str, Any]:
    """Read saved correspondence for an already inspected completed backup pair.

    The caller owns the independently pinned physical source/target inspection.
    Match the retained migration image to the target bytes, then read its one
    execution marker and existing receipts. This is not a fresh semantic audit
    or an issued executor capability, and creates no durable proof record.
    """

    verified, plan_sha256, artifact, database, state = _inspect_offline_migration_source_binding(
        migration_id=migration_id, plan=plan, backup=backup,
    )
    if type(target_backup) is not BackupSet:
        raise MigrationExecutionError("verified_backup_required", "target requires BackupSet")
    source_manifest = verified.manifest
    target_manifest = target_backup.manifest
    _source_history, target_history = _expected_histories(verified, plan)
    if (
        target_backup.path == verified.path
        or target_manifest.store.schema_version != plan.target_schema_version
        or target_manifest.store.project_id != source_manifest.store.project_id
        or target_manifest.store.root_identity != source_manifest.store.root_identity
        or target_manifest.store.canonical_authority_digest
        != source_manifest.store.canonical_authority_digest
        or target_manifest.canonical != source_manifest.canonical
        or target_manifest.closure != source_manifest.closure
        or target_manifest.cas_inventory_contract != _COMMITTED_BLOB_CAS_INVENTORY
        or target_manifest.evidence_inventory != source_manifest.evidence_inventory
        or target_manifest.evidence_metadata_sha256 != source_manifest.evidence_metadata_sha256
        or target_manifest.deletion_directives != source_manifest.deletion_directives
        or target_manifest.tombstones != source_manifest.tombstones
        or target_manifest.store.current_writer_epoch is not None
        or target_manifest.store.writer_lifecycle != source_manifest.store.writer_lifecycle
        or {
            item.version: (item.name, item.digest_sha256)
            for item in target_manifest.store.migration_history
        } != target_history
        or target_manifest.store.schema_object_digest
        != _expected_target_schema_object_digest(plan.target_schema_version)
    ):
        raise MigrationExecutionError(
            "migration_result_stale", "prepared backup pair differs from the migration plan", artifact=artifact
        )
    started_receipts, outcome_receipts = _attempt_receipts(
        artifact=artifact, migration_id=migration_id, state=state,
        plan=plan, verified=verified, plan_sha256=plan_sha256,
    )
    expected_files = {
        "workspace.sqlite3", "migration_state.json",
        *(f"attempt-{number:04d}.started.json" for number in started_receipts),
        *(f"attempt-{number:04d}.outcome.json" for number in outcome_receipts),
    }
    files = _passed_artifact_files(artifact, require_read_only=True)
    if {item[0] for item in files} != expected_files or (
        "workspace.sqlite3", target_manifest.sqlite_sha256, target_manifest.sqlite_length
    ) not in files:
        raise MigrationExecutionError(
            "migration_result_stale", "prepared target differs from the protected migration bytes", artifact=artifact
        )
    from .workspace_recovery import _store_binding_from_retained_snapshot
    from .workspace_store import WorkspaceStore

    # Project the exact matched image, not a self-declared target manifest. The
    # constructor does not open a writer or audit; the reader only derives the
    # existing metadata/count binding from the already authenticated bytes.
    projected_store = _store_binding_from_retained_snapshot(
        WorkspaceStore(WorkspacePaths.from_root(target_backup.path), target_manifest.store.project_id),
        database,
    )
    if projected_store != target_manifest.store:
        raise MigrationExecutionError(
            "migration_result_stale", "prepared target Store metadata differs from its migration bytes", artifact=artifact
        )
    connection = open_snapshot_connection(database)
    try:
        rows = tuple(connection.execute(
            "SELECT * FROM migration_execution WHERE target_schema_version = ?",
            (plan.target_schema_version,),
        ))
    finally:
        connection.close()
    marker = dict(rows[0]) if len(rows) == 1 else {}
    expected_marker = {
        "execution_id": f"migration.{migration_id}",
        "source_schema_version": plan.current_schema_version,
        "target_schema_version": plan.target_schema_version,
        "verified_backup_manifest_sha256": source_manifest.manifest_sha256,
        "applied_history_sha256": plan.applied_history_sha256,
        "plan_sha256": plan_sha256,
        "status": "applied",
        "canonical_effect": "none",
    }
    if any(marker.get(key) != value for key, value in expected_marker.items()):
        raise MigrationExecutionError(
            "migration_result_stale", "prepared target execution marker differs", artifact=artifact
        )
    attempt_number, attempt_id, _started, _outcome = _validated_commit_receipt_values(
        migration_id=migration_id, state=state, plan=plan,
        started_receipts=started_receipts, outcome_receipts=outcome_receipts,
        marker=marker, logical_sha256=str(state.get("post_logical_sha256", "")),
        schema_object_sha256=target_manifest.store.schema_object_digest,
        history_count=len(target_history), require_outcome=True,
    )
    if (
        state["attempt_count"] != attempt_number
        or state.get("successful_attempt_id") != attempt_id
        or state.get("last_attempt_id") != (
            None if attempt_number == 1 else f"{migration_id}.attempt.{attempt_number - 1:04d}"
        )
        or state.get("pre_logical_sha256") != state["source_logical_sha256"]
    ):
        raise MigrationExecutionError(
            "migration_result_stale", "prepared migration passed state differs", artifact=artifact
        )
    return OfflineMigrationResult(
        migration_id=migration_id, artifact=artifact, database=database,
        attempt_id=attempt_id, source_schema_version=plan.current_schema_version,
        target_schema_version=plan.target_schema_version, plan_sha256=plan_sha256,
        backup_manifest_sha256=source_manifest.manifest_sha256,
        pre_logical_sha256=str(state["source_logical_sha256"]),
        post_logical_sha256=str(state["post_logical_sha256"]),
        migration_history_count=len(target_history),
    ).to_payload()


def _verify_offline_migration_result_copy(
    result: OfflineMigrationResult,
    *,
    plan: OfflineMigrationPlan,
    backup: BackupSet,
    database: Path,
) -> OfflineMigrationResult:
    """Verify one immutable copied target against the exact live result witness."""

    current = _verify_issued_offline_migration_result(
        result,
        plan=plan,
        backup=backup,
    )
    copied = Path(database).resolve(strict=True)
    if (
        copied == current.database
        or copied.is_symlink()
        or not copied.is_file()
        or bool(stat.S_IMODE(copied.stat().st_mode) & stat.S_IWUSR)
    ):
        raise MigrationExecutionError(
            "migration_result_copy_invalid",
            "migration result copy must be a distinct immutable regular file",
            artifact=copied.parent,
        )
    expected_database = next(
        (item[1:] for item in current._artifact_files if item[0] == "workspace.sqlite3"),
        None,
    )
    if _sha256_file(copied) != expected_database:
        raise MigrationExecutionError(
            "migration_result_copy_invalid",
            "copied database differs from the exact issued migration result",
            artifact=copied.parent,
        )
    return current


def _protect_artifact(artifact: Path) -> None:
    for item in sorted(
        artifact.rglob("*"), key=lambda value: len(value.parts), reverse=True
    ):
        if item.is_file():
            os.chmod(item, stat.S_IREAD if os.name == "nt" else 0o400)
        elif item.is_dir():
            os.chmod(item, stat.S_IREAD | stat.S_IEXEC if os.name == "nt" else 0o500)
    os.chmod(artifact, stat.S_IREAD | stat.S_IEXEC if os.name == "nt" else 0o500)


def execute_offline_migration(
    *,
    plan: OfflineMigrationPlan,
    backup: BackupSet,
    migration_id: str,
    actor: str,
    fault_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
    observer: WorkspaceObserver = NOOP_WORKSPACE_OBSERVER,
    observation_environment: ObservationEnvironment = ObservationEnvironment.LOCAL,
) -> OfflineMigrationResult:
    """Execute one explicit migration in an inactive side-by-side artifact."""

    if not isinstance(actor, str) or not actor.strip():
        raise MigrationExecutionError(
            "migration_actor_required", "migration actor is required"
        )
    observation_environment = ObservationEnvironment(observation_environment)
    verified, plan_sha256 = _validate_plan(plan, backup)
    paths, artifact = _artifact_paths(verified, migration_id)
    paths.recovery.mkdir(mode=0o700, exist_ok=True)
    with _acquire_migration_lease(
        paths,
        migration_id=migration_id,
        actor=actor.strip(),
        fault_hook=fault_hook,
    ) as lease:
        from .workspace_recovery import _revalidate_unchanged_portable_backup

        try:
            locked_verified = _revalidate_unchanged_portable_backup(verified)
        except RecoveryError as exc:
            raise MigrationExecutionError(exc.code, str(exc), artifact=backup.path) from exc
        locked_verified, locked_plan_sha256 = _validate_plan(plan, locked_verified)
        if (
            locked_verified != verified
            or locked_plan_sha256 != plan_sha256
        ):
            raise MigrationExecutionError(
                "migration_plan_stale",
                "migration source or plan changed after lease acquisition",
                artifact=artifact,
            )
        verified = locked_verified
        database, state = _prepare_artifact(
            paths=paths,
            artifact=artifact,
            verified=verified,
            plan=plan,
            plan_sha256=plan_sha256,
            lease_id=lease.lease_id,
            fault_hook=fault_hook,
        )
        started_receipts, outcome_receipts = _attempt_receipts(
            artifact=artifact,
            migration_id=migration_id,
            state=state,
            plan=plan,
            verified=verified,
            plan_sha256=plan_sha256,
        )
        attempt_number = len(started_receipts) + 1
        if state.get("status") == "passed":
            _reconcile_atomic_write_temporaries(artifact)
            result = _reissue_validated_offline_migration_result(
                migration_id=migration_id,
                plan=plan,
                backup=verified,
                require_read_only=False,
            )
            _protect_artifact(artifact)
            _verify_issued_offline_migration_result(
                result,
                plan=plan,
                backup=verified,
            )
            emit_event_safely(
                observer,
                kind=EventKind.MIGRATION_CHANGED,
                occurred_at=_now(),
                environment=observation_environment,
                attributes={
                    "migration_id": migration_id,
                    "attempt_id": result.attempt_id,
                    "stage": "verification",
                    "status": "passed_reissued",
                },
            )
            return result
        if attempt_number > _MAX_ATTEMPT_NUMBER:
            try:
                exhausted_connection = open_snapshot_connection(database)
                try:
                    exhausted_inspection = _inspect_database(
                        exhausted_connection,
                        state=state,
                        plan=plan,
                        verified=verified,
                        plan_sha256=plan_sha256,
                        allow_sealed_delete=(
                            connection_profile(exhausted_connection).journal_mode
                            == "delete"
                        ),
                    )
                finally:
                    exhausted_connection.close()
            except MigrationExecutionError:
                raise
            except Exception as exc:
                raise MigrationExecutionError(
                    "migration_reconciliation_required",
                    "exhausted migration attempt sequence could not be classified",
                    artifact=artifact,
                ) from exc
            if exhausted_inspection.classification == "exact_source":
                invalid_outcome, missing_outcomes = _source_attempt_receipt_status(
                    started_receipts=started_receipts,
                    outcome_receipts=outcome_receipts,
                    source_logical_sha256=str(state["source_logical_sha256"]),
                    source_schema_version=plan.current_schema_version,
                )
                if invalid_outcome or (
                    missing_outcomes
                    and missing_outcomes != (len(started_receipts),)
                ):
                    raise MigrationExecutionError(
                        "migration_reconciliation_required",
                        "attempt receipts do not agree with the exact source image",
                        artifact=artifact,
                    )
                raise MigrationExecutionError(
                    "migration_attempt_limit_reached",
                    "migration attempt sequence exhausted its fixed four-digit identity space",
                    artifact=artifact,
                )
            if exhausted_inspection.classification != "exact_commit":
                raise MigrationExecutionError(
                    "migration_reconciliation_required",
                    "exhausted migration artifact is neither the exact source nor the exact committed target",
                    artifact=artifact,
                )

        _reconcile_atomic_write_temporaries(artifact)
        connection = _connect_workspace(database)
        inspection = _inspect_database(
            connection,
            state=state,
            plan=plan,
            verified=verified,
            plan_sha256=plan_sha256,
        )

        if inspection.classification == "exact_commit":
            marker = dict(inspection.execution or {})
            try:
                (
                    attempt_number,
                    attempt_id,
                    started,
                    existing_outcome,
                ) = _validated_exact_commit_receipt(
                    migration_id=migration_id,
                    state=state,
                    plan=plan,
                    started_receipts=started_receipts,
                    outcome_receipts=outcome_receipts,
                    inspection=inspection,
                    require_outcome=False,
                )
            except MigrationExecutionError:
                connection.close()
                _replace_state(
                    artifact / "migration_state.json",
                    {
                        **state,
                        "status": "reconciliation_required",
                        "active_attempt_id": None,
                        "selected_live_root": False,
                        "mission_auto_resume": False,
                        "canonical_effect": "none",
                    },
                )
                raise MigrationExecutionError(
                    "migration_reconciliation_required",
                    "committed database lacks one exact matching attempt witness",
                    artifact=artifact,
                )
            assert inspection.verification is not None
            _seal_database(connection, artifact=artifact)
            connection.close()
            database_bytes = _sha256_file(database)
            if existing_outcome is None:
                existing_outcome = {
                    **started,
                    "completed_at": marker["completed_at"],
                    "status": "passed",
                    "pre_logical_sha256": state["source_logical_sha256"],
                    "post_logical_sha256": inspection.logical_sha256,
                    "schema_version": inspection.verification.schema_version,
                    "schema_object_sha256": inspection.verification.schema_object_digest,
                    "migration_history_count": inspection.history_count,
                }
                _write_exclusive(
                    artifact / f"attempt-{attempt_number:04d}.outcome.json",
                    existing_outcome,
                )
            passed_state = {
                    **state,
                    "attempt_count": attempt_number,
                    "active_attempt_id": None,
                    "status": "passed",
                    "successful_attempt_id": attempt_id,
                    "pre_logical_sha256": state["source_logical_sha256"],
                    "post_logical_sha256": inspection.logical_sha256,
                    "selected_live_root": False,
                    "mission_auto_resume": False,
                    "canonical_effect": "none",
                }
            if attempt_number == 1:
                passed_state.pop("last_attempt_id", None)
            else:
                passed_state["last_attempt_id"] = (
                    f"{migration_id}.attempt.{attempt_number - 1:04d}"
                )
            _replace_state(artifact / "migration_state.json", passed_state)
            if fault_hook is not None:
                fault_hook(
                    "after_migration_passed_state",
                    {"attempt_id": attempt_id},
                )
            result = _issue_result_from_inspection(
                migration_id=migration_id,
                plan=plan,
                backup=verified,
                inspection=inspection,
                database_bytes=database_bytes,
                require_read_only=False,
            )
            _protect_artifact(artifact)
            _verify_issued_offline_migration_result(
                result,
                plan=plan,
                backup=verified,
            )
            emit_event_safely(
                observer,
                kind=EventKind.MIGRATION_CHANGED,
                occurred_at=_now(),
                environment=observation_environment,
                attributes={
                    "migration_id": migration_id,
                    "attempt_id": attempt_id,
                    "stage": "verification",
                    "status": "passed",
                },
            )
            return result

        if inspection.classification != "exact_source":
            connection.close()
            _replace_state(
                artifact / "migration_state.json",
                {
                    **state,
                    "status": "reconciliation_required",
                    "active_attempt_id": None,
                    "selected_live_root": False,
                    "mission_auto_resume": False,
                    "canonical_effect": "none",
                },
            )
            raise MigrationExecutionError(
                "migration_reconciliation_required",
                "side-by-side image is neither the exact source nor the exact committed target",
                artifact=artifact,
            )

        _seal_database(connection, artifact=artifact)
        connection.close()
        source_logical_sha256 = inspection.logical_sha256
        invalid_outcome, missing_outcomes = _source_attempt_receipt_status(
            started_receipts=started_receipts,
            outcome_receipts=outcome_receipts,
            source_logical_sha256=source_logical_sha256,
            source_schema_version=plan.current_schema_version,
        )
        if invalid_outcome or (
            missing_outcomes and missing_outcomes != (len(started_receipts),)
        ):
            _replace_state(
                artifact / "migration_state.json",
                {
                    **state,
                    "status": "reconciliation_required",
                    "active_attempt_id": None,
                    "selected_live_root": False,
                    "mission_auto_resume": False,
                    "canonical_effect": "none",
                },
            )
            raise MigrationExecutionError(
                "migration_reconciliation_required",
                "attempt receipts do not agree with the exact source image",
                artifact=artifact,
            )
        if missing_outcomes:
            interrupted_number = missing_outcomes[0]
            interrupted = started_receipts[interrupted_number]
            _write_exclusive(
                artifact / f"attempt-{interrupted_number:04d}.outcome.json",
                {
                    **interrupted,
                    "completed_at": _now(),
                    "status": "interrupted_before_commit",
                    "error_code": "migration_process_interrupted",
                    "error_type": "ProcessExit",
                    "rollback_exact": True,
                    "pre_logical_sha256": source_logical_sha256,
                    "post_logical_sha256": source_logical_sha256,
                    "schema_version": plan.current_schema_version,
                },
            )
            state = {
                **state,
                "attempt_count": interrupted_number,
                "active_attempt_id": None,
                "status": "retryable",
                "last_attempt_id": interrupted["attempt_id"],
                "pre_logical_sha256": source_logical_sha256,
                "post_logical_sha256": source_logical_sha256,
                "selected_live_root": False,
                "mission_auto_resume": False,
                "canonical_effect": "none",
            }
            _replace_state(artifact / "migration_state.json", state)

        attempt_id = f"{migration_id}.attempt.{attempt_number:04d}"
        started_at = _now()
        started_path = artifact / f"attempt-{attempt_number:04d}.started.json"
        outcome_path = artifact / f"attempt-{attempt_number:04d}.outcome.json"
        started = {
            "format": "research-workspace-migration-attempt-v1",
            "attempt_id": attempt_id,
            "actor": actor.strip(),
            "started_at": started_at,
            "plan_sha256": plan_sha256,
            "backup_manifest_sha256": verified.manifest.manifest_sha256,
            "source_schema_version": plan.current_schema_version,
            "target_schema_version": plan.target_schema_version,
            "status": "started",
            "selected_live_root": False,
            "mission_auto_resume": False,
            "canonical_effect": "none",
        }
        _write_exclusive(started_path, started)
        _replace_state(
            artifact / "migration_state.json",
            {
                **state,
                "attempt_count": attempt_number,
                "active_attempt_id": attempt_id,
                "status": "running",
                "selected_live_root": False,
                "mission_auto_resume": False,
                "canonical_effect": "none",
            },
        )
        emit_event_safely(
            observer,
            kind=EventKind.MIGRATION_CHANGED,
            occurred_at=_now(),
            environment=observation_environment,
            attributes={
                "migration_id": migration_id,
                "attempt_id": attempt_id,
                "stage": "execution",
                "status": "started",
            },
        )

        connection = None
        passed_state_persisted = False
        pre_logical_sha256 = source_logical_sha256
        try:
            connection = _connect_workspace(database)
            current = _inspect_database(
                connection,
                state=state,
                plan=plan,
                verified=verified,
                plan_sha256=plan_sha256,
            )
            if current.classification != "exact_source":
                raise MigrationExecutionError(
                    "migration_reconciliation_required",
                    "side-by-side image changed before execution",
                    artifact=artifact,
                )
            pre_logical_sha256 = current.logical_sha256
            execution = _OfflineMigrationExecution(
                execution_id=f"migration.{migration_id}",
                attempt_id=attempt_id,
                source_schema_version=plan.current_schema_version,
                target_schema_version=plan.target_schema_version,
                verified_backup_manifest_sha256=verified.manifest.manifest_sha256,
                applied_history_sha256=plan.applied_history_sha256,
                plan_sha256=plan_sha256,
                started_at=started_at,
                completed_at=_now(),
            )
            from .workspace_recovery import _offline_migration_root_identity

            capability = _offline_migration_capability(
                database,
                root_identity=_offline_migration_root_identity(verified, paths),
            )
            applied = _apply_offline_migration_plan(
                connection,
                pending_migrations=plan.pending_migrations,
                execution=execution,
                capability=capability,
                fault_hook=fault_hook,
            )
            if fault_hook is not None:
                fault_hook(
                    "after_migration_commit",
                    {
                        "attempt_id": attempt_id,
                        "target_schema_version": plan.target_schema_version,
                    },
                )
            committed = _inspect_database(
                connection,
                state=state,
                plan=plan,
                verified=verified,
                plan_sha256=plan_sha256,
                applied=applied,
            )
            if (
                committed.classification != "exact_commit"
                or committed.verification is None
            ):
                raise MigrationExecutionError(
                    "migration_verification_failed",
                    "committed migration did not match its exact witness",
                    artifact=artifact,
                )
            _seal_database(connection, artifact=artifact)
            connection.close()
            connection = None
            database_bytes = _sha256_file(database)
            outcome = {
                **started,
                "completed_at": execution.completed_at,
                "status": "passed",
                "pre_logical_sha256": pre_logical_sha256,
                "post_logical_sha256": committed.logical_sha256,
                "schema_version": committed.verification.schema_version,
                "schema_object_sha256": committed.verification.schema_object_digest,
                "migration_history_count": committed.history_count,
            }
            _write_exclusive(outcome_path, outcome)
            if fault_hook is not None:
                fault_hook("after_migration_outcome", {"attempt_id": attempt_id})
            passed_state = {
                    **state,
                    "attempt_count": attempt_number,
                    "active_attempt_id": None,
                    "status": "passed",
                    "successful_attempt_id": attempt_id,
                    "pre_logical_sha256": pre_logical_sha256,
                    "post_logical_sha256": committed.logical_sha256,
                    "selected_live_root": False,
                    "mission_auto_resume": False,
                    "canonical_effect": "none",
                }
            if attempt_number == 1:
                passed_state.pop("last_attempt_id", None)
            else:
                passed_state["last_attempt_id"] = (
                    f"{migration_id}.attempt.{attempt_number - 1:04d}"
                )
            _replace_state(artifact / "migration_state.json", passed_state)
            passed_state_persisted = True
            if fault_hook is not None:
                fault_hook(
                    "after_migration_passed_state",
                    {"attempt_id": attempt_id},
                )
            result = _issue_result_from_inspection(
                migration_id=migration_id,
                plan=plan,
                backup=verified,
                inspection=committed,
                database_bytes=database_bytes,
                require_read_only=False,
            )
            _protect_artifact(artifact)
            _verify_issued_offline_migration_result(
                result,
                plan=plan,
                backup=verified,
            )
            emit_event_safely(
                observer,
                kind=EventKind.MIGRATION_CHANGED,
                occurred_at=_now(),
                environment=observation_environment,
                attributes={
                    "migration_id": migration_id,
                    "attempt_id": attempt_id,
                    "stage": "verification",
                    "status": "passed",
                },
            )
            return result
        except BaseException as exc:
            error_code = (
                exc.code
                if isinstance(exc, MigrationExecutionError)
                else "migration_execution_failed"
            )
            if passed_state_persisted:
                emit_event_safely(
                    observer,
                    kind=EventKind.MIGRATION_CHANGED,
                    occurred_at=_now(),
                    level=ObservationLevel.ERROR,
                    environment=observation_environment,
                    attributes={
                        "migration_id": migration_id,
                        "attempt_id": attempt_id,
                        "stage": "verification",
                        "status": "passed_unprotected",
                        "reason_code": error_code,
                    },
                )
                raise MigrationExecutionError(
                    "migration_result_finalization_interrupted",
                    "migration passed durably but closed-tree protection was interrupted",
                    artifact=artifact,
                ) from exc
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                finally:
                    connection.close()
                    connection = None

            classified: _DatabaseReconciliation | None = None
            current_version = -1
            classification_error: BaseException | None = None
            try:
                recovery_connection = _connect_workspace(database)
                try:
                    classified = _inspect_database(
                        recovery_connection,
                        state=state,
                        plan=plan,
                        verified=verified,
                        plan_sha256=plan_sha256,
                    )
                    current_version = int(
                        recovery_connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    _seal_database(recovery_connection, artifact=artifact)
                finally:
                    recovery_connection.close()
            except BaseException as recovery_exc:
                classification_error = recovery_exc

            if classified is None or classified.classification not in {
                "exact_source",
                "exact_commit",
            }:
                current_logical = (
                    "" if classified is None else classified.logical_sha256
                )
                _replace_state(
                    artifact / "migration_state.json",
                    {
                        **state,
                        "attempt_count": attempt_number,
                        "active_attempt_id": None,
                        "status": "reconciliation_required",
                        "last_attempt_id": attempt_id,
                        "pre_logical_sha256": pre_logical_sha256,
                        "post_logical_sha256": current_logical,
                        "selected_live_root": False,
                        "mission_auto_resume": False,
                        "canonical_effect": "none",
                    },
                )
                emit_event_safely(
                    observer,
                    kind=EventKind.MIGRATION_CHANGED,
                    occurred_at=_now(),
                    level=ObservationLevel.ERROR,
                    environment=observation_environment,
                    attributes={
                        "migration_id": migration_id,
                        "attempt_id": attempt_id,
                        "stage": "execution",
                        "status": "reconciliation_required",
                        "reason_code": error_code,
                    },
                )
                raise MigrationExecutionError(
                    "migration_reconciliation_required",
                    "failed migration image could not be classified as the exact source or target",
                    artifact=artifact,
                ) from (classification_error or exc)

            if classified.classification == "exact_commit":
                _replace_state(
                    artifact / "migration_state.json",
                    {
                        **state,
                        "attempt_count": attempt_number,
                        "active_attempt_id": None,
                        "status": "reconciliation_required",
                        "last_attempt_id": attempt_id,
                        "pre_logical_sha256": pre_logical_sha256,
                        "post_logical_sha256": classified.logical_sha256,
                        "selected_live_root": False,
                        "mission_auto_resume": False,
                        "canonical_effect": "none",
                    },
                )
                emit_event_safely(
                    observer,
                    kind=EventKind.MIGRATION_CHANGED,
                    occurred_at=_now(),
                    level=ObservationLevel.ERROR,
                    environment=observation_environment,
                    attributes={
                        "migration_id": migration_id,
                        "attempt_id": attempt_id,
                        "stage": "execution",
                        "status": "reconciliation_required",
                        "reason_code": error_code,
                    },
                )
                raise MigrationExecutionError(
                    "migration_reconciliation_required",
                    "migration committed exactly; the next exact invocation will finalize its result",
                    artifact=artifact,
                ) from exc

            current_logical = classified.logical_sha256
            rollback_exact = True
            outcome = {
                **started,
                "completed_at": _now(),
                "status": "failed",
                "error_code": error_code,
                "error_type": type(exc).__name__,
                "rollback_exact": rollback_exact,
                "pre_logical_sha256": pre_logical_sha256,
                "post_logical_sha256": current_logical,
                "schema_version": current_version,
            }
            if not outcome_path.exists():
                _write_exclusive(outcome_path, outcome)
            _replace_state(
                artifact / "migration_state.json",
                {
                    **state,
                    "attempt_count": attempt_number,
                    "active_attempt_id": None,
                    "status": "retryable",
                    "last_attempt_id": attempt_id,
                    "pre_logical_sha256": pre_logical_sha256,
                    "post_logical_sha256": current_logical,
                    "selected_live_root": False,
                    "mission_auto_resume": False,
                    "canonical_effect": "none",
                },
            )
            emit_event_safely(
                observer,
                kind=EventKind.MIGRATION_CHANGED,
                occurred_at=_now(),
                level=ObservationLevel.ERROR,
                environment=observation_environment,
                attributes={
                    "migration_id": migration_id,
                    "attempt_id": attempt_id,
                    "stage": "execution",
                    "status": "failed",
                    "reason_code": error_code,
                },
            )
            raise MigrationExecutionError(
                error_code, str(exc), artifact=artifact
            ) from exc
