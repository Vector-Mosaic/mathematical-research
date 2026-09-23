"""Bound backup and fail-closed recovery for the inactive Research Workspace.

Every current backup is derived from a verified :class:`WorkspaceStore`, every
CAS object referenced by its copied SQLite snapshot, one exact typed Evidence
closure retained as semantic metadata, and a fresh live-canonical readback.
The ordinary API prepares a takeover permit without selecting a live root.
One closed owner operation may also import an exact portable checkpoint source
at a caller-selected target and consume that permit in the Store transaction.
Neither route resumes a Mission, migrates a schema, or mutates canonical
mathematics.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .canonical_snapshot import CANONICAL_STATE_REPO_PATH
from .evidence_store import (
    BlobRecord,
    ClosureManifest,
    ClosureMember,
    ClosureReference,
    DeletionDirective,
    EvidenceCAS,
    EvidenceStoreError,
    EvidenceTombstone,
    cas_relative_path,
    verify_closure_manifest,
)
from .json_support import (
    canonical_json_bytes,
    loads_strict_json_bytes,
    loads_strict_json_object,
)
from .observability import (
    NOOP_WORKSPACE_OBSERVER,
    EventKind,
    ObservationEnvironment,
    WorkspaceObserver,
    emit_event_safely,
)
from .research_model import deep_freeze, deep_thaw
from .workspace_schema import (
    APPLICATION_VERSION,
    is_direct_mission_generation,
    load_migrations,
    open_immutable_snapshot_connection,
    open_snapshot_connection,
    schema_object_digest,
)
from .workspace_paths import (
    WorkspacePathError,
    WorkspacePaths,
    attest_current_principal,
    stable_principal_owner_binding,
)
from .workspace_operation_lease import (
    HeldKernelLease,
    KernelLeaseError,
    acquire_kernel_lease,
    valid_kernel_lease_id,
)
from .workspace_store import (
    WorkspaceBackupReport,
    WorkspaceCheckpointSource,
    WorkspaceIntegrityError,
    WorkspaceStore,
    _WRITER_DIGEST_COLUMNS,
    _HISTORICAL_SCHEMA6_IMMUTABLE_SNAPSHOT_AUTHORITY,
    _HISTORICAL_SCHEMA7_IMMUTABLE_SNAPSHOT_AUTHORITY,
    _HISTORICAL_SCHEMA8_IMMUTABLE_SNAPSHOT_AUTHORITY,
    _issue_recovery_backup_integrity_scope,
    _latest_mission_checkpoint_from_connection,
    _validated_journal_index,
    _validated_persisted_closure_authority_at_write,
    _validated_writer_epoch_tail,
    _writer_digest,
)


_RECOVERY_ISSUER_TOKEN = object()
_LEGACY_AUTHORITY_VECTOR_KEYS = {
    "source_class",
    "canonical_state_path",
    "canonical_state_sha256",
    "canonical_schema_version",
    "validation_contract_sha256",
    "campaign_path",
    "campaign_sha256",
    "protocol_path",
    "protocol_sha256",
    "state_schema_path",
    "state_schema_sha256",
    "workbench_schema_path",
    "workbench_schema_sha256",
    "architecture_sha256",
    "contracts_sha256",
    "status_sha256",
    "testing_sha256",
    "workbench_sha256",
    "workbench_record_revisions",
    "renderer_contract_version",
    "source_commit",
}
_LEGACY_VALIDATION_CONTRACT_PATHS = (
    "packages/research-core/research_core/json_support.py",
    "contracts/schemas/research_state.schema.json",
    "packages/research-core/research_core/validator.py",
)


def _legacy_validation_contract_digest(repo_root: Path) -> str:
    """Reproduce the retired unversioned vector digest for offline recovery only."""

    digest = hashlib.sha256()
    for repo_path in _LEGACY_VALIDATION_CONTRACT_PATHS:
        raw = (repo_root / repo_path).read_bytes()
        name = repo_path.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()
_RECOVERY_ISSUER_SECRET = os.urandom(32)


BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TAKEOVER_UNSEAL_MARKER = ".takeover-unseal.json"
_READ_ONLY_SEAL = "read_only_seal.json"
_FIXED_RESTORE_LOCK_FORMAT = "research-portable-fixed-target-restore-lock-v1"
_OPERATING_MODE_SCHEMA_VERSION = 4
_LEGACY_CLOSURE_CAS_INVENTORY = "selected_evidence_closure.v1"
_COMMITTED_BLOB_CAS_INVENTORY = "sqlite_committed_blob_inventory.v2"
_SYSTEMCTL = Path("/usr/bin/systemctl")
_POSIX_TEST = Path("/usr/bin/test")
_PORTABLE_TAKEOVER_CREATION_BASIS = "portable checkpoint-source takeover"
_PORTABLE_TAKEOVER_RESULT_SCHEMA = (
    "mathematical_research.portable_checkpoint_source_takeover_owner.v1"
)
_STAGING_IMPORT_LOCK_FORMAT = "research-portable-staging-import-lock-v1"
_STAGING_IMPORT_EVIDENCE_SCHEMA = (
    "mathematical_research.staging_portable_import.v1"
)
_STAGING_IMPORT_REPORT_SCHEMA = (
    "mathematical_research.staging_portable_import_report.v1"
)
_STAGING_MIGRATION_REPORT_SCHEMA = (
    "mathematical_research.staging_portable_migration_report.v2"
)
_STAGING_SCHEMA12_MIGRATION_REPORT_SCHEMA = (
    "mathematical_research.staging_portable_migration_report.v4"
)
_STAGING_MIGRATION_OPERATION_SCHEMA = (
    "mathematical_research.staging_portable_migration_operation.v1"
)
_STAGING_MIGRATION_OPERATION_PREFIX = "migration-"
_STAGING_MIGRATION_SCOPE_ISSUER = object()
# The v1 operation digest shipped with the historical theorem slug.  Keep that
# wire identity stable for replay while using the canonical Store identity for
# every semantic and data-plane check.
_STAGING_MIGRATION_OPERATION_PROJECT_ID_V1 = "riemann_hypothesis"
_RH_STAGING_PROJECT_ID = "project.riemann_hypothesis"
_STAGING_PATH_COMPONENT = "rh-staging"
_STAGING_WORKSPACES_COMPONENT = "workspaces"
_PRODUCTION_PATH_MARKERS = frozenset({"production", "prod", "rh-mission"})
_STAGING_CREDENTIAL_FILENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".git-credentials",
        "auth.json",
        "cookies.json",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
    }
)
_STAGING_FORBIDDEN_ENVIRONMENT_KEYS = (
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "CODEX_HOME",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "HCLOUD_TOKEN",
    "HETZNER_API_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
)
_STAGING_ENVIRONMENT_MARKER_KEYS = (
    "DEPLOYMENT_ENVIRONMENT",
    "RH_ENVIRONMENT",
    "RH_MISSION_ENVIRONMENT",
    "VECTOR_MOSAIC_ENVIRONMENT",
)
_PRODUCTION_ENVIRONMENT_MARKERS = frozenset({"live", "prod", "production"})


class RecoveryError(RuntimeError):
    """Fail-closed recovery error with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecoveryLifecycle(str, Enum):
    OFFLINE = "offline"
    RECOVERING = "recovering"
    VERIFIED_READ_ONLY = "verified_read_only"
    TAKEOVER_PENDING = "takeover_pending"
    ACTIVE = "active"
    DRAINING = "draining"
    QUIESCED = "quiesced"
    BLOCKED_READ_ONLY = "blocked_read_only"


@dataclass(frozen=True)
class AppliedMigrationBinding:
    """One migration row derived from the backed-up database itself."""

    version: int
    name: str
    digest_sha256: str
    applied_at: str

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version < 1:
            raise ValueError("applied migration version must be positive")
        if not self.name or not self.applied_at:
            raise ValueError("applied migration name and timestamp are required")
        _require_digest(self.digest_sha256, "applied migration digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "digest_sha256": self.digest_sha256,
            "applied_at": self.applied_at,
        }


# TAKEOVER_PENDING intentionally has no generic ACTIVE edge.  Only an explicit
# TakeoverPermit may be consumed by the writer-owned transaction.
ALLOWED_TRANSITIONS: Mapping[RecoveryLifecycle, frozenset[RecoveryLifecycle]] = {
    RecoveryLifecycle.OFFLINE: frozenset({RecoveryLifecycle.RECOVERING}),
    RecoveryLifecycle.RECOVERING: frozenset(
        {RecoveryLifecycle.VERIFIED_READ_ONLY, RecoveryLifecycle.BLOCKED_READ_ONLY}
    ),
    RecoveryLifecycle.VERIFIED_READ_ONLY: frozenset(
        {
            RecoveryLifecycle.TAKEOVER_PENDING,
            RecoveryLifecycle.QUIESCED,
            RecoveryLifecycle.BLOCKED_READ_ONLY,
        }
    ),
    RecoveryLifecycle.TAKEOVER_PENDING: frozenset(
        {RecoveryLifecycle.BLOCKED_READ_ONLY}
    ),
    RecoveryLifecycle.ACTIVE: frozenset(
        {RecoveryLifecycle.DRAINING, RecoveryLifecycle.BLOCKED_READ_ONLY}
    ),
    RecoveryLifecycle.DRAINING: frozenset(
        {RecoveryLifecycle.QUIESCED, RecoveryLifecycle.BLOCKED_READ_ONLY}
    ),
    RecoveryLifecycle.QUIESCED: frozenset({RecoveryLifecycle.OFFLINE}),
    RecoveryLifecycle.BLOCKED_READ_ONLY: frozenset({RecoveryLifecycle.RECOVERING}),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def _contained(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RecoveryError(
            "restore_path_escape", f"path escapes recovery root: {candidate}"
        ) from exc
    return resolved


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def _reject_fixed_target_alias_chain(path: Path) -> None:
    """Reject traversal and every existing reparse component without following it."""

    if any(part in {".", ".."} for part in path.parts):
        raise RecoveryError(
            "restore_path_invalid",
            "fixed restore target cannot contain traversal components",
        )
    parts = path.parts
    if not parts:
        raise RecoveryError("restore_path_invalid", "fixed restore target is empty")
    cursor = Path(parts[0])
    for part in parts[1:]:
        cursor /= part
        try:
            entry = os.lstat(cursor)
        except FileNotFoundError:
            # A descendant cannot exist without its first missing ancestor.
            break
        except OSError as exc:
            raise RecoveryError(
                "restore_path_invalid",
                "fixed restore target ancestry cannot be authenticated",
            ) from exc
        if stat.S_ISLNK(entry.st_mode) or bool(
            getattr(entry, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise RecoveryError(
                "restore_path_invalid",
                f"fixed restore target cannot traverse a link or reparse point: {cursor}",
            )


def _validated_fixed_target_path(
    value: Path | str,
) -> tuple[Path, Path, tuple[int, int]]:
    """Apply WorkspacePaths plain-root authority without deriving it through aliases."""

    requested = Path(value).expanduser()
    if not requested.is_absolute() or requested.name in {"", ".", ".."}:
        raise RecoveryError(
            "restore_path_invalid", "fixed restore target must be one absolute path"
        )
    _reject_fixed_target_alias_chain(requested)
    try:
        normalized = WorkspacePaths.from_root(requested).root
    except (OSError, WorkspacePathError) as exc:
        raise RecoveryError(
            "restore_path_invalid",
            "fixed restore target violates plain workspace-root authority",
        ) from exc
    # Recheck the lexical chain after WorkspacePaths validation so no alias can
    # be substituted across the authority handoff.
    _reject_fixed_target_alias_chain(requested)
    lexical = requested.absolute()
    try:
        same_parent = os.path.samefile(lexical.parent, normalized.parent)
    except OSError as exc:
        raise RecoveryError(
            "restore_path_invalid",
            "fixed restore parent identity cannot be compared",
        ) from exc
    if (
        not same_parent
        or os.path.normcase(lexical.name) != os.path.normcase(normalized.name)
    ):
        raise RecoveryError(
            "restore_path_invalid",
            "fixed restore target is not physically normalized",
        )
    lexical = normalized
    parent = normalized.parent
    try:
        parent_entry = os.lstat(parent)
    except OSError as exc:
        raise RecoveryError(
            "restore_path_invalid", "fixed restore parent is unavailable"
        ) from exc
    if not stat.S_ISDIR(parent_entry.st_mode) or _is_reparse_point(parent):
        raise RecoveryError(
            "restore_path_invalid", "fixed restore parent must be an ordinary directory"
        )
    return lexical, parent, (int(parent_entry.st_dev), int(parent_entry.st_ino))


def _assert_plain_tree(
    root: Path, *, code: str, require_independent_files: bool = False
) -> None:
    if _is_reparse_point(root) or not root.is_dir():
        raise RecoveryError(
            code, f"recovery-owned root is not a plain directory: {root}"
        )
    for item in root.rglob("*"):
        if _is_reparse_point(item):
            raise RecoveryError(
                code, f"recovery-owned tree contains a reparse point: {item}"
            )
        if require_independent_files:
            entry = item.lstat()
            if stat.S_ISDIR(entry.st_mode):
                continue
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise RecoveryError(
                    code,
                    f"recovery-owned tree contains a non-independent regular file: {item}",
                )


def _workspace_paths_for_backup(
    path: Path, backup_id: str | None = None
) -> WorkspacePaths:
    """Recover and validate the owning root from ``root/backups/<backup-id>``."""

    resolved = path.resolve(strict=False)
    if resolved.parent.name != "backups":
        raise RecoveryError(
            "backup_path_invalid", "backup must be under WorkspacePaths.backups"
        )
    paths = WorkspacePaths.from_root(resolved.parent.parent)
    paths.revalidate_physical(require_root=True)
    if resolved.parent != paths.backups.resolve(strict=True):
        raise RecoveryError(
            "backup_path_invalid", "backup parent is not the owning backups root"
        )
    if backup_id is not None and not (
        resolved.name == backup_id or resolved.name.startswith(f".stage-{backup_id}-")
    ):
        raise RecoveryError(
            "backup_path_invalid", "backup directory name is not bound to backup ID"
        )
    return paths


def _make_owner_read_only(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _make_owner_writable(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    os.chmod(path, mode | stat.S_IWUSR)


def _windows_acl_principal() -> str:
    result = subprocess.run(
        ["whoami"],
        check=False,
        capture_output=True,
        text=True,
    )
    principal = result.stdout.strip()
    if (
        result.returncode != 0
        or not principal
        or "\n" in principal
        or "\r" in principal
    ):
        raise RecoveryError(
            "restore_not_physically_read_only",
            "unable to resolve the effective Windows identity for recovery ACLs",
        )
    return principal


def _apply_windows_write_deny(root: Path, *, deny_delete: bool = True) -> None:
    if os.name != "nt":
        return
    principal = _windows_acl_principal()
    denied_rights = "WD,AD,WEA,WA,DC,DE" if deny_delete else "WD,AD,WEA,WA"
    result = subprocess.run(
        [
            "icacls",
            str(root),
            "/deny",
            f"{principal}:(OI)(CI)({denied_rights})",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RecoveryError(
            "restore_not_physically_read_only",
            "unable to install the recovery-owned Windows write-deny ACL",
        )


def _remove_windows_write_deny(root: Path, *, recursive: bool = False) -> None:
    if os.name != "nt" or not root.exists():
        return
    command = ["icacls", str(root), "/remove:d", _windows_acl_principal()]
    if recursive:
        command.extend(["/T", "/C", "/Q"])
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RecoveryError(
            "takeover_unseal_forbidden",
            "unable to remove the recovery-owned Windows write-deny ACL",
        )


def _verify_windows_write_denied(root: Path, *, deny_delete: bool = True) -> None:
    if os.name != "nt":
        return
    _verify_windows_paths_write_denied((root, *root.rglob("*")), deny_delete=deny_delete)


def _verify_windows_paths_write_denied(
    paths: Iterable[Path], *, deny_delete: bool = True,
) -> None:
    if os.name != "nt":
        return
    # Query the effective token against each security descriptor. A create/delete
    # probe can repair the very ACL it rejects, violating observation-only use.
    import ctypes
    from ctypes import wintypes

    class GenericMapping(ctypes.Structure):
        _fields_ = [
            (name, wintypes.DWORD) for name in ("read", "write", "execute", "all")
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    handle_pointer = ctypes.POINTER(wintypes.HANDLE)
    dword_pointer = ctypes.POINTER(wintypes.DWORD)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenThreadToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.BOOL,
        handle_pointer,
    ]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, handle_pointer]
    advapi.DuplicateToken.argtypes = [wintypes.HANDLE, ctypes.c_int, handle_pointer]
    advapi.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        pointer,
        wintypes.DWORD,
        dword_pointer,
    ]
    advapi.AccessCheck.argtypes = [
        pointer,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(GenericMapping),
        pointer,
        dword_pointer,
        dword_pointer,
        ctypes.POINTER(wintypes.BOOL),
    ]
    token = wintypes.HANDLE()
    effective = wintypes.HANDLE()
    try:
        # Respect an existing thread impersonation identity; use the process
        # identity only for ERROR_NO_TOKEN, never for another lookup failure.
        if not advapi.OpenThreadToken(
            kernel.GetCurrentThread(), 0x000A, True, ctypes.byref(token)
        ):
            if ctypes.get_last_error() != 1008:
                raise ctypes.WinError(ctypes.get_last_error())
            if not advapi.OpenProcessToken(
                kernel.GetCurrentProcess(), 0x000A, ctypes.byref(token)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.DuplicateToken(token, 2, ctypes.byref(effective)):
            raise ctypes.WinError(ctypes.get_last_error())
        mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        for path in paths:
            size = wintypes.DWORD()
            # OWNER | GROUP | DACL; AccessCheck requires all three.
            if (
                advapi.GetFileSecurityW(str(path), 7, None, 0, ctypes.byref(size))
                or ctypes.get_last_error() != 122
            ):
                raise OSError("unable to size recovery security descriptor")
            descriptor = ctypes.create_string_buffer(size.value)
            if not advapi.GetFileSecurityW(
                str(path), 7, descriptor, size, ctypes.byref(size)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            privilege_size = wintypes.DWORD(0)
            granted = wintypes.DWORD()
            allowed = wintypes.BOOL()
            # MAXIMUM_ALLOWED reveals individual write rights; checking one
            # combined mask would miss a tree granting only some write rights.
            if not advapi.AccessCheck(
                descriptor,
                effective,
                0x02000000,
                ctypes.byref(mapping),
                None,
                ctypes.byref(privilege_size),
                ctypes.byref(granted),
                ctypes.byref(allowed),
            ):
                if ctypes.get_last_error() != 122:
                    raise ctypes.WinError(ctypes.get_last_error())
                privileges = ctypes.create_string_buffer(privilege_size.value)
                if not advapi.AccessCheck(
                    descriptor,
                    effective,
                    0x02000000,
                    ctypes.byref(mapping),
                    privileges,
                    ctypes.byref(privilege_size),
                    ctypes.byref(granted),
                    ctypes.byref(allowed),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            forbidden = (
                0x0002 | 0x0004 | 0x0010 | 0x0100
            )  # data, append, EA, attributes
            if deny_delete:
                forbidden |= 0x00010000 | (0x0040 if path.is_dir() else 0)
            if granted.value & forbidden:
                raise RecoveryError(
                    "restore_not_physically_read_only",
                    "recovery-owned Windows tree still grants write access",
                )
    except OSError as exc:
        raise RecoveryError(
            "restore_not_physically_read_only",
            "unable to verify the recovery Windows access rights",
        ) from exc
    finally:
        if effective.value:
            kernel.CloseHandle(effective)
        if token.value:
            kernel.CloseHandle(token)


def _write_bit_present(modes: Iterable[int]) -> bool:
    """Platform-neutral adapter for POSIX closed-tree mode verification."""

    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return any(stat.S_IMODE(mode) & writable for mode in modes)


def _force_owner_read_only_tree(root: Path, *, apply_windows_acl: bool = True) -> None:
    """Best-effort physical fail-closed protection for a recovery-owned tree."""

    for item in root.rglob("*"):
        if item.is_file():
            try:
                _make_owner_read_only(item)
            except OSError:
                pass
    for directory in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            _make_owner_read_only(directory)
        except OSError:
            pass
    try:
        _make_owner_read_only(root)
    except OSError:
        pass
    if apply_windows_acl and os.name == "nt":
        try:
            _apply_windows_write_deny(root)
        except RecoveryError:
            pass


def _protect_closed_tree(root: Path, *, deny_delete: bool = True,
                         explicit_windows_acl: bool = False) -> None:
    for item in root.rglob("*"):
        if item.is_file():
            _make_owner_read_only(item)
    for directory in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _make_owner_read_only(directory)
    _make_owner_read_only(root)
    _apply_windows_write_deny(root, deny_delete=deny_delete)
    if explicit_windows_acl and os.name == "nt":
        # Private extracted directories may carry explicit grants, which precede
        # inherited denies in Windows access checks. Materialization therefore
        # installs the deny at each independently validated ordinary entry.
        for item in root.rglob("*"):
            _apply_windows_write_deny(item, deny_delete=deny_delete)
    _verify_closed_tree_protection(root, deny_delete=deny_delete)


def _strictly_protect_closed_tree_or_force(root: Path) -> None:
    """Apply verified per-entry protection, retaining best-effort containment."""

    try:
        _protect_closed_tree(root, explicit_windows_acl=True)
    except BaseException as protection_error:
        try:
            _force_owner_read_only_tree(root)
        except BaseException as fallback_error:
            protection_error.add_note(
                "best-effort read-only fallback also raised "
                f"{type(fallback_error).__name__}"
            )
        raise


def _verify_closed_tree_protection(root: Path, *, deny_delete: bool = True) -> None:
    if _write_bit_present(
        item.stat().st_mode for item in root.rglob("*") if item.is_file()
    ):
        raise RecoveryError(
            "recovery_tree_mutable",
            "recovery-owned closed tree contains an owner-writable file",
        )
    if os.name != "nt":
        directories = (root, *(item for item in root.rglob("*") if item.is_dir()))
        if _write_bit_present(path.stat().st_mode for path in directories):
            raise RecoveryError(
                "recovery_tree_mutable",
                "recovery-owned closed tree contains an owner-writable directory",
            )
    else:
        _verify_windows_write_denied(root, deny_delete=deny_delete)


def _read_only_seal_payload(
    root: Path, relative_files: Iterable[str]
) -> dict[str, Any]:
    protected = tuple(sorted(set(relative_files)))
    return {
        "format": "research-workspace-read-only-v1",
        "lifecycle": RecoveryLifecycle.VERIFIED_READ_ONLY.value,
        "protected_files": list(protected),
        "protected_root_sha256": _tree_digest(root, protected),
        "writable": False,
        "mission_auto_resume": False,
        "canonical_effect": "none",
    }


def _seal_restored_tree(
    root: Path,
    relative_files: Iterable[str],
    *,
    takeover_pending: bool = False,
    deny_delete: bool = True,
) -> str:
    protected = tuple(sorted(set(relative_files)))
    seal_raw = canonical_json_bytes(_read_only_seal_payload(root, protected))
    seal_path = root / (
        TAKEOVER_UNSEAL_MARKER if takeover_pending else _READ_ONLY_SEAL
    )
    _write_exclusive(seal_path, seal_raw, read_only=True)
    # Current checkpoint-source restores publish the Store gate before the first
    # permission change.  A hard death while protection is being applied thus
    # leaves either a disposable owned stage or an already gated target.
    _protect_closed_tree(root, deny_delete=deny_delete)
    return _digest(seal_raw)


def _verify_read_only_seal(
    root: Path,
    relative_files: Iterable[str],
    expected_sha256: str,
    *,
    seal_name: str = _READ_ONLY_SEAL,
) -> None:
    if seal_name not in {_READ_ONLY_SEAL, TAKEOVER_UNSEAL_MARKER}:
        raise RecoveryError(
            "restore_not_physically_read_only", "read-only seal name is invalid"
        )
    seal_path = _contained(root, root / seal_name)
    if seal_path.is_symlink() or not seal_path.is_file():
        raise RecoveryError(
            "restore_not_physically_read_only", "read-only seal is absent"
        )
    expected = canonical_json_bytes(_read_only_seal_payload(root, relative_files))
    if seal_path.read_bytes() != expected or _digest(expected) != expected_sha256:
        raise RecoveryError(
            "restore_not_physically_read_only", "read-only seal binding changed"
        )
    for relative in (*tuple(relative_files), seal_name):
        path = _contained(root, root / relative)
        if stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR:
            raise RecoveryError(
                "restore_not_physically_read_only",
                f"restored file is writable: {relative}",
            )
    if os.name != "nt":
        directories = (root, *(item for item in root.rglob("*") if item.is_dir()))
        if any(
            stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR for path in directories
        ):
            raise RecoveryError(
                "restore_not_physically_read_only", "restored directory is writable"
            )
    else:
        _verify_windows_write_denied(root)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree(
    path: Path,
    *,
    owned_parent: Path,
    ignore_errors: bool = False,
) -> None:
    """Remove one plain direct child of an explicitly named task-owned parent."""

    target = Path(os.path.abspath(path))
    parent = Path(os.path.abspath(owned_parent))
    if _is_reparse_point(parent) or not parent.is_dir():
        raise RecoveryError(
            "recovery_cleanup_unsafe",
            f"cleanup parent is not a plain directory: {owned_parent}",
        )
    try:
        same_parent = os.path.samefile(target.parent, parent)
    except OSError as exc:
        raise RecoveryError(
            "recovery_cleanup_unsafe",
            "cleanup parent identity could not be verified",
        ) from exc
    if not same_parent:
        raise RecoveryError(
            "recovery_cleanup_unsafe",
            "cleanup target must be a direct child of the task-owned parent",
        )
    if _is_reparse_point(target):
        raise RecoveryError(
            "recovery_cleanup_unsafe",
            f"cleanup target is a reparse point: {path}",
        )
    if not target.exists():
        return
    _assert_plain_tree(target, code="recovery_cleanup_unsafe")

    try:
        if os.name == "nt":
            _remove_windows_write_deny(target, recursive=True)
        entries = tuple(target.rglob("*"))
        directories = (target, *(item for item in entries if item.is_dir()))
        files = tuple(item for item in entries if item.is_file())
        for item in (*directories, *files):
            if _is_reparse_point(item):
                raise RecoveryError(
                    "recovery_cleanup_unsafe",
                    f"cleanup tree changed to a reparse point: {item}",
                )
            mode = stat.S_IMODE(item.stat().st_mode) | stat.S_IWUSR
            if item.is_dir():
                mode |= stat.S_IRUSR | stat.S_IXUSR
            os.chmod(item, mode)
        # Python 3.12 rmtree retains fd-relative symlink-attack resistance where
        # the platform exposes it.  The explicit boundary/plain-tree preflight
        # guards other platforms.  Preconditioning avoids an unsafe generic
        # retry callback while handling POSIX 0555 directories and Windows
        # read-only files produced by the recovery seal.
        shutil.rmtree(target)
    except Exception:
        if target.exists():
            # Permission changes are disposal-only.  If disposal aborts, put
            # every still-plain residue back behind the recovery protection.
            # A reparse point introduced during cleanup is a security failure
            # and is never hidden by ignore_errors.
            _assert_plain_tree(target, code="recovery_cleanup_unsafe")
            _force_owner_read_only_tree(target)
        if ignore_errors:
            return
        raise


def _sqlite_integrity(path: Path) -> None:
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=0,
        )
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_key_failures = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RecoveryError(
            "backup_sqlite_invalid", f"SQLite verification failed: {exc}"
        ) from exc
    if not result or result[0] != "ok" or foreign_key_failures:
        raise RecoveryError(
            "backup_sqlite_invalid", "SQLite integrity or foreign keys failed"
        )


def _store_mode_binding_from_metadata(
    metadata: Any,
    *,
    schema_version: int,
) -> tuple[str, int]:
    """Read the integrity-bearing mode pair without inventing historical state.

    Schema targets before migration 4 predate both columns and have exactly one
    legal historical interpretation.  Current images must persist both values;
    a partial or missing current binding is corruption, not a defaulting case.
    """

    keys = set(metadata.keys())
    mode_present = "operating_mode" in keys
    version_present = "root_digest_version" in keys
    if schema_version < _OPERATING_MODE_SCHEMA_VERSION:
        if mode_present or version_present:
            raise RecoveryError(
                "backup_store_binding_invalid",
                "historical schema unexpectedly carries operating-mode columns",
            )
        return ("inactive_foundation", 1)
    if not mode_present or not version_present:
        raise RecoveryError(
            "backup_store_binding_invalid",
            "current schema lacks its explicit operating-mode binding",
        )
    return (str(metadata["operating_mode"]), int(metadata["root_digest_version"]))


def _verify_sqlite_store_binding(
    path: Path,
    expected: StoreBinding,
    canonical: CanonicalBinding,
) -> None:
    """Bind manifest store metadata to the exported SQLite image itself."""

    revision_tables = (
        "mission_revision",
        "branch_revision",
        "strategy_revision",
        "context_revision",
        "session_revision",
        "candidate_revision",
    )
    try:
        connection = open_snapshot_connection(path)
        try:
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            migration_history = _migration_history(connection)
            migration_digest = _history_set_digest(migration_history)
            migration_history_sha256 = _digest(
                canonical_json_bytes([item.to_payload() for item in migration_history])
            )
            object_digest = schema_object_digest(connection)
            metadata = connection.execute(
                "SELECT * FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            if metadata is None:
                raise RecoveryError(
                    "backup_store_binding_invalid",
                    "exported SQLite image lacks workspace metadata",
                )
            high_watermark = int(
                connection.execute(
                    "SELECT COALESCE(MAX(epoch), 0) FROM writer_epoch"
                ).fetchone()[0]
            )
            revision_count = sum(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in revision_tables
            )
            command_count = int(
                connection.execute("SELECT COUNT(*) FROM command_result").fetchone()[0]
            )
            journal_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transition_journal"
                ).fetchone()[0]
            )
        finally:
            connection.close()
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError(
            "backup_store_binding_invalid",
            f"unable to verify exported WorkspaceStore binding: {exc}",
        ) from exc
    current_epoch = (
        int(metadata["current_writer_epoch"])
        if metadata["current_writer_epoch"] is not None
        else None
    )
    if schema_version != int(metadata["schema_version"]):
        raise RecoveryError(
            "backup_store_binding_invalid",
            "exported SQLite user_version differs from workspace metadata",
        )
    operating_mode, root_digest_version = _store_mode_binding_from_metadata(
        metadata,
        schema_version=schema_version,
    )
    actual = (
        str(metadata["project_id"]),
        str(metadata["root_identity"]),
        str(metadata["application_version"]),
        int(metadata["schema_version"]),
        operating_mode,
        root_digest_version,
        migration_digest,
        migration_history,
        migration_history_sha256,
        object_digest,
        str(metadata["canonical_authority_digest"]),
        current_epoch,
        high_watermark,
        str(metadata["lifecycle"]),
        int(metadata["current_project_commit"]),
        str(metadata["current_root_digest"]),
        (
            str(metadata["transition_head_digest"])
            if metadata["transition_head_digest"] is not None
            else None
        ),
    )
    declared = (
        expected.project_id,
        expected.root_identity,
        expected.application_version,
        expected.schema_version,
        expected.operating_mode,
        expected.root_digest_version,
        expected.migration_digest,
        expected.migration_history,
        expected.migration_history_sha256,
        expected.schema_object_digest,
        expected.canonical_authority_digest,
        expected.current_writer_epoch,
        expected.writer_epoch_high_watermark,
        expected.writer_lifecycle,
        expected.project_commit_id,
        expected.project_root_digest,
        expected.transition_head,
    )
    if actual != declared:
        raise RecoveryError(
            "backup_store_binding_invalid",
            "backup manifest store binding differs from exported SQLite metadata",
        )
    if _digest(str(metadata["canonical_authority_json"]).encode("utf-8")) != str(
        metadata["canonical_authority_digest"]
    ):
        raise RecoveryError(
            "backup_store_binding_invalid",
            "exported canonical-authority binding digest is corrupt",
        )
    try:
        authority = loads_strict_json_object(str(metadata["canonical_authority_json"]))
        persisted_canonical = _canonical_binding_from_authority(
            authority,
            source_commit=canonical.source_commit,
        )
    except Exception as exc:
        raise RecoveryError(
            "backup_store_binding_invalid",
            f"exported canonical authority vector is invalid: {exc}",
        ) from exc
    if persisted_canonical != canonical:
        raise RecoveryError(
            "backup_store_binding_invalid",
            "backup canonical binding differs from exported WorkspaceStore authority",
        )
    integrity_payload = {
        "project_id": str(metadata["project_id"]),
        "schema_version": int(metadata["schema_version"]),
        "current_writer_epoch": current_epoch,
        "current_project_commit": int(metadata["current_project_commit"]),
        "current_root_digest": str(metadata["current_root_digest"]),
        "transition_head_digest": (
            str(metadata["transition_head_digest"])
            if metadata["transition_head_digest"] is not None
            else None
        ),
        "revision_count": revision_count,
        "command_count": command_count,
        "journal_count": journal_count,
    }
    if (
        _digest(canonical_json_bytes(integrity_payload))
        != expected.integrity_report_sha256
    ):
        raise RecoveryError(
            "backup_store_binding_invalid",
            "backup manifest integrity report does not describe the exported SQLite image",
        )


def _write_exclusive(path: Path, raw: bytes, *, read_only: bool = False) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if read_only:
        os.chmod(path, stat.S_IREAD if os.name == "nt" else 0o444)


def _copy_verified(
    source: Path,
    target: Path,
    expected_digest: str,
    expected_length: int,
    *,
    read_only: bool = False,
) -> None:
    if source.is_symlink() or not source.is_file():
        raise RecoveryError(
            "backup_copy_source_invalid", f"copy source is not a regular file: {source}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    length = 0
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = input_stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                length += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if length != expected_length or digest.hexdigest() != expected_digest:
        target.unlink(missing_ok=True)
        raise RecoveryError("backup_copy_mismatch", f"copied bytes differ for {source}")
    if read_only:
        os.chmod(target, stat.S_IREAD if os.name == "nt" else 0o444)


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip().lower()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise RecoveryError(
            "canonical_source_commit_unavailable", "unable to verify repository HEAD"
        )
    return value


@dataclass(frozen=True)
class CanonicalBinding:
    path: str
    sha256: str
    source_commit: str
    authority_vector: Mapping[str, Any]
    validation_contract_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, str)
            or not self.path
            or "\\" in self.path
            or Path(self.path).is_absolute()
            or ".." in Path(self.path).parts
            or Path(self.path).parts[0].endswith(":")
        ):
            raise ValueError("canonical binding path must be a logical relative path")
        _require_digest(self.sha256, "canonical sha256")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise ValueError("canonical binding requires an exact source commit")
        authority = deep_thaw(self.authority_vector)
        if not isinstance(authority, dict):
            raise ValueError("canonical authority vector must be an object")
        if authority.get("binding_version") == 2:
            expected = {
                "binding_version": 2,
                "source_class": "live_canonical",
                "canonical_state_path": self.path,
                "canonical_state_sha256": self.sha256,
                "source_commit": self.source_commit,
            }
            if authority != expected:
                raise ValueError("v2 canonical authority is not the exact five-key binding")
            if self.validation_contract_sha256 is not None:
                raise ValueError("v2 canonical binding cannot carry a legacy contract digest")
        elif "binding_version" not in authority:
            if set(authority) != _LEGACY_AUTHORITY_VECTOR_KEYS:
                raise ValueError("legacy authority is not the exact unversioned vector")
            if self.validation_contract_sha256 is None:
                raise ValueError("legacy canonical binding lacks its contract digest")
            _require_digest(
                self.validation_contract_sha256,
                "legacy validation contract sha256",
            )
            required = {
                "source_class": "live_canonical",
                "canonical_state_path": self.path,
                "canonical_state_sha256": self.sha256,
                "validation_contract_sha256": self.validation_contract_sha256,
            }
            if any(authority.get(key) != value for key, value in required.items()):
                raise ValueError("legacy authority differs from its exact source fields")
            if authority.get("source_commit") not in {None, self.source_commit}:
                raise ValueError("legacy authority source commit differs from provenance")
        else:
            raise ValueError("unsupported canonical authority binding version")
        object.__setattr__(self, "authority_vector", deep_freeze(authority))

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "path": self.path,
            "sha256": self.sha256,
            "source_commit": self.source_commit,
            "authority_vector": deep_thaw(self.authority_vector),
        }
        if self.validation_contract_sha256 is not None:
            payload["validation_contract_sha256"] = self.validation_contract_sha256
        return payload

    def authority_payload(self) -> dict[str, Any]:
        authority = deep_thaw(self.authority_vector)
        if not isinstance(authority, dict):
            raise ValueError("canonical authority vector must be an object")
        return authority


@dataclass(frozen=True)
class StoreBinding:
    project_id: str
    root_identity: str
    application_version: str
    schema_version: int
    operating_mode: str
    root_digest_version: int
    migration_digest: str
    migration_history: tuple[AppliedMigrationBinding, ...]
    migration_history_sha256: str
    schema_object_digest: str
    canonical_authority_digest: str
    current_writer_epoch: int | None
    writer_epoch_high_watermark: int
    writer_lifecycle: str
    project_commit_id: int
    project_root_digest: str
    transition_head: str | None
    integrity_report_sha256: str

    def __post_init__(self) -> None:
        if (
            not self.project_id
            or not self.application_version
            or not self.writer_lifecycle
            or not self.operating_mode
        ):
            raise ValueError("store binding text fields are required")
        _require_digest(self.root_identity, "root identity")
        _require_digest(self.project_root_digest, "project root digest")
        _require_digest(self.integrity_report_sha256, "integrity report digest")
        _require_digest(self.migration_digest, "migration digest")
        _require_digest(self.migration_history_sha256, "migration-history digest")
        _require_digest(self.schema_object_digest, "schema-object digest")
        _require_digest(self.canonical_authority_digest, "canonical-authority digest")
        if self.transition_head is not None:
            _require_digest(self.transition_head, "transition head")
        counters = (
            self.schema_version,
            self.root_digest_version,
            self.writer_epoch_high_watermark,
            self.project_commit_id,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in counters
        ):
            raise ValueError("store schema and project commit must be integers")
        if (
            self.schema_version < 1
            or self.root_digest_version < 1
            or self.writer_epoch_high_watermark < 0
            or self.project_commit_id < 0
        ):
            raise ValueError("store schema and project commit are out of range")
        if (self.operating_mode, self.root_digest_version) not in {
            ("inactive_foundation", 1),
            ("pre_cutover_observer", 2),
            ("mission_runtime", 3),
            ("mission_runtime", 4),
            ("mission_runtime", 5),
            ("mission_runtime", 6),
        }:
            raise ValueError(
                "store operating mode and root-digest version are not an authorized pair"
            )
        if self.operating_mode == "mission_runtime" and (
            (self.root_digest_version == 3 and self.schema_version > 6)
            or (self.root_digest_version == 4 and self.schema_version not in {7, 8})
            or (self.root_digest_version == 5 and self.schema_version != 9)
            or (self.root_digest_version == 6 and self.schema_version not in {10, 12})
        ):
            raise ValueError(
                "Mission Store schema and root-digest generations disagree"
            )
        expected_versions = tuple(range(1, self.schema_version + 1))
        if tuple(item.version for item in self.migration_history) != expected_versions:
            raise ValueError(
                "migration history must be contiguous through schema_version"
            )
        if (
            _digest(
                canonical_json_bytes(
                    [item.to_payload() for item in self.migration_history]
                )
            )
            != self.migration_history_sha256
        ):
            raise ValueError("migration-history digest does not bind the applied rows")
        if self.current_writer_epoch is not None and (
            isinstance(self.current_writer_epoch, bool) or self.current_writer_epoch < 1
        ):
            raise ValueError("current writer epoch must be positive or absent")
        if (
            self.current_writer_epoch is not None
            and self.current_writer_epoch > self.writer_epoch_high_watermark
        ):
            raise ValueError("current writer epoch cannot exceed its high-watermark")

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "root_identity": self.root_identity,
            "application_version": self.application_version,
            "schema_version": self.schema_version,
            "operating_mode": self.operating_mode,
            "root_digest_version": self.root_digest_version,
            "migration_digest": self.migration_digest,
            "migration_history": [item.to_payload() for item in self.migration_history],
            "migration_history_sha256": self.migration_history_sha256,
            "schema_object_digest": self.schema_object_digest,
            "canonical_authority_digest": self.canonical_authority_digest,
            "current_writer_epoch": self.current_writer_epoch,
            "writer_epoch_high_watermark": self.writer_epoch_high_watermark,
            "writer_lifecycle": self.writer_lifecycle,
            "project_commit_id": self.project_commit_id,
            "project_root_digest": self.project_root_digest,
            "transition_head": self.transition_head,
            "integrity_report_sha256": self.integrity_report_sha256,
        }


@dataclass(frozen=True)
class RestoreTestRecord:
    verification_id: str
    verified_at: str
    sqlite_integrity: str
    evidence_count: int
    restored_root_sha256: str
    deletion_fence_sha256: str
    read_only_seal_sha256: str
    status: str
    receipt_sha256: str

    def body_payload(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "verified_at": self.verified_at,
            "sqlite_integrity": self.sqlite_integrity,
            "evidence_count": self.evidence_count,
            "restored_root_sha256": self.restored_root_sha256,
            "deletion_fence_sha256": self.deletion_fence_sha256,
            "read_only_seal_sha256": self.read_only_seal_sha256,
            "status": self.status,
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.body_payload(), "receipt_sha256": self.receipt_sha256}


@dataclass(frozen=True, slots=True)
class CheckpointSourceBinding:
    """Path-free binding from one claimed checkpoint source into a BackupSet."""

    source_incarnation: str
    mission_id: str
    checkpoint_id: str
    checkpoint_sha256: str
    checkpoint_project_commit: int
    project_id: str
    source_root_identity: str
    canonical_authority_digest: str
    prior_writer_epoch: int
    prior_writer_owner: str
    project_commit: int
    root_digest: str
    transition_head_digest: str | None
    database_sha256: str
    database_length: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_incarnation, "source incarnation"),
            (self.checkpoint_sha256, "checkpoint digest"),
            (self.source_root_identity, "source root identity"),
            (self.canonical_authority_digest, "canonical authority digest"),
            (self.root_digest, "root digest"),
            (self.database_sha256, "database digest"),
        ):
            _require_digest(value, f"checkpoint source {label}")
        if self.transition_head_digest is not None:
            _require_digest(
                self.transition_head_digest,
                "checkpoint source transition head",
            )
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.mission_id,
                self.checkpoint_id,
                self.project_id,
                self.prior_writer_owner,
            )
        ):
            raise ValueError("checkpoint source text bindings must be non-empty")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < minimum
            for value, minimum in (
                (self.checkpoint_project_commit, 0),
                (self.prior_writer_epoch, 1),
                (self.project_commit, 0),
                (self.database_length, 1),
            )
        ):
            raise ValueError("checkpoint source numeric bindings are invalid")

    @classmethod
    def from_source(cls, source: WorkspaceCheckpointSource) -> "CheckpointSourceBinding":
        if type(source) is not WorkspaceCheckpointSource or source.state != "claimed":
            raise ValueError("BackupSet checkpoint source must be exactly claimed")
        payload = source.source_binding_payload()
        return cls(
            **{
                "source_incarnation": payload["source_incarnation"],
                "mission_id": payload["mission_id"],
                "checkpoint_id": payload["checkpoint_id"],
                "checkpoint_sha256": payload["checkpoint_sha256"],
                "checkpoint_project_commit": payload["checkpoint_project_commit"],
                "project_id": payload["project_id"],
                "source_root_identity": payload["source_root_identity"],
                "canonical_authority_digest": payload[
                    "canonical_authority_digest"
                ],
                "prior_writer_epoch": payload["prior_writer_epoch"],
                "prior_writer_owner": payload["prior_writer_owner"],
                "project_commit": payload["project_commit"],
                "root_digest": payload["root_digest"],
                "transition_head_digest": payload["transition_head_digest"],
                "database_sha256": payload["database_sha256"],
                "database_length": payload["database_length"],
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "mathematical_research.checkpoint_source.v1",
            "source_incarnation": self.source_incarnation,
            "mission_id": self.mission_id,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_sha256": self.checkpoint_sha256,
            "checkpoint_project_commit": self.checkpoint_project_commit,
            "project_id": self.project_id,
            "source_root_identity": self.source_root_identity,
            "canonical_authority_digest": self.canonical_authority_digest,
            "prior_writer_epoch": self.prior_writer_epoch,
            "prior_writer_owner": self.prior_writer_owner,
            "project_commit": self.project_commit,
            "root_digest": self.root_digest,
            "transition_head_digest": self.transition_head_digest,
            "database_sha256": self.database_sha256,
            "database_length": self.database_length,
        }


@dataclass(frozen=True)
class BackupManifest:
    backup_id: str
    created_at: str
    store: StoreBinding
    canonical: CanonicalBinding
    closure: ClosureManifest
    cas_inventory_contract: str
    sqlite_relative_path: str
    sqlite_sha256: str
    sqlite_length: int
    evidence_inventory: tuple[BlobRecord, ...]
    evidence_root_sha256: str
    evidence_metadata_sha256: str
    deletion_directives: tuple[DeletionDirective, ...]
    tombstones: tuple[EvidenceTombstone, ...]
    integrity_result: str
    restore_test: RestoreTestRecord
    complete: bool
    manifest_sha256: str
    canonical_effect: str = "none"
    checkpoint_source: CheckpointSourceBinding | None = None

    def __post_init__(self) -> None:
        if self.cas_inventory_contract not in {
            _LEGACY_CLOSURE_CAS_INVENTORY,
            _COMMITTED_BLOB_CAS_INVENTORY,
        }:
            raise ValueError("backup CAS inventory contract is unsupported")
        object.__setattr__(
            self,
            "evidence_inventory",
            tuple(sorted(self.evidence_inventory, key=lambda item: item.sha256)),
        )
        inventory_digests = tuple(item.sha256 for item in self.evidence_inventory)
        if (
            inventory_digests != tuple(sorted(set(inventory_digests)))
            or (
                self.cas_inventory_contract == _LEGACY_CLOSURE_CAS_INVENTORY
                and set(inventory_digests) != set(self.closure.blob_sha256s)
            )
            or (
                self.cas_inventory_contract == _COMMITTED_BLOB_CAS_INVENTORY
                and not set(self.closure.blob_sha256s).issubset(inventory_digests)
            )
        ):
            raise ValueError("backup CAS inventory does not match its contract")
        object.__setattr__(
            self,
            "deletion_directives",
            tuple(sorted(self.deletion_directives, key=lambda item: item.directive_id)),
        )
        object.__setattr__(
            self,
            "tombstones",
            tuple(sorted(self.tombstones, key=lambda item: item.tombstone_id)),
        )
        if self.checkpoint_source is not None:
            source = self.checkpoint_source
            if type(source) is not CheckpointSourceBinding or (
                source.project_id,
                source.source_root_identity,
                source.canonical_authority_digest,
                source.prior_writer_epoch,
                source.project_commit,
                source.root_digest,
                source.transition_head_digest,
                source.database_sha256,
                source.database_length,
            ) != (
                self.store.project_id,
                self.store.root_identity,
                self.store.canonical_authority_digest,
                self.store.writer_epoch_high_watermark,
                self.store.project_commit_id,
                self.store.project_root_digest,
                self.store.transition_head,
                self.sqlite_sha256,
                self.sqlite_length,
            ) or self.store.current_writer_epoch is not None or (
                self.store.writer_lifecycle != RecoveryLifecycle.QUIESCED.value
            ):
                raise ValueError(
                    "checkpoint source does not match its quiesced BackupSet binding"
                )

    def body_payload(self) -> dict[str, Any]:
        payload = {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "store": self.store.to_payload(),
            "canonical": self.canonical.to_payload(),
            "closure": self.closure.to_payload(),
            "sqlite_relative_path": self.sqlite_relative_path,
            "sqlite_sha256": self.sqlite_sha256,
            "sqlite_length": self.sqlite_length,
            "evidence_inventory": [
                item.to_payload() for item in self.evidence_inventory
            ],
            "evidence_root_sha256": self.evidence_root_sha256,
            "evidence_metadata_sha256": self.evidence_metadata_sha256,
            "deletion_directives": [
                item.to_payload() for item in self.deletion_directives
            ],
            "tombstones": [item.to_payload() for item in self.tombstones],
            "integrity_result": self.integrity_result,
            "restore_test": self.restore_test.to_payload(),
            "complete": self.complete,
            "canonical_effect": "none",
        }
        if self.cas_inventory_contract != _LEGACY_CLOSURE_CAS_INVENTORY:
            payload["cas_inventory_contract"] = self.cas_inventory_contract
        if self.checkpoint_source is not None:
            payload["checkpoint_source"] = self.checkpoint_source.to_payload()
        return payload

    def to_payload(self) -> dict[str, Any]:
        return {**self.body_payload(), "manifest_sha256": self.manifest_sha256}

    @property
    def takeover_classification(self) -> str:
        """Classify recovery use without changing the sealed manifest bytes.

        An online backup taken while a writer epoch is active is a valid,
        restore-tested inspection snapshot.  It is not a takeover generation:
        the later fence proving that writer dead cannot already belong to this
        backup's immutable Evidence closure.  A quiesced/offline generation is
        therefore required before the existing persisted-fence takeover path
        can be used.
        """

        if self.store.operating_mode == "pre_cutover_observer":
            return "inspection_only_historical_workspace"
        if self.store.operating_mode == "mission_runtime" and (
            (self.store.schema_version == 6 and self.store.root_digest_version == 3)
            or (self.store.schema_version == 7 and self.store.root_digest_version == 4)
        ):
            return "inspection_only_historical_migration_bridge"
        if (
            self.store.current_writer_epoch is not None
            or self.store.writer_lifecycle == RecoveryLifecycle.ACTIVE.value
        ):
            return "inspection_only_active_writer"
        if self.checkpoint_source is not None:
            return "takeover_candidate_requires_operational_fence"
        return "takeover_candidate_requires_persisted_fence"


@dataclass(frozen=True)
class BackupSet:
    path: Path
    manifest: BackupManifest


def derive_staging_portable_migration_operation_id(
    *,
    source_generation_id: str,
    mission_id: str,
    source_backup_id: str,
    source_manifest_sha256: str,
    source_release_sha: str,
    migration_executor_release_sha: str,
    source_schema_version: int = 9,
) -> str:
    """Bind one of the two admitted staging source/executor migration pairs."""

    if type(source_schema_version) is not int or source_schema_version not in {9, 10}:
        raise RecoveryError(
            "portable_migration_identity_invalid",
            "staging migration source must be schema9/root5 or schema10/root6",
        )

    for value, label in (
        (source_generation_id, "source generation"),
        (mission_id, "Mission"),
        (source_backup_id, "source BackupSet"),
    ):
        if not isinstance(value, str) or BACKUP_ID_RE.fullmatch(value) is None:
            raise RecoveryError(
                "portable_migration_identity_invalid",
                f"staging portable migration {label} identity is invalid",
            )
    try:
        _require_digest(source_manifest_sha256, "portable migration manifest")
    except ValueError as exc:
        raise RecoveryError(
            "portable_migration_identity_invalid",
            "staging portable migration manifest identity is invalid",
        ) from exc
    for value, label in (
        (source_release_sha, "source release"),
        (migration_executor_release_sha, "migration executor release"),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise RecoveryError(
                "portable_migration_identity_invalid",
                f"staging portable migration {label} identity is invalid",
            )
    operation_sha256 = _digest(
        canonical_json_bytes(
            {
                "format": (
                    _STAGING_MIGRATION_OPERATION_SCHEMA if source_schema_version == 9
                    else "mathematical_research.staging_portable_migration_operation.v3"
                ),
                "mission_id": mission_id,
                "project_id": (
                    _STAGING_MIGRATION_OPERATION_PROJECT_ID_V1
                    if source_schema_version == 9 else _RH_STAGING_PROJECT_ID
                ),
                "source_generation_id": source_generation_id,
                "source_backup_id": source_backup_id,
                "source_manifest_sha256": source_manifest_sha256,
                "source_release_sha": source_release_sha,
                "migration_executor_release_sha": migration_executor_release_sha,
                **({"source_schema_version": 10, "source_root_digest_version": 6}
                   if source_schema_version == 10 else {}),
                "target_schema_version": 10 if source_schema_version == 9 else 12,
                "target_root_digest_version": 6,
            }
        )
    )
    return f"{_STAGING_MIGRATION_OPERATION_PREFIX}{operation_sha256}"


@dataclass(frozen=True, slots=True)
class _StagingPortableMigrationScope:
    """Process-local permission to treat two exact staging children as portable."""

    backups_root: Path
    source_generation_id: str
    source_backup_id: str
    source_manifest_sha256: str
    source_root_identity: str
    migration_operation_id: str
    migration_executor_release_sha: str
    migrated_backup_id: str
    source_schema_version: int
    _issuer: object = field(repr=False, compare=False)

    def _kind(self, path: Path) -> str | None:
        if (
            self._issuer is not _STAGING_MIGRATION_SCOPE_ISSUER
            or self.backups_root.parent.name != self.migration_operation_id
            or not BACKUP_ID_RE.fullmatch(self.source_generation_id)
            or re.fullmatch(r"[0-9a-f]{40}", self.migration_executor_release_sha)
            is None
            or type(self.source_schema_version) is not int
            or self.source_schema_version not in {9, 10}
        ):
            raise RecoveryError(
                "portable_migration_scope_invalid",
                "portable migration verification scope was not issued by Recovery",
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != self.backups_root:
            return None
        if resolved.name == self.source_backup_id:
            return "source"
        if resolved.name == self.migrated_backup_id or resolved.name.startswith(
            f".stage-{self.migrated_backup_id}-"
        ):
            return "migrated"
        return None

    def authorize(self, backup: BackupSet) -> bool:
        kind = self._kind(backup.path)
        if kind is None:
            return False
        manifest = backup.manifest
        if manifest.store.root_identity != self.source_root_identity:
            raise RecoveryError(
                "portable_migration_scope_invalid",
                "portable migration BackupSet changed source-root provenance",
            )
        if kind == "source" and (
            manifest.backup_id != self.source_backup_id
            or manifest.manifest_sha256 != self.source_manifest_sha256
            or manifest.store.schema_version != self.source_schema_version
            or manifest.store.root_digest_version
            != (5 if self.source_schema_version == 9 else 6)
        ):
            raise RecoveryError(
                "portable_migration_scope_invalid",
                "portable migration source differs from its exact admitted generation",
            )
        if kind == "migrated" and (
            manifest.backup_id != self.migrated_backup_id
            or manifest.store.schema_version != (10 if self.source_schema_version == 9 else 12)
            or manifest.store.root_digest_version != 6
        ):
            raise RecoveryError(
                "portable_migration_scope_invalid",
                "portable migration result differs from its exact successor generation",
            )
        return True


_STAGING_PORTABLE_MIGRATION_SCOPE: ContextVar[
    _StagingPortableMigrationScope | None
] = ContextVar("research-staging-portable-migration-scope", default=None)


@dataclass(frozen=True, slots=True)
class PortableBackupVerificationReport:
    """Closed facts retaining a BackupSet's passed audit, with current byte/custody binding."""

    schema_version: str
    backup_schema_version: int
    backup_id: str
    manifest_sha256: str
    database_sha256: str
    database_length: int
    source_root_identity: str
    project_id: str
    project_commit: int
    project_root_digest: str
    transition_head: str | None
    canonical_path: str
    canonical_sha256: str
    canonical_source_commit: str
    closure_id: str
    closure_sha256: str
    cas_object_count: int
    cas_total_bytes: int
    checkpoint_id: str | None
    checkpoint_sha256: str | None
    checkpoint_project_commit: int | None
    structural_verification_passed: bool
    semantic_verification_passed: bool
    read_only_tree_verified: bool
    capability_bearing_values_returned: bool


@dataclass(frozen=True, slots=True)
class StagingPortableImportReport:
    """Bounded, non-capability result for one disposable staging import."""

    schema_version: str
    status: str
    mission_id: str
    project_id: str
    backup_id: str
    source_manifest_sha256: str
    source_database_sha256: str
    source_root_identity: str
    target_database_sha256: str
    target_root_identity: str
    project_commit: int
    project_root_digest: str
    transition_head: str | None
    canonical_authority_digest: str
    mission_revision: int
    mission_payload_sha256: str
    writer_owner: str
    writer_epoch: int
    writer_lifecycle: str
    portable_verification_passed: bool
    structural_verification_passed: bool
    semantic_verification_passed: bool
    cas_verification_passed: bool
    source_material_unchanged: bool
    mission_mutations: int
    provider_effect: bool
    canonical_effect: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != _STAGING_IMPORT_REPORT_SCHEMA
            or self.status != "staging_imported_quiesced"
            or self.project_id != _RH_STAGING_PROJECT_ID
            or not self.mission_id
            or not BACKUP_ID_RE.fullmatch(self.backup_id)
            or self.writer_lifecycle != RecoveryLifecycle.QUIESCED.value
            or self.canonical_effect != "none"
            or self.provider_effect
            or self.mission_mutations != 0
            or isinstance(self.project_commit, bool)
            or self.project_commit < 0
            or isinstance(self.mission_revision, bool)
            or self.mission_revision < 1
            or isinstance(self.writer_epoch, bool)
            or self.writer_epoch < 1
            or any(
                value is not True
                for value in (
                    self.portable_verification_passed,
                    self.structural_verification_passed,
                    self.semantic_verification_passed,
                    self.cas_verification_passed,
                    self.source_material_unchanged,
                )
            )
        ):
            raise ValueError("staging portable-import report is malformed")
        for value, label in (
            (self.source_manifest_sha256, "staging source manifest"),
            (self.source_database_sha256, "staging source database"),
            (self.source_root_identity, "staging source root"),
            (self.target_database_sha256, "staging target database"),
            (self.target_root_identity, "staging target root"),
            (self.project_root_digest, "staging project root"),
            (self.canonical_authority_digest, "staging canonical authority"),
            (self.mission_payload_sha256, "staging Mission payload"),
        ):
            _require_digest(value, label)
        if self.transition_head is not None:
            _require_digest(self.transition_head, "staging transition head")
        if (
            not self.writer_owner.startswith("os-principal-owner:sha256:")
            or SHA256_RE.fullmatch(self.writer_owner.removeprefix(
                "os-principal-owner:sha256:"
            ))
            is None
        ):
            raise ValueError("staging writer owner is not one redacted OS principal")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "backup_id": self.backup_id,
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_database_sha256": self.source_database_sha256,
            "source_root_identity": self.source_root_identity,
            "target_database_sha256": self.target_database_sha256,
            "target_root_identity": self.target_root_identity,
            "project_commit": self.project_commit,
            "project_root_digest": self.project_root_digest,
            "transition_head": self.transition_head,
            "canonical_authority_digest": self.canonical_authority_digest,
            "mission_revision": self.mission_revision,
            "mission_payload_sha256": self.mission_payload_sha256,
            "writer_owner": self.writer_owner,
            "writer_epoch": self.writer_epoch,
            "writer_lifecycle": self.writer_lifecycle,
            "portable_verification_passed": self.portable_verification_passed,
            "structural_verification_passed": self.structural_verification_passed,
            "semantic_verification_passed": self.semantic_verification_passed,
            "cas_verification_passed": self.cas_verification_passed,
            "source_material_unchanged": self.source_material_unchanged,
            "mission_mutations": self.mission_mutations,
            "provider_effect": self.provider_effect,
            "canonical_effect": self.canonical_effect,
        }


@dataclass(frozen=True, slots=True)
class StagingPortableMigrationResult:
    """Verified side-by-side staging BackupSet and bounded migration facts."""

    backup: BackupSet = field(repr=False)
    mission_id: str
    source_generation_id: str
    source_release_sha: str
    migration_operation_id: str
    migration_operation_sha256: str
    migration_executor_release_sha: str
    source: PortableBackupVerificationReport
    source_root_digest_version: int
    migration_id: str
    migration_attempt_id: str
    migration_plan_sha256: str
    pre_logical_sha256: str
    post_logical_sha256: str
    migrated: PortableBackupVerificationReport
    migrated_root_digest_version: int

    def __post_init__(self) -> None:
        try:
            expected_operation_id = derive_staging_portable_migration_operation_id(
                source_generation_id=self.source_generation_id,
                mission_id=self.mission_id,
                source_backup_id=self.source.backup_id,
                source_manifest_sha256=self.source.manifest_sha256,
                source_release_sha=self.source_release_sha,
                migration_executor_release_sha=(
                    self.migration_executor_release_sha
                ),
                source_schema_version=self.source.backup_schema_version,
            )
        except RecoveryError as exc:
            raise ValueError("staging portable-migration result is malformed") from exc
        if (
            type(self.backup) is not BackupSet
            or self.backup.manifest.manifest_sha256 != self.migrated.manifest_sha256
            or self.backup.manifest.backup_id != self.migrated.backup_id
            or (
                self.source.backup_schema_version, self.source_root_digest_version,
                self.migrated.backup_schema_version, self.migrated_root_digest_version,
            ) not in {(9, 5, 10, 6), (10, 6, 12, 6)}
            or self.source.project_id != _RH_STAGING_PROJECT_ID
            or self.migrated.project_id != _RH_STAGING_PROJECT_ID
            or not self.mission_id
            or not BACKUP_ID_RE.fullmatch(self.source_generation_id)
            or self.migration_operation_id != expected_operation_id
            or self.migration_operation_sha256
            != expected_operation_id.removeprefix(_STAGING_MIGRATION_OPERATION_PREFIX)
            or self.migration_id
            != f"rh-staging-{self.migration_operation_id}-schema{self.migrated.backup_schema_version}"
            or self.migrated.backup_id != f"{self.source.backup_id}.schema{self.migrated.backup_schema_version}"
            or not BACKUP_ID_RE.fullmatch(self.migration_id)
            or not BACKUP_ID_RE.fullmatch(self.migration_attempt_id)
            or re.fullmatch(r"[0-9a-f]{40}", self.source_release_sha) is None
            or re.fullmatch(
                r"[0-9a-f]{40}", self.migration_executor_release_sha
            )
            is None
        ):
            raise ValueError("staging portable-migration result is malformed")
        for value, label in (
            (self.migration_operation_sha256, "staging migration operation"),
            (self.migration_plan_sha256, "staging migration plan"),
            (self.pre_logical_sha256, "staging pre-migration logical state"),
            (self.post_logical_sha256, "staging post-migration logical state"),
        ):
            _require_digest(value, label)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": (
                _STAGING_MIGRATION_REPORT_SCHEMA if self.source.backup_schema_version == 9
                else _STAGING_SCHEMA12_MIGRATION_REPORT_SCHEMA
            ),
            "status": "staging_migrated_side_by_side",
            "mission_id": self.mission_id,
            "project_id": self.source.project_id,
            "source_generation_id": self.source_generation_id,
            "source_release_sha": self.source_release_sha,
            "migration_operation_id": self.migration_operation_id,
            "migration_operation_sha256": self.migration_operation_sha256,
            "migration_executor_release_sha": self.migration_executor_release_sha,
            "source": {
                "backup_id": self.source.backup_id,
                "manifest_sha256": self.source.manifest_sha256,
                "database_sha256": self.source.database_sha256,
                "schema_version": self.source.backup_schema_version,
                "root_digest_version": self.source_root_digest_version,
                "project_commit": self.source.project_commit,
                "project_root_digest": self.source.project_root_digest,
                "transition_head": self.source.transition_head,
                "checkpoint_id": self.source.checkpoint_id,
                "checkpoint_sha256": self.source.checkpoint_sha256,
                "source_material_unchanged": True,
            },
            "migration": {
                "operation_id": self.migration_operation_id,
                "operation_sha256": self.migration_operation_sha256,
                "executor_release_sha": self.migration_executor_release_sha,
                "migration_id": self.migration_id,
                "attempt_id": self.migration_attempt_id,
                "source_schema_version": self.source.backup_schema_version,
                "target_schema_version": self.migrated.backup_schema_version,
                "plan_sha256": self.migration_plan_sha256,
                "pre_logical_sha256": self.pre_logical_sha256,
                "post_logical_sha256": self.post_logical_sha256,
                "selected_live_root": False,
                "mission_auto_resume": False,
                "canonical_effect": "none",
            },
            "migrated": {
                "backup_id": self.migrated.backup_id,
                "manifest_sha256": self.migrated.manifest_sha256,
                "database_sha256": self.migrated.database_sha256,
                "schema_version": self.migrated.backup_schema_version,
                "root_digest_version": self.migrated_root_digest_version,
                "project_id": self.migrated.project_id,
                "project_commit": self.migrated.project_commit,
                "project_root_digest": self.migrated.project_root_digest,
                "transition_head": self.migrated.transition_head,
                "checkpoint_id": self.migrated.checkpoint_id,
                "checkpoint_sha256": self.migrated.checkpoint_sha256,
                "semantic_verification_passed": (
                    self.migrated.semantic_verification_passed
                ),
                "structural_verification_passed": (
                    self.migrated.structural_verification_passed
                ),
            },
            "rollback": {
                "source_backup_preserved": True,
                "source_backup_id": self.source.backup_id,
                "source_manifest_sha256": self.source.manifest_sha256,
                "selected_live_root": False,
                "mission_auto_resume": False,
            },
            "mission_mutations": 0,
            "provider_effect": False,
            "canonical_effect": "none",
        }


@dataclass(frozen=True, slots=True)
class InstalledCurrentBackupReport:
    """Closed facts from the installed current-schema BackupSet owner."""

    schema_version: str
    backup_id: str
    manifest_sha256: str
    mission_id: str
    project_id: str
    executing_release_sha: str
    installed_release_record_sha256: str
    installed_release_bundle_sha256: str
    canonical_source_commit: str
    database_project_commit: int
    database_root_digest: str
    database_transition_head: str | None
    checkpoint_id: str
    checkpoint_sha256: str
    checkpoint_project_commit: int
    closure_id: str
    closure_sha256: str
    cas_inventory_contract: str
    cas_object_count: int
    cas_total_bytes: int
    writer_leases_created: int
    mission_mutations: int
    lifecycle_mutations: int
    host_effects: int
    provider_effects: int


def _is_current_takeover_contract(manifest: BackupManifest) -> bool:
    store = manifest.store
    return (
        store.application_version == APPLICATION_VERSION
        and is_direct_mission_generation(
            store.schema_version, store.root_digest_version, store.operating_mode,
        )
    )


@dataclass(frozen=True, slots=True)
class _VerifiedBackupIntegrityAuthority:
    """Process authority for one disk-reverified BackupSet snapshot.

    Recovery issues this value only after the public structural/disk verifier
    has re-read the source BackupSet and, when present, the migration executor
    has reverified the exact closed result artifact against that source.
    """

    backup_path: Path
    database: Path
    cas_root: Path
    source_root_identity: str
    portable_materialization: bool
    closure: ClosureManifest
    inventory: tuple[BlobRecord, ...]
    source_backup_manifest_sha256: str
    migration_result_sha256: str | None
    database_sha256: str
    database_length: int
    snapshot_tree_sha256: str
    migration_lease_path: str | None
    migration_lease_id: str | None
    rebackup_lease_path: str | None
    rebackup_lease_id: str | None
    _issuer_token: object = field(default=None, repr=False, compare=False)
    _issuer_seal: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "backup_path", self.backup_path.resolve(strict=True))
        object.__setattr__(self, "database", self.database.resolve(strict=True))
        object.__setattr__(self, "cas_root", self.cas_root.resolve(strict=True))
        if (
            self._issuer_token is not _RECOVERY_ISSUER_TOKEN
            or type(self.closure) is not ClosureManifest
            or type(self.inventory) is not tuple
            or any(type(item) is not BlobRecord for item in self.inventory)
            or tuple(item.sha256 for item in self.inventory)
            != tuple(sorted(set(item.sha256 for item in self.inventory)))
            or not set(self.closure.blob_sha256s).issubset(
                {item.sha256 for item in self.inventory}
            )
            or SHA256_RE.fullmatch(self.source_root_identity) is None
            or type(self.portable_materialization) is not bool
        ):
            raise ValueError("verified backup integrity authority is malformed")
        _require_digest(
            self.source_backup_manifest_sha256,
            "verified backup source manifest",
        )
        if self.migration_result_sha256 is not None:
            _require_digest(
                self.migration_result_sha256,
                "verified backup migration result",
            )
        _require_digest(self.database_sha256, "verified backup database")
        if type(self.database_length) is not int or self.database_length < 0:
            raise ValueError("verified backup database length is invalid")
        _require_digest(self.snapshot_tree_sha256, "verified backup snapshot tree")
        lease_fields = (
            self.migration_lease_path,
            self.migration_lease_id,
            self.rebackup_lease_path,
            self.rebackup_lease_id,
        )
        if self.migration_result_sha256 is None:
            if any(value is not None for value in lease_fields):
                raise ValueError(
                    "ordinary backup authority cannot bind migration leases"
                )
        elif any(not isinstance(value, str) or not value for value in lease_fields):
            raise ValueError(
                "migrated backup authority requires both exact live leases"
            )
        if not hmac.compare_digest(
            self._issuer_seal,
            _verified_backup_integrity_authority_seal(
                backup_path=self.backup_path,
                database=self.database,
                cas_root=self.cas_root,
                source_root_identity=self.source_root_identity,
                portable_materialization=self.portable_materialization,
                closure=self.closure,
                inventory=self.inventory,
                source_backup_manifest_sha256=self.source_backup_manifest_sha256,
                migration_result_sha256=self.migration_result_sha256,
                database_sha256=self.database_sha256,
                database_length=self.database_length,
                snapshot_tree_sha256=self.snapshot_tree_sha256,
                migration_lease_path=self.migration_lease_path,
                migration_lease_id=self.migration_lease_id,
                rebackup_lease_path=self.rebackup_lease_path,
                rebackup_lease_id=self.rebackup_lease_id,
            ),
        ):
            raise ValueError("verified backup integrity authority seal is stale")

    def verify_issued(self) -> None:
        if (
            type(self) is not _VerifiedBackupIntegrityAuthority
            or self._issuer_token is not _RECOVERY_ISSUER_TOKEN
            or not hmac.compare_digest(
                self._issuer_seal,
                _verified_backup_integrity_authority_seal(
                    backup_path=self.backup_path,
                    database=self.database,
                    cas_root=self.cas_root,
                    source_root_identity=self.source_root_identity,
                    portable_materialization=self.portable_materialization,
                    closure=self.closure,
                    inventory=self.inventory,
                    source_backup_manifest_sha256=(self.source_backup_manifest_sha256),
                    migration_result_sha256=self.migration_result_sha256,
                    database_sha256=self.database_sha256,
                    database_length=self.database_length,
                    snapshot_tree_sha256=self.snapshot_tree_sha256,
                    migration_lease_path=self.migration_lease_path,
                    migration_lease_id=self.migration_lease_id,
                    rebackup_lease_path=self.rebackup_lease_path,
                    rebackup_lease_id=self.rebackup_lease_id,
                ),
            )
        ):
            raise RecoveryError(
                "backup_integrity_authority_unissued",
                "backup semantic verification requires exact Recovery authority",
            )


def _verified_backup_integrity_authority_payload(
    *,
    backup_path: Path,
    database: Path,
    cas_root: Path,
    source_root_identity: str,
    portable_materialization: bool,
    closure: ClosureManifest,
    inventory: tuple[BlobRecord, ...],
    source_backup_manifest_sha256: str,
    migration_result_sha256: str | None,
    database_sha256: str,
    database_length: int,
    snapshot_tree_sha256: str,
    migration_lease_path: str | None,
    migration_lease_id: str | None,
    rebackup_lease_path: str | None,
    rebackup_lease_id: str | None,
) -> dict[str, Any]:
    return {
        "backup_path": str(backup_path),
        "database": str(database),
        "cas_root": str(cas_root),
        "source_root_identity": source_root_identity,
        "portable_materialization": portable_materialization,
        "closure": closure.to_payload(),
        "inventory": [item.to_payload() for item in inventory],
        "source_backup_manifest_sha256": source_backup_manifest_sha256,
        "migration_result_sha256": migration_result_sha256,
        "database_sha256": database_sha256,
        "database_length": database_length,
        "snapshot_tree_sha256": snapshot_tree_sha256,
        "migration_lease_path": migration_lease_path,
        "migration_lease_id": migration_lease_id,
        "rebackup_lease_path": rebackup_lease_path,
        "rebackup_lease_id": rebackup_lease_id,
    }


def _verified_backup_integrity_authority_seal(
    *,
    backup_path: Path,
    database: Path,
    cas_root: Path,
    source_root_identity: str,
    portable_materialization: bool,
    closure: ClosureManifest,
    inventory: tuple[BlobRecord, ...],
    source_backup_manifest_sha256: str,
    migration_result_sha256: str | None,
    database_sha256: str,
    database_length: int,
    snapshot_tree_sha256: str,
    migration_lease_path: str | None,
    migration_lease_id: str | None,
    rebackup_lease_path: str | None,
    rebackup_lease_id: str | None,
) -> str:
    return hmac.new(
        _RECOVERY_ISSUER_SECRET,
        canonical_json_bytes(
            {
                "domain": "workspace_recovery.verified_backup_integrity.v1",
                "authority": _verified_backup_integrity_authority_payload(
                    backup_path=backup_path,
                    database=database,
                    cas_root=cas_root,
                    source_root_identity=source_root_identity,
                    portable_materialization=portable_materialization,
                    closure=closure,
                    inventory=inventory,
                    source_backup_manifest_sha256=source_backup_manifest_sha256,
                    migration_result_sha256=migration_result_sha256,
                    database_sha256=database_sha256,
                    database_length=database_length,
                    snapshot_tree_sha256=snapshot_tree_sha256,
                    migration_lease_path=migration_lease_path,
                    migration_lease_id=migration_lease_id,
                    rebackup_lease_path=rebackup_lease_path,
                    rebackup_lease_id=rebackup_lease_id,
                ),
            }
        ),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class RecoveryState:
    lifecycle: RecoveryLifecycle
    previous_lifecycle: RecoveryLifecycle
    writer_epoch: int | None
    clean_shutdown_observed: bool
    mission_auto_resume: bool = False
    reason: str = "startup_reconciliation_required"


@dataclass(frozen=True)
class ExternalReconciliation:
    unresolved_attempt_ids: tuple[str, ...] = ()
    unknown_effect_attempt_ids: tuple[str, ...] = ()
    late_result_ids: tuple[str, ...] = ()
    unsettled_reservation_ids: tuple[str, ...] = ()
    persisted_state_sha256: str = "0" * 64

    def __post_init__(self) -> None:
        for name in (
            "unresolved_attempt_ids",
            "unknown_effect_attempt_ids",
            "late_result_ids",
            "unsettled_reservation_ids",
        ):
            values = tuple(sorted(set(getattr(self, name))))
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{name} requires non-empty persisted IDs")
            object.__setattr__(self, name, values)
        _require_digest(self.persisted_state_sha256, "reconciliation state digest")

    @property
    def blocks_takeover(self) -> bool:
        return bool(
            self.unresolved_attempt_ids
            or self.unknown_effect_attempt_ids
            or self.unsettled_reservation_ids
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "unresolved_attempt_ids": list(self.unresolved_attempt_ids),
            "unknown_effect_attempt_ids": list(self.unknown_effect_attempt_ids),
            "late_result_ids": list(self.late_result_ids),
            "unsettled_reservation_ids": list(self.unsettled_reservation_ids),
            "persisted_state_sha256": self.persisted_state_sha256,
            "blocks_takeover": self.blocks_takeover,
        }


@dataclass(frozen=True)
class RestoreVerification:
    restored_root_sha256: str
    deletion_fence_sha256: str
    read_only_seal_sha256: str
    sqlite_integrity: str
    evidence_count: int
    verified_at: str


@dataclass(frozen=True)
class RestoredWorkspace:
    root: Path
    backup: BackupSet
    lifecycle: RecoveryLifecycle
    reconciliation: ExternalReconciliation
    verification: RestoreVerification
    selected_live_root: bool = False
    mission_auto_resume: bool = False
    source_materialization_portable: bool = False
    restore_parent: Path | None = None
    caller_selected_target: bool = False
    _fixed_target_token: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TrustedSourceControlVerification:
    """Closed readback supplied by the explicit trusted operational controller."""

    mission_id: str
    source_incarnation: str
    source_root_identity: str
    backup_manifest_sha256: str
    prior_writer_epoch: int
    prior_writer_owner: str
    source_unit: str
    source_unit_user: str
    source_unit_enablement: str
    source_unit_activity: str
    source_unit_control_group: str
    source_unit_tasks_current: int | None
    write_exclusion_source_incarnation: str
    physical_write_exclusion_method: str
    physical_write_exclusion_target: str
    physical_write_exclusion_result: str
    verification_reference: str
    verified_at: str
    _issuer_token: object = field(default=None, repr=False, compare=False)
    _issuer_seal: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_incarnation, "source incarnation"),
            (self.source_root_identity, "source root identity"),
            (self.backup_manifest_sha256, "backup manifest"),
        ):
            _require_digest(value, f"trusted-control {label}")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.mission_id,
                self.prior_writer_owner,
                self.source_unit,
                self.source_unit_user,
                self.physical_write_exclusion_method,
                self.physical_write_exclusion_target,
                self.verification_reference,
                self.verified_at,
            )
        ):
            raise ValueError("trusted-control fence readback fields must be non-empty")
        if isinstance(self.prior_writer_epoch, bool) or self.prior_writer_epoch < 1:
            raise ValueError("trusted-control prior writer epoch must be positive")
        if (
            self.source_unit_enablement != "disabled"
            or self.source_unit_activity != "inactive"
            or not (
                (
                    self.source_unit_control_group
                    == f"/system.slice/{self.source_unit}"
                    and self.source_unit_tasks_current == 0
                )
                or (
                    self.source_unit_control_group == ""
                    and self.source_unit_tasks_current is None
                )
            )
            or self.write_exclusion_source_incarnation != self.source_incarnation
            or self.physical_write_exclusion_result
            != "old_incarnation_cannot_start_or_write"
            or self.physical_write_exclusion_method.casefold()
            in {
                "unreachable",
                "network_unreachable",
                "ssh_unreachable",
                "health_probe_failed",
            }
        ):
            raise ValueError(
                "trusted-control readback must prove disabled+inactive source control "
                "and positive physical write exclusion"
            )
        _validate_source_unit(self.source_unit)
        if self._issuer_token is not _RECOVERY_ISSUER_TOKEN:
            raise ValueError(
                "trusted-control verification is issued only by the local readback adapter"
            )
        expected_seal = hmac.new(
            _RECOVERY_ISSUER_SECRET,
            canonical_json_bytes(self.to_payload()),
            hashlib.sha256,
        ).hexdigest()
        if self._issuer_seal and not hmac.compare_digest(
            self._issuer_seal,
            expected_seal,
        ):
            raise ValueError("trusted-control verification issuer binding is stale")
        object.__setattr__(self, "_issuer_seal", expected_seal)

    @property
    def control_verification_sha256(self) -> str:
        payload = self.to_payload()
        payload.pop("verified_at")
        return _digest(canonical_json_bytes(payload))

    def verify_issued(self) -> None:
        expected_seal = hmac.new(
            _RECOVERY_ISSUER_SECRET,
            canonical_json_bytes(self.to_payload()),
            hashlib.sha256,
        ).hexdigest()
        if self._issuer_token is not _RECOVERY_ISSUER_TOKEN or not hmac.compare_digest(
            self._issuer_seal,
            expected_seal,
        ):
            raise RecoveryError(
                "source_control_verification_unissued",
                "source-control verification was not issued by actual local readback",
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "mathematical_research.source_control_fence_readback.v2",
            "mission_id": self.mission_id,
            "source_incarnation": self.source_incarnation,
            "source_root_identity": self.source_root_identity,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "prior_writer_epoch": self.prior_writer_epoch,
            "prior_writer_owner": self.prior_writer_owner,
            "source_unit": self.source_unit,
            "source_unit_user": self.source_unit_user,
            "source_unit_enablement": self.source_unit_enablement,
            "source_unit_activity": self.source_unit_activity,
            "source_unit_control_group": self.source_unit_control_group,
            "source_unit_tasks_current": self.source_unit_tasks_current,
            "write_exclusion_source_incarnation": (
                self.write_exclusion_source_incarnation
            ),
            "physical_write_exclusion_method": self.physical_write_exclusion_method,
            "physical_write_exclusion_target": self.physical_write_exclusion_target,
            "physical_write_exclusion_result": self.physical_write_exclusion_result,
            "verification_reference": self.verification_reference,
            "verified_at": self.verified_at,
        }


def _validate_source_unit(source_unit: str) -> str:
    """Accept an explicit exact service identity, never an inferred host target."""

    if (
        not isinstance(source_unit, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*[.]service", source_unit) is None
    ):
        raise ValueError("source_unit must be one explicit exact systemd service name")
    return source_unit


def _closed_source_unit_readback(*, source_unit: str) -> Mapping[str, str]:
    """Read only the operator-selected unit and require complete quiescent facts."""

    _validate_source_unit(source_unit)

    if os.name != "posix":
        raise RecoveryError(
            "trusted_source_control_unavailable",
            "source-control readback requires the local Linux service process",
        )
    result = subprocess.run(
        [
            str(_SYSTEMCTL),
            "show",
            source_unit,
            "--property=LoadState,UnitFileState,ActiveState,SubState,MainPID,"
            "User,ControlGroup,TasksCurrent",
            "--no-pager",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stderr:
        raise RecoveryError(
            "trusted_source_control_unavailable",
            "fixed systemd source-control readback failed",
        )
    expected_keys = {
        "LoadState",
        "UnitFileState",
        "ActiveState",
        "SubState",
        "MainPID",
        "User",
        "ControlGroup",
        "TasksCurrent",
    }
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected_keys or key in values:
            raise RecoveryError(
                "trusted_source_control_invalid",
                "fixed systemd source-control readback is not closed",
            )
        values[key] = value
    if set(values) != expected_keys:
        raise RecoveryError(
            "trusted_source_control_invalid",
            "fixed systemd source-control readback is incomplete",
        )
    if (
        values["LoadState"] != "loaded"
        or values["UnitFileState"] != "disabled"
        or values["ActiveState"] != "inactive"
        or values["SubState"] != "dead"
        or values["MainPID"] != "0"
        or not (
            (
                values["ControlGroup"] == f"/system.slice/{source_unit}"
                and values["TasksCurrent"] == "0"
            )
            or (
                values["ControlGroup"] == ""
                and values["TasksCurrent"] == "[not set]"
            )
        )
        or not values["User"]
        or values["User"] == "root"
    ):
        raise RecoveryError(
            "trusted_source_control_not_fenced",
            "source unit is not loaded, disabled, inactive, dead, bound to its "
            "empty assigned-or-unallocated cgroup, and owned by a non-root "
            "service principal",
        )
    return deep_freeze(values)


def _verify_source_tree_write_exclusion(
    source_root: Path,
    *,
    service_user: str,
) -> str:
    """Prove this exact unit process cannot write or replace source-root members."""

    root = source_root.resolve(strict=True)
    _assert_plain_tree(root, code="trusted_source_control_invalid")
    checked = (root.parent, root, *tuple(sorted(root.rglob("*"))))
    for path in checked:
        result = subprocess.run(
            [
                str(_POSIX_TEST),
                "-w",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout or result.stderr or result.returncode not in {0, 1}:
            raise RecoveryError(
                "trusted_source_control_invalid",
                "service-principal physical write-exclusion readback failed",
            )
        if result.returncode == 0:
            raise RecoveryError(
                "trusted_source_control_not_fenced",
                "source unit principal can still write or replace source material",
            )
    return _digest(
        canonical_json_bytes(
            {
                "method": "posix_service_principal_tree_write_denied",
                "source_root": str(root),
                "service_user": service_user,
                "checked_paths": [str(path) for path in checked],
            }
        )
    )


def _verify_absent_source_path_write_exclusion(
    source_root: Path,
    *,
    service_user: str,
) -> str:
    """Prove an absent fixed source cannot be recreated by the service owner."""

    if os.path.lexists(source_root):
        raise RecoveryError(
            "trusted_source_control_not_fenced",
            "the source path expected to be absent is present",
        )
    source_parent = source_root.parent
    state_root = source_parent.parent
    state_parent = state_root.parent
    try:
        parent = source_parent.resolve(strict=True)
        resolved_state_root = state_root.resolve(strict=True)
        resolved_state_parent = state_parent.resolve(strict=True)
        checked = (resolved_state_parent, resolved_state_root, parent)
        metadata = tuple(os.lstat(path) for path in checked)
    except OSError as exc:
        raise RecoveryError(
            "trusted_source_control_invalid",
            "the absent source parent cannot be authenticated",
        ) from exc
    if (
        parent != source_parent.absolute()
        or resolved_state_root != state_root.absolute()
        or resolved_state_parent != state_parent.absolute()
        or any(_is_reparse_point(path) for path in checked)
        or any(not stat.S_ISDIR(item.st_mode) for item in metadata)
        or any(item.st_uid != 0 or item.st_gid != 0 for item in metadata)
        or any(stat.S_IMODE(item.st_mode) & 0o022 for item in metadata)
    ):
        raise RecoveryError(
            "trusted_source_control_invalid",
            "the absent source parent is redirected or is not one directory",
        )
    for path in checked:
        result = subprocess.run(
            [str(_POSIX_TEST), "-w", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout or result.stderr or result.returncode not in {0, 1}:
            raise RecoveryError(
                "trusted_source_control_invalid",
                "service-principal absent-path exclusion readback failed",
            )
        if result.returncode == 0:
            raise RecoveryError(
                "trusted_source_control_not_fenced",
                "source-unit principal can replace or recreate the absent source path",
            )
    if os.path.lexists(source_root):
        raise RecoveryError(
            "trusted_source_control_not_fenced",
            "the absent source path reappeared during exclusion readback",
        )
    return _digest(
        canonical_json_bytes(
            {
                "method": "posix_service_principal_absent_path_creation_denied",
                "source_root": str(source_root.absolute()),
                "service_user": service_user,
                "checked_paths": [str(path) for path in checked],
            }
        )
    )


def _verify_trusted_source_control(
    backup: BackupSet,
    *,
    source_root: Path | str,
    source_unit: str,
) -> TrustedSourceControlVerification:
    """Issue process-local authority only from exact systemd and POSIX readbacks."""

    verified = _verify_portable_backup_for_trust_transition(backup)
    source = verified.manifest.checkpoint_source
    if source is None:
        raise RecoveryError(
            "source_incarnation_fence_unavailable",
            "legacy BackupSet has no current checkpoint-source incarnation",
        )
    requested_root = Path(source_root)
    if not requested_root.is_absolute():
        raise RecoveryError(
            "trusted_source_control_invalid",
            "source-control readback requires one absolute source path",
        )
    root = requested_root.absolute()
    source_present = os.path.lexists(root)
    if source_present:
        try:
            root = requested_root.resolve(strict=True)
            if root != requested_root.absolute():
                raise WorkspacePathError("source path is redirected")
            paths = WorkspacePaths.from_root(root)
            paths.revalidate_physical(require_root=True, require_database=True)
        except (OSError, WorkspacePathError) as exc:
            raise RecoveryError(
                "trusted_source_control_invalid",
                "the present source path is redirected or cannot be authenticated",
            ) from exc
        if paths.root_identity != source.source_root_identity:
            raise RecoveryError(
                "source_incarnation_fence_binding_mismatch",
                "physical source root differs from the checkpoint-source incarnation",
            )
    unit = _closed_source_unit_readback(source_unit=source_unit)
    principal = attest_current_principal()
    if (
        principal.principal_name != str(unit["User"])
        or stable_principal_owner_binding(principal) != source.prior_writer_owner
    ):
        raise RecoveryError(
            "source_incarnation_fence_binding_mismatch",
            "recovery process is not the exact disabled source-unit prior writer",
        )
    if source_present:
        exclusion_method = "posix_service_principal_tree_write_denied"
        exclusion_reference = _verify_source_tree_write_exclusion(
            root,
            service_user=str(unit["User"]),
        )
    else:
        exclusion_method = "posix_service_principal_absent_path_creation_denied"
        exclusion_reference = _verify_absent_source_path_write_exclusion(
            root,
            service_user=str(unit["User"]),
        )
    readback_reference = _digest(
        canonical_json_bytes(
            {
                "unit": deep_thaw(unit),
                "physical_exclusion_sha256": exclusion_reference,
            }
        )
    )
    return TrustedSourceControlVerification(
        mission_id=source.mission_id,
        source_incarnation=source.source_incarnation,
        source_root_identity=source.source_root_identity,
        backup_manifest_sha256=verified.manifest.manifest_sha256,
        prior_writer_epoch=source.prior_writer_epoch,
        prior_writer_owner=source.prior_writer_owner,
        source_unit=source_unit,
        source_unit_user=str(unit["User"]),
        source_unit_enablement=str(unit["UnitFileState"]),
        source_unit_activity=str(unit["ActiveState"]),
        source_unit_control_group=str(unit["ControlGroup"]),
        source_unit_tasks_current=(
            None
            if unit["TasksCurrent"] == "[not set]"
            else int(unit["TasksCurrent"])
        ),
        write_exclusion_source_incarnation=source.source_incarnation,
        physical_write_exclusion_method=exclusion_method,
        physical_write_exclusion_target=str(root),
        physical_write_exclusion_result="old_incarnation_cannot_start_or_write",
        verification_reference=readback_reference,
        verified_at=_now(),
        _issuer_token=_RECOVERY_ISSUER_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class SourceIncarnationFence:
    """Process-local capability derived from one trusted-control readback."""

    mission_id: str
    source_incarnation: str
    source_root_identity: str
    backup_manifest_sha256: str
    prior_writer_epoch: int
    prior_writer_owner: str
    source_unit: str
    source_unit_user: str
    physical_write_exclusion_target: str
    control_verification_sha256: str
    verification_reference: str
    _issuer_token: object = field(default=None, repr=False, compare=False)
    _issuer_seal: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_incarnation, "source incarnation"),
            (self.source_root_identity, "source root identity"),
            (self.backup_manifest_sha256, "backup manifest"),
            (self.control_verification_sha256, "control verification"),
        ):
            _require_digest(value, f"source fence {label}")
        if (
            not self.mission_id
            or not self.prior_writer_owner
            or not self.source_unit
            or not self.source_unit_user
            or not self.physical_write_exclusion_target
            or not self.verification_reference
            or self.prior_writer_epoch < 1
            or self._issuer_token is not _RECOVERY_ISSUER_TOKEN
        ):
            raise ValueError("source-incarnation fence binding is invalid")
        expected_seal = hmac.new(
            _RECOVERY_ISSUER_SECRET,
            canonical_json_bytes(self.to_payload()),
            hashlib.sha256,
        ).hexdigest()
        if self._issuer_seal and not hmac.compare_digest(
            self._issuer_seal,
            expected_seal,
        ):
            raise ValueError("source-incarnation fence issuer binding is stale")
        object.__setattr__(self, "_issuer_seal", expected_seal)

    @property
    def fence_sha256(self) -> str:
        return _digest(canonical_json_bytes(self.to_payload()))

    def verify_issued(self) -> None:
        expected_seal = hmac.new(
            _RECOVERY_ISSUER_SECRET,
            canonical_json_bytes(self.to_payload()),
            hashlib.sha256,
        ).hexdigest()
        if self._issuer_token is not _RECOVERY_ISSUER_TOKEN or not hmac.compare_digest(
            self._issuer_seal,
            expected_seal,
        ):
            raise RecoveryError(
                "source_incarnation_fence_unissued",
                "source-incarnation fence was not issued from trusted-control readback",
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "mathematical_research.source_incarnation_fence.v1",
            "mission_id": self.mission_id,
            "source_incarnation": self.source_incarnation,
            "source_root_identity": self.source_root_identity,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "prior_writer_epoch": self.prior_writer_epoch,
            "prior_writer_owner": self.prior_writer_owner,
            "source_unit": self.source_unit,
            "source_unit_user": self.source_unit_user,
            "physical_write_exclusion_target": self.physical_write_exclusion_target,
            "control_verification_sha256": self.control_verification_sha256,
            "verification_reference": self.verification_reference,
        }


def _issue_source_incarnation_fence(
    backup: BackupSet,
    *,
    trusted_control_verification: TrustedSourceControlVerification,
) -> SourceIncarnationFence:
    """Issue only from exact disabled/inactive and physical-exclusion readback."""

    if type(trusted_control_verification) is not TrustedSourceControlVerification:
        raise TypeError("source fence requires exact trusted-control verification")
    trusted_control_verification.verify_issued()
    fresh_control = _verify_trusted_source_control(
        backup,
        source_root=trusted_control_verification.physical_write_exclusion_target,
        source_unit=trusted_control_verification.source_unit,
    )
    if (
        fresh_control.control_verification_sha256
        != trusted_control_verification.control_verification_sha256
    ):
        raise RecoveryError(
            "source_control_verification_stale",
            "source-control or physical write-exclusion readback changed before fencing",
        )
    verified = _verify_portable_backup_for_trust_transition(backup)
    source = verified.manifest.checkpoint_source
    if source is None:
        raise RecoveryError(
            "source_incarnation_fence_unavailable",
            "legacy BackupSet has no current checkpoint-source incarnation",
        )
    expected = (
        source.mission_id,
        source.source_incarnation,
        source.source_root_identity,
        verified.manifest.manifest_sha256,
        source.prior_writer_epoch,
        source.prior_writer_owner,
    )
    actual = (
        trusted_control_verification.mission_id,
        trusted_control_verification.source_incarnation,
        trusted_control_verification.source_root_identity,
        trusted_control_verification.backup_manifest_sha256,
        trusted_control_verification.prior_writer_epoch,
        trusted_control_verification.prior_writer_owner,
    )
    if actual != expected:
        raise RecoveryError(
            "source_incarnation_fence_binding_mismatch",
            "trusted-control readback names another source, Mission, backup, or writer",
        )
    return SourceIncarnationFence(
        mission_id=source.mission_id,
        source_incarnation=source.source_incarnation,
        source_root_identity=source.source_root_identity,
        backup_manifest_sha256=verified.manifest.manifest_sha256,
        prior_writer_epoch=source.prior_writer_epoch,
        prior_writer_owner=source.prior_writer_owner,
        source_unit=trusted_control_verification.source_unit,
        source_unit_user=trusted_control_verification.source_unit_user,
        physical_write_exclusion_target=(
            trusted_control_verification.physical_write_exclusion_target
        ),
        control_verification_sha256=(
            trusted_control_verification.control_verification_sha256
        ),
        verification_reference=trusted_control_verification.verification_reference,
        _issuer_token=_RECOVERY_ISSUER_TOKEN,
    )


@dataclass(frozen=True)
class WriterFenceProof:
    evidence_id: str
    evidence_revision: int
    evidence_payload_sha256: str

    def __post_init__(self) -> None:
        if not self.evidence_id or self.evidence_revision < 1:
            raise ValueError(
                "writer-fence proof requires an exact persisted Evidence revision"
            )
        _require_digest(self.evidence_payload_sha256, "writer-fence Evidence payload")


@dataclass(frozen=True)
class TakeoverPlan:
    restored_root: Path
    source_root_identity: str
    target_root_identity: str
    backup_manifest_sha256: str
    canonical: CanonicalBinding
    prior_epoch: int | None
    prior_owner: str | None
    proposed_epoch: int
    lifecycle: RecoveryLifecycle
    fence_evidence_sha256: str | None
    fence_evidence_id: str | None
    fence_evidence_revision: int | None
    restored_root_sha256: str
    read_only_seal_sha256: str
    reconciliation: ExternalReconciliation
    mission_auto_resume: bool = False
    canonical_effect: str = "none"
    source_incarnation: str | None = None
    source_mission_id: str | None = None
    operational_fence_sha256: str | None = None
    _issuer_token: object = field(default=None, repr=False, compare=False)
    _issuer_seal: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_digest(self.source_root_identity, "takeover source-root identity")
        _require_digest(self.target_root_identity, "takeover target-root identity")
        _require_digest(self.backup_manifest_sha256, "takeover backup manifest")
        _require_digest(self.read_only_seal_sha256, "takeover read-only seal")
        _require_digest(self.restored_root_sha256, "takeover restored-root digest")
        if self.source_incarnation is None:
            if (
                self.source_mission_id is not None
                or self.operational_fence_sha256 is not None
                or self.fence_evidence_sha256 is None
                or self.fence_evidence_id is None
                or self.fence_evidence_revision is None
                or self.fence_evidence_revision < 1
            ):
                raise ValueError("legacy takeover requires exact fence Evidence identity")
            _require_digest(self.fence_evidence_sha256, "takeover fence evidence")
        else:
            _require_digest(self.source_incarnation, "takeover source incarnation")
            if (
                not self.source_mission_id
                or self.operational_fence_sha256 is None
                or any(
                    value is not None
                    for value in (
                        self.fence_evidence_sha256,
                        self.fence_evidence_id,
                        self.fence_evidence_revision,
                    )
                )
            ):
                raise ValueError(
                    "current takeover requires one operational source-incarnation fence"
                )
            _require_digest(
                self.operational_fence_sha256,
                "takeover operational fence",
            )
        if self.lifecycle is not RecoveryLifecycle.TAKEOVER_PENDING:
            raise ValueError("takeover plans must remain pending")
        expected_epoch = (self.prior_epoch or 0) + 1
        if self.proposed_epoch != expected_epoch:
            raise ValueError(
                "takeover proposed epoch must follow the fenced high-watermark"
            )
        if self.mission_auto_resume or self.canonical_effect != "none":
            raise ValueError(
                "takeover plans cannot resume Missions or affect canonical truth"
            )
        if self._issuer_token is not _RECOVERY_ISSUER_TOKEN:
            raise ValueError(
                "takeover plans are issued only by verified recovery preparation"
            )
        expected_seal = hmac.new(
            _RECOVERY_ISSUER_SECRET,
            canonical_json_bytes(self.to_payload()),
            hashlib.sha256,
        ).hexdigest()
        if self._issuer_seal and not hmac.compare_digest(
            self._issuer_seal, expected_seal
        ):
            raise ValueError("takeover plan issuer binding is stale")
        object.__setattr__(self, "_issuer_seal", expected_seal)

    def verify_issued(self) -> None:
        expected_seal = hmac.new(
            _RECOVERY_ISSUER_SECRET,
            canonical_json_bytes(self.to_payload()),
            hashlib.sha256,
        ).hexdigest()
        if self._issuer_token is not _RECOVERY_ISSUER_TOKEN or not hmac.compare_digest(
            self._issuer_seal, expected_seal
        ):
            raise RecoveryError(
                "takeover_plan_unissued",
                "takeover plan was not issued by verified recovery preparation",
            )

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "source_root_identity": self.source_root_identity,
            "target_root_identity": self.target_root_identity,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "canonical": self.canonical.to_payload(),
            "prior_epoch": self.prior_epoch,
            "prior_owner": self.prior_owner,
            "proposed_epoch": self.proposed_epoch,
            "lifecycle": self.lifecycle.value,
            "fence_evidence_sha256": self.fence_evidence_sha256,
            "fence_evidence_id": self.fence_evidence_id,
            "fence_evidence_revision": self.fence_evidence_revision,
            "restored_root_sha256": self.restored_root_sha256,
            "read_only_seal_sha256": self.read_only_seal_sha256,
            "reconciliation": self.reconciliation.to_payload(),
            "mission_auto_resume": self.mission_auto_resume,
            "canonical_effect": self.canonical_effect,
        }
        if self.source_incarnation is not None:
            payload.update(
                {
                    "source_incarnation": self.source_incarnation,
                    "source_mission_id": self.source_mission_id,
                    "operational_fence_sha256": self.operational_fence_sha256,
                }
            )
        return payload


@dataclass(frozen=True)
class TakeoverPermit:
    plan: TakeoverPlan
    command_id: str
    actor: str
    explicit: bool
    canonical_freshness_sha256: str
    canonical_authority_root: Path
    writer_creation_basis: str = "explicit takeover"
    proposed_lifecycle: RecoveryLifecycle = RecoveryLifecycle.ACTIVE
    requires_writer_transaction: bool = True
    mission_auto_resume: bool = False
    canonical_effect: str = "none"
    canonical_verification_kind: str = "git_head"
    installed_release_sha: str | None = None
    installed_release_bundle_sha256: str | None = None
    installed_release_record_sha256: str | None = None
    _issuer_token: object = field(default=None, repr=False, compare=False)
    _issuer_seal: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        self.plan.verify_issued()
        if (
            not self.command_id
            or not self.actor
            or not self.writer_creation_basis
            or not self.explicit
            or self.proposed_lifecycle is not RecoveryLifecycle.ACTIVE
            or not self.requires_writer_transaction
            or self.mission_auto_resume
            or self.canonical_effect != "none"
        ):
            raise ValueError(
                "takeover permit is not an explicit proof-neutral activation permit"
            )
        _require_digest(self.canonical_freshness_sha256, "takeover canonical freshness")
        authority_root = Path(self.canonical_authority_root).resolve(strict=True)
        if not authority_root.is_dir():
            raise ValueError("takeover canonical authority root must be a directory")
        object.__setattr__(self, "canonical_authority_root", authority_root)
        if self.canonical_verification_kind == "git_head":
            if any(
                value is not None
                for value in (
                    self.installed_release_sha,
                    self.installed_release_bundle_sha256,
                    self.installed_release_record_sha256,
                )
            ):
                raise ValueError(
                    "Git-HEAD takeover cannot carry installed-release authority"
                )
        elif self.canonical_verification_kind == "installed_release_archive":
            if (
                not isinstance(self.installed_release_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", self.installed_release_sha) is None
                or self.installed_release_bundle_sha256 is None
                or self.installed_release_record_sha256 is None
            ):
                raise ValueError(
                    "installed-release takeover lacks exact release authority"
                )
            _require_digest(
                self.installed_release_bundle_sha256,
                "takeover installed-release bundle",
            )
            _require_digest(
                self.installed_release_record_sha256,
                "takeover installed-release record",
            )
        else:
            raise ValueError("unsupported takeover canonical verification kind")
        if self._issuer_token is not _RECOVERY_ISSUER_TOKEN:
            raise ValueError(
                "takeover permits are issued only by explicit recovery confirmation"
            )
        expected_seal = hmac.new(
            _RECOVERY_ISSUER_SECRET,
            canonical_json_bytes(self.to_payload()),
            hashlib.sha256,
        ).hexdigest()
        if self._issuer_seal and not hmac.compare_digest(
            self._issuer_seal, expected_seal
        ):
            raise ValueError("takeover permit issuer binding is stale")
        object.__setattr__(self, "_issuer_seal", expected_seal)

    def verify_issued(self) -> None:
        self.plan.verify_issued()
        expected_seal = hmac.new(
            _RECOVERY_ISSUER_SECRET,
            canonical_json_bytes(self.to_payload()),
            hashlib.sha256,
        ).hexdigest()
        if self._issuer_token is not _RECOVERY_ISSUER_TOKEN or not hmac.compare_digest(
            self._issuer_seal, expected_seal
        ):
            raise RecoveryError(
                "takeover_permit_unissued",
                "takeover permit was not issued by explicit recovery confirmation",
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_payload(),
            "command_id": self.command_id,
            "actor": self.actor,
            "writer_creation_basis": self.writer_creation_basis,
            "explicit": self.explicit,
            "canonical_freshness_sha256": self.canonical_freshness_sha256,
            "canonical_authority_root": str(self.canonical_authority_root),
            "proposed_lifecycle": self.proposed_lifecycle.value,
            "requires_writer_transaction": self.requires_writer_transaction,
            "mission_auto_resume": self.mission_auto_resume,
            "canonical_effect": self.canonical_effect,
            "canonical_verification_kind": self.canonical_verification_kind,
            "installed_release_sha": self.installed_release_sha,
            "installed_release_bundle_sha256": (
                self.installed_release_bundle_sha256
            ),
            "installed_release_record_sha256": (
                self.installed_release_record_sha256
            ),
        }


@dataclass(frozen=True)
class OfflineMigrationPlan:
    current_schema_version: int
    target_schema_version: int
    verified_backup_manifest_sha256: str
    applied_history_sha256: str
    pending_migrations: tuple[tuple[int, str, str], ...]
    lifecycle: RecoveryLifecycle
    automatic: bool = False
    downgrade: bool = False


@dataclass(frozen=True)
class _PersistedBackupEvidence:
    closure: ClosureManifest
    blobs: Mapping[str, BlobRecord]
    cas_inventory_contract: str
    deletion_directives: tuple[DeletionDirective, ...]
    tombstones: tuple[EvidenceTombstone, ...]
    metadata_sha256: str

    @property
    def inventory(self) -> tuple[BlobRecord, ...]:
        return tuple(self.blobs[digest] for digest in sorted(self.blobs))


def begin_recovery(
    observed_lifecycle: RecoveryLifecycle | str,
    *,
    writer_epoch: int | None,
    clean_shutdown_observed: bool,
) -> RecoveryState:
    observed = RecoveryLifecycle(observed_lifecycle)
    if writer_epoch is not None and writer_epoch < 1:
        raise ValueError("writer_epoch must be positive or absent")
    reason = (
        "clean_offline_verification_required"
        if observed is RecoveryLifecycle.OFFLINE and clean_shutdown_observed
        else "unclean_or_nonterminal_writer_requires_reconciliation"
    )
    return RecoveryState(
        lifecycle=RecoveryLifecycle.RECOVERING,
        previous_lifecycle=observed,
        writer_epoch=writer_epoch,
        clean_shutdown_observed=clean_shutdown_observed,
        reason=reason,
    )


def transition_recovery(
    state: RecoveryState,
    target: RecoveryLifecycle | str,
    *,
    reason: str,
) -> RecoveryState:
    target_state = RecoveryLifecycle(target)
    if target_state not in ALLOWED_TRANSITIONS[state.lifecycle]:
        raise RecoveryError(
            "invalid_recovery_transition",
            f"{state.lifecycle.value} cannot transition to {target_state.value}",
        )
    return RecoveryState(
        lifecycle=target_state,
        previous_lifecycle=state.lifecycle,
        writer_epoch=state.writer_epoch,
        clean_shutdown_observed=state.clean_shutdown_observed,
        reason=reason,
    )


def _canonical_binding_from_authority(
    authority: Mapping[str, Any],
    *,
    source_commit: str,
) -> CanonicalBinding:
    if authority.get("binding_version") == 2:
        return CanonicalBinding(
            path=str(authority["canonical_state_path"]),
            sha256=str(authority["canonical_state_sha256"]),
            source_commit=source_commit,
            authority_vector=authority,
        )
    if "binding_version" not in authority:
        return CanonicalBinding(
            path=str(authority["canonical_state_path"]),
            sha256=str(authority["canonical_state_sha256"]),
            source_commit=source_commit,
            authority_vector=authority,
            validation_contract_sha256=str(authority["validation_contract_sha256"]),
        )
    raise ValueError("unsupported canonical authority binding version")


def _canonical_binding_from_metadata(
    metadata: Mapping[str, Any],
    *,
    source_commit: str,
) -> CanonicalBinding:
    try:
        authority_raw = str(metadata["canonical_authority_json"]).encode("utf-8")
        authority = loads_strict_json_object(authority_raw)
        if canonical_json_bytes(authority) != authority_raw:
            raise ValueError("stored canonical authority is not exact canonical JSON")
        binding = _canonical_binding_from_authority(
            authority,
            source_commit=source_commit,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError(
            "workspace_canonical_binding_invalid",
            f"WorkspaceStore lacks an exact canonical authority vector: {exc}",
        ) from exc
    if str(metadata["canonical_authority_digest"]) != _digest(authority_raw):
        raise RecoveryError(
            "workspace_canonical_binding_invalid", "stored authority digest mismatch"
        )
    return binding


def _canonical_binding_from_store(
    store: WorkspaceStore,
    *,
    source_commit: str,
) -> CanonicalBinding:
    return _canonical_binding_from_metadata(
        store.read_metadata(),
        source_commit=source_commit,
    )


def _canonical_binding_from_database(
    database: Path,
    *,
    source_commit: str,
) -> CanonicalBinding:
    connection = open_snapshot_connection(database)
    try:
        metadata = connection.execute(
            "SELECT * FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None:
            raise RecoveryError(
                "workspace_canonical_binding_invalid",
                "copied WorkspaceStore metadata is absent",
            )
        return _canonical_binding_from_metadata(
            metadata,
            source_commit=source_commit,
        )
    finally:
        connection.close()


def _canonical_binding_from_installed_snapshot(database: Path) -> CanonicalBinding:
    """Read the canonical source commit from copied schema-9 authority."""

    connection = open_snapshot_connection(database)
    try:
        metadata = connection.execute(
            "SELECT * FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None:
            raise RecoveryError(
                "workspace_canonical_binding_invalid",
                "copied WorkspaceStore metadata is absent",
            )
        try:
            authority_raw = str(metadata["canonical_authority_json"]).encode("utf-8")
            authority = loads_strict_json_object(authority_raw)
            source_commit = authority["source_commit"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise RecoveryError(
                "workspace_canonical_binding_invalid",
                "copied schema-9 authority has no exact source commit",
            ) from exc
        if (
            authority.get("binding_version") != 2
            or canonical_json_bytes(authority) != authority_raw
            or not isinstance(source_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        ):
            raise RecoveryError(
                "workspace_canonical_binding_invalid",
                "copied schema-9 authority source commit is invalid",
            )
        return _canonical_binding_from_metadata(
            metadata,
            source_commit=source_commit,
        )
    finally:
        connection.close()


def _verify_canonical_state_bytes(
    binding: CanonicalBinding,
    repo_root: Path | str,
) -> Path:
    root = Path(repo_root).resolve()
    if binding.path != CANONICAL_STATE_REPO_PATH:
        raise RecoveryError(
            "canonical_source_not_authoritative", "binding is not the RH canonical path"
        )
    canonical_path = _contained(root, root / binding.path)
    if canonical_path.is_symlink() or not canonical_path.is_file():
        raise RecoveryError(
            "canonical_source_not_authoritative", "canonical source is unavailable"
        )
    actual_sha256, _ = _sha256_file(canonical_path)
    if actual_sha256 != binding.sha256:
        raise RecoveryError("canonical_binding_stale", "canonical bytes changed")
    return root


def _verify_canonical_source_bytes(
    binding: CanonicalBinding,
    repo_root: Path | str,
) -> tuple[Path, str]:
    root = _verify_canonical_state_bytes(binding, repo_root)
    if binding.validation_contract_sha256 is not None:
        actual_contract = _legacy_validation_contract_digest(root)
        if actual_contract != binding.validation_contract_sha256:
            raise RecoveryError(
                "canonical_binding_stale", "legacy validation-contract bytes changed"
            )
    return root, _digest(canonical_json_bytes(binding.to_payload()))


def verify_live_canonical_binding(
    binding: CanonicalBinding, repo_root: Path | str
) -> str:
    root, binding_digest = _verify_canonical_source_bytes(binding, repo_root)
    if _git_head(root) != binding.source_commit:
        raise RecoveryError(
            "canonical_binding_stale", "repository source commit changed"
        )
    return binding_digest


def _verify_installed_release_canonical_binding(
    binding: CanonicalBinding,
    repo_root: Path | str,
    *,
    exact_source_commit: str,
) -> str:
    """Verify canonical bytes under one separately authenticated installed release.

    This is intentionally separate from :func:`verify_live_canonical_binding`.
    The ordinary repository path still requires a real Git HEAD; the closed
    ``.6`` owner bridge may use this byte verifier only after authenticating the
    immutable installed-release directory and its exact release record. A
    legacy validation-contract digest with no authority-origin commit remains
    historical Store provenance; it is preserved in ``binding`` rather than
    rebound to a later runtime release.
    """

    if (
        not isinstance(exact_source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", exact_source_commit) is None
        or binding.source_commit != exact_source_commit
    ):
        raise RecoveryError(
            "canonical_binding_stale",
            "installed release and canonical binding name different source commits",
        )
    root = _verify_canonical_state_bytes(binding, repo_root)
    authority = binding.authority_payload()
    if (
        binding.validation_contract_sha256 is not None
        and authority.get("source_commit") is not None
        and _legacy_validation_contract_digest(root)
        != binding.validation_contract_sha256
    ):
        raise RecoveryError(
            "canonical_binding_stale", "legacy validation-contract bytes changed"
        )
    return _digest(canonical_json_bytes(binding.to_payload()))


def _verify_installed_release_canonical_archive(
    binding: CanonicalBinding,
    installed_release_root: Path | str,
    *,
    executing_release_sha: str,
) -> tuple[str, str, str]:
    """Bind canonical bytes to an authenticated installed archive and commit.

    The installer record supplies the immutable archive digest and exact
    executing release. Current v2 canonical bytes separately carry the exact
    authority-origin commit. The Git-HEAD verifier remains the ordinary repo
    route and is intentionally not weakened for installed releases.
    """

    if (
        not isinstance(executing_release_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", executing_release_sha) is None
    ):
        raise RecoveryError(
            "installed_release_invalid",
            "executing installed-release SHA is invalid",
        )
    authority = binding.authority_payload()
    if (
        authority.get("binding_version") != 2
        or authority.get("source_commit") != binding.source_commit
    ):
        raise RecoveryError(
            "canonical_source_commit_unavailable",
            "installed takeover requires current canonical bytes with an exact "
            "authority-origin commit",
        )
    from .mission_attempt_runtime import (
        FormalAttemptBridgeError,
        installed_release_source_digest,
    )

    root = Path(installed_release_root).resolve(strict=True)
    record = root / ".mathematical-research-release.json"
    try:
        record_sha256_before, record_length_before = _sha256_file(record)
        bundle_sha256_before = installed_release_source_digest(
            root,
            executing_release_sha,
        )
        binding_sha256 = _verify_installed_release_canonical_binding(
            binding,
            root,
            exact_source_commit=binding.source_commit,
        )
        bundle_sha256_after = installed_release_source_digest(
            root,
            executing_release_sha,
        )
        record_sha256_after, record_length_after = _sha256_file(record)
    except (FormalAttemptBridgeError, OSError) as exc:
        raise RecoveryError("installed_release_invalid", str(exc)) from exc
    if (
        bundle_sha256_before != bundle_sha256_after
        or (record_sha256_before, record_length_before)
        != (record_sha256_after, record_length_after)
    ):
        raise RecoveryError(
            "installed_release_changed",
            "installed release identity changed during canonical verification",
        )
    return binding_sha256, bundle_sha256_before, record_sha256_before


def _verify_takeover_permit_canonical(permit: TakeoverPermit) -> str:
    """Reverify the exact canonical authority mode sealed into one permit."""

    permit.verify_issued()
    if permit.canonical_verification_kind == "git_head":
        return verify_live_canonical_binding(
            permit.plan.canonical,
            permit.canonical_authority_root,
        )
    assert permit.installed_release_sha is not None
    assert permit.installed_release_bundle_sha256 is not None
    assert permit.installed_release_record_sha256 is not None
    binding_sha256, bundle_sha256, record_sha256 = (
        _verify_installed_release_canonical_archive(
            permit.plan.canonical,
            permit.canonical_authority_root,
            executing_release_sha=permit.installed_release_sha,
        )
    )
    if (
        bundle_sha256 != permit.installed_release_bundle_sha256
        or record_sha256 != permit.installed_release_record_sha256
    ):
        raise RecoveryError(
            "installed_release_changed",
            "installed release differs from the permit-bound archive",
        )
    return binding_sha256


def _json_value(raw: Any, label: str) -> Any:
    if not isinstance(raw, str):
        raise RecoveryError(
            "persisted_evidence_invalid", f"{label} is not canonical JSON text"
        )
    try:
        return loads_strict_json_bytes(raw.encode("utf-8"))
    except Exception as exc:
        raise RecoveryError(
            "persisted_evidence_invalid", f"{label} is invalid: {exc}"
        ) from exc


def _migration_history(
    connection: sqlite3.Connection,
) -> tuple[AppliedMigrationBinding, ...]:
    rows = tuple(connection.execute("SELECT * FROM schema_migration ORDER BY version"))
    try:
        history = tuple(
            AppliedMigrationBinding(
                version=int(row["version"]),
                name=str(row["name"]),
                digest_sha256=str(row["digest_sha256"]),
                applied_at=str(row["applied_at"]),
            )
            for row in rows
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError(
            "migration_history_invalid", f"invalid applied migration row: {exc}"
        ) from exc
    if tuple(item.version for item in history) != tuple(range(1, len(history) + 1)):
        raise RecoveryError(
            "migration_history_invalid", "applied migration versions are not contiguous"
        )
    return history


def _history_set_digest(history: Sequence[AppliedMigrationBinding]) -> str:
    return _digest(
        canonical_json_bytes(
            [
                {
                    "version": item.version,
                    "name": item.name,
                    "sha256": item.digest_sha256,
                }
                for item in history
            ]
        )
    )


def _normalized_row(
    table: str,
    row: sqlite3.Row,
    *,
    json_columns: Iterable[str] = (),
    excluded_columns: Iterable[str] = (),
) -> dict[str, Any]:
    json_names = set(json_columns)
    excluded_names = set(excluded_columns)
    return {
        "table": table,
        "values": {
            key: _json_value(row[key], f"{table}.{key}")
            if key in json_names
            else row[key]
            for key in row.keys()
            if key not in excluded_names
        },
    }


def _persisted_provenance_digest(row: sqlite3.Row) -> str:
    payload = {
        "provenance_id": str(row["provenance_id"]),
        "evidence_id": str(row["evidence_id"]),
        "evidence_revision": int(row["evidence_revision"]),
        "origin": _json_value(row["origin_json"], "provenance.origin"),
        "activity": _json_value(row["activity_json"], "provenance.activity"),
        "agent": _json_value(row["agent_json"], "provenance.agent"),
        "tool": _json_value(row["tool_json"], "provenance.tool"),
        "input": _json_value(row["input_json"], "provenance.input"),
        "transformation": _json_value(
            row["transformation_json"], "provenance.transformation"
        ),
        "custody": _json_value(row["custody_json"], "provenance.custody"),
    }
    return _digest(canonical_json_bytes(payload))


def _persisted_independence_digest(row: sqlite3.Row) -> str:
    payload = {
        "disclosure_id": str(row["disclosure_id"]),
        "evidence_id": str(row["evidence_id"]),
        "evidence_revision": int(row["evidence_revision"]),
        "model": _json_value(row["model_json"], "independence.model"),
        "exposure": _json_value(row["exposure_json"], "independence.exposure"),
        "method": _json_value(row["method_json"], "independence.method"),
        "sources": _json_value(row["sources_json"], "independence.sources"),
        "implementation": _json_value(
            row["implementation_json"], "independence.implementation"
        ),
        "environment": _json_value(row["environment_json"], "independence.environment"),
        "correlation": _json_value(row["correlation_json"], "independence.correlation"),
    }
    return _digest(canonical_json_bytes(payload))


def _closure_from_persisted_row(row: sqlite3.Row) -> ClosureManifest:
    members = _json_value(row["members_json"], "closure.members")
    contract = _json_value(row["contract_json"], "closure.contract")
    if not isinstance(members, list) or not isinstance(contract, Mapping):
        raise RecoveryError(
            "persisted_closure_invalid", "persisted closure shape is invalid"
        )
    expected_contract = {
        "closure_id",
        "root",
        "members",
        "omissions",
        "conflicts",
        "canonical_pre_state",
        "canonical_effect",
        "manifest_sha256",
    }
    if set(contract) != expected_contract or contract["members"] != members:
        raise RecoveryError(
            "persisted_closure_invalid", "persisted closure contract is not closed"
        )
    payload = dict(contract)
    try:
        closure = _closure_from_payload(payload)
    except Exception as exc:
        if isinstance(exc, RecoveryError):
            raise
        raise RecoveryError("persisted_closure_invalid", str(exc)) from exc
    if (
        str(row["closure_id"]) != closure.closure_id
        or str(row["root_kind"]) != closure.root.target_kind
        or str(row["root_digest"]) != closure.manifest_sha256
    ):
        raise RecoveryError(
            "persisted_closure_invalid", "closure root kind is inconsistent"
        )
    if _digest(canonical_json_bytes(closure.body_payload())) != closure.manifest_sha256:
        raise RecoveryError(
            "persisted_closure_invalid", "closure digest is inconsistent"
        )
    return closure


def _directive_from_row(row: sqlite3.Row) -> DeletionDirective:
    authorization = _json_value(row["authorization_json"], "deletion.authorization")
    scope = _json_value(row["exact_scope_json"], "deletion.scope")
    if not isinstance(authorization, Mapping) or set(authorization) != {
        "authorization_sha256"
    }:
        raise RecoveryError(
            "persisted_deletion_invalid", "deletion authorization is not closed"
        )
    if not isinstance(scope, Mapping) or set(scope) != {
        "reason",
        "blob_sha256s",
        "evidence_references",
    }:
        raise RecoveryError(
            "persisted_deletion_invalid", "deletion scope is not closed"
        )
    try:
        return DeletionDirective(
            directive_id=str(row["directive_id"]),
            authorization_sha256=str(authorization["authorization_sha256"]),
            reason=str(scope["reason"]),
            blob_sha256s=tuple(str(item) for item in scope["blob_sha256s"]),
            evidence_references=tuple(
                (str(item[0]), int(item[1])) for item in scope["evidence_references"]
            ),
            lifecycle=str(row["lifecycle"]),
        )
    except Exception as exc:
        raise RecoveryError(
            "persisted_deletion_invalid", f"invalid deletion directive: {exc}"
        ) from exc


def _tombstone_from_row(row: sqlite3.Row) -> EvidenceTombstone:
    reason = _json_value(row["reason_json"], "tombstone.reason")
    if not isinstance(reason, Mapping) or set(reason) != {"reason_sha256"}:
        raise RecoveryError(
            "persisted_deletion_invalid", "tombstone reason is not closed"
        )
    try:
        return EvidenceTombstone(
            tombstone_id=str(row["tombstone_id"]),
            evidence_id=str(row["evidence_id"]),
            evidence_revision=int(row["evidence_revision"]),
            directive_id=str(row["directive_id"]),
            reason_sha256=str(reason["reason_sha256"]),
        )
    except Exception as exc:
        raise RecoveryError(
            "persisted_deletion_invalid", f"invalid Evidence tombstone: {exc}"
        ) from exc


def _committed_blob_digests(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """Enumerate every Blob identity committed by the copied SQLite snapshot."""

    digests = tuple(
        str(row[0])
        for row in connection.execute("SELECT sha256 FROM blob ORDER BY sha256")
    )
    if digests != tuple(sorted(set(digests))) or any(
        SHA256_RE.fullmatch(digest) is None for digest in digests
    ):
        raise RecoveryError(
            "persisted_evidence_invalid",
            "committed Blob identities are not exact unique SHA-256 digests",
        )
    return digests


def _load_persisted_backup_evidence(
    database: Path,
    *,
    closure_id: str,
    cas: EvidenceCAS,
    cas_inventory_contract: str,
) -> _PersistedBackupEvidence:
    """Derive one semantic closure plus its contract-selected CAS inventory."""

    if cas_inventory_contract not in {
        _LEGACY_CLOSURE_CAS_INVENTORY,
        _COMMITTED_BLOB_CAS_INVENTORY,
    }:
        raise RecoveryError(
            "backup_manifest_invalid",
            "backup CAS inventory contract is unsupported",
        )

    connection = open_snapshot_connection(database)
    try:
        closure_row = connection.execute(
            "SELECT * FROM closure_manifest WHERE closure_id = ?", (closure_id,)
        ).fetchone()
        if closure_row is None:
            raise RecoveryError(
                "persisted_closure_absent", "selected closure is not persisted"
            )
        closure = _closure_from_persisted_row(closure_row)
        try:
            closure_origin = _validated_persisted_closure_authority_at_write(
                connection,
                closure_id=closure_id,
                _allow_sealed_delete=True,
            )
        except WorkspaceIntegrityError as exc:
            raise RecoveryError("backup_closure_invalid", str(exc)) from exc
        if (
            closure_origin.closure_id != closure.closure_id
            or closure_origin.closure_digest != closure.manifest_sha256
            or closure_origin.canonical_effect != "none"
        ):
            raise RecoveryError(
                "backup_closure_invalid",
                "persisted closure differs from its exact authority-at-write binding",
            )
        member_by_key = {member.key: member for member in closure.members}
        evidence_refs = {
            (member.member_id, int(member.revision))
            for member in closure.members
            if member.kind == "evidence" and member.revision is not None
        }
        normalized_rows: list[dict[str, Any]] = [
            _normalized_row(
                "closure_manifest",
                closure_row,
                json_columns=("members_json", "contract_json"),
            )
        ]

        inventory_digests = (
            tuple(closure.blob_sha256s)
            if cas_inventory_contract == _LEGACY_CLOSURE_CAS_INVENTORY
            else _committed_blob_digests(connection)
        )
        if not set(closure.blob_sha256s).issubset(inventory_digests):
            raise RecoveryError(
                "backup_blob_reference_schema_invalid",
                "selected Evidence closure contains a Blob absent from durable references",
            )
        blobs: dict[str, BlobRecord] = {}
        for digest in inventory_digests:
            row = connection.execute(
                "SELECT * FROM blob WHERE sha256 = ?", (digest,)
            ).fetchone()
            if row is None:
                raise RecoveryError(
                    "persisted_evidence_invalid", f"Blob row is absent: {digest}"
                )
            try:
                record = BlobRecord(
                    sha256=str(row["sha256"]),
                    length=int(row["byte_length"]),
                    media_type=str(row["media_type"]),
                    encoding=(
                        str(row["encoding"]) if row["encoding"] is not None else None
                    ),
                    logical_path=str(row["logical_cas_path"]),
                    integrity_state=str(row["integrity_state"]),
                    availability_state=str(row["availability_state"]),
                    quarantine_state=str(row["quarantine_state"]),
                    quarantine_reason=(
                        str(row["quarantine_reason"])
                        if "quarantine_reason" in row.keys()
                        and row["quarantine_reason"] is not None
                        else None
                    ),
                    first_verified_at=(
                        str(row["first_verified_at"])
                        if row["first_verified_at"] is not None
                        else ""
                    ),
                    last_verified_at=(
                        str(row["last_verified_at"])
                        if row["last_verified_at"] is not None
                        else ""
                    ),
                )
            except Exception as exc:
                raise RecoveryError(
                    "persisted_evidence_invalid", f"invalid Blob row: {exc}"
                ) from exc
            if (
                record.integrity_state != "verified"
                or record.availability_state != "verified_available"
            ):
                raise RecoveryError(
                    "backup_evidence_unavailable",
                    "durably referenced Blob is not verified and available",
                )
            try:
                cas.verify_record(record)
            except EvidenceStoreError as exc:
                raise RecoveryError("backup_cas_invalid", str(exc)) from exc
            blobs[digest] = record
            normalized_rows.append(_normalized_row("blob", row))

        for evidence_id, revision in sorted(evidence_refs):
            row = connection.execute(
                "SELECT * FROM evidence_item_revision WHERE evidence_id = ? AND revision = ?",
                (evidence_id, revision),
            ).fetchone()
            head = connection.execute(
                "SELECT * FROM evidence_item_head WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            member = member_by_key[("evidence", evidence_id, revision)]
            if row is None or str(row["payload_digest"]) != member.payload_sha256:
                raise RecoveryError(
                    "persisted_evidence_invalid", "Evidence revision digest mismatch"
                )
            if (
                head is None
                or int(head["revision"]) != revision
                or str(head["payload_digest"]) != member.payload_sha256
            ):
                raise RecoveryError(
                    "persisted_evidence_invalid", "Evidence head differs from closure"
                )
            if str(row["canonical_effect"]) != "none":
                raise RecoveryError(
                    "persisted_evidence_invalid", "Evidence claims canonical effect"
                )
            if (
                "availability_state" in row.keys()
                and str(row["availability_state"]) != "verified_available"
            ):
                raise RecoveryError(
                    "backup_evidence_unavailable", "Evidence revision is not available"
                )
            normalized_rows.append(
                _normalized_row(
                    "evidence_item_revision",
                    row,
                    json_columns=(
                        "subject_json",
                        "exact_scope_json",
                        "limitations_json",
                        "non_inferences_json",
                        "security_json",
                        "retention_json",
                    ),
                    # The root6-only project-commit pointer authenticates an
                    # exact native row origin. It is deliberately absent for
                    # migrated root5 rows and is not portable Evidence meaning.
                    excluded_columns=("project_commit_no",),
                )
            )
            bindings = tuple(
                connection.execute(
                    "SELECT * FROM evidence_blob WHERE evidence_id = ? AND evidence_revision = ? "
                    "ORDER BY ordinal",
                    (evidence_id, revision),
                )
            )
            if not bindings:
                raise RecoveryError(
                    "persisted_evidence_invalid",
                    "Evidence has no persisted Blob binding",
                )
            bound_digests = {str(item["blob_sha256"]) for item in bindings}
            referenced_digests = {
                reference.target_id
                for reference in member.references
                if reference.relation == "blob"
            }
            if bound_digests != referenced_digests:
                raise RecoveryError(
                    "persisted_evidence_invalid",
                    "Evidence Blob bindings differ from closure",
                )
            normalized_rows.extend(
                _normalized_row("evidence_blob", item) for item in bindings
            )

            provenance_rows = tuple(
                connection.execute(
                    "SELECT * FROM provenance_event WHERE evidence_id = ? AND evidence_revision = ? "
                    "ORDER BY provenance_id",
                    (evidence_id, revision),
                )
            )
            disclosure_rows = tuple(
                connection.execute(
                    "SELECT * FROM independence_disclosure WHERE evidence_id = ? "
                    "AND evidence_revision = ? ORDER BY disclosure_id",
                    (evidence_id, revision),
                )
            )
            expected_provenance = {
                reference.target_id: member_by_key[reference.key].payload_sha256
                for reference in member.references
                if reference.relation == "provenance"
            }
            expected_disclosures = {
                reference.target_id: member_by_key[reference.key].payload_sha256
                for reference in member.references
                if reference.relation == "independence_disclosure"
            }
            actual_provenance = {
                str(item["provenance_id"]): _persisted_provenance_digest(item)
                for item in provenance_rows
            }
            actual_disclosures = {
                str(item["disclosure_id"]): _persisted_independence_digest(item)
                for item in disclosure_rows
            }
            if (
                actual_provenance != expected_provenance
                or actual_disclosures != expected_disclosures
            ):
                raise RecoveryError(
                    "persisted_evidence_invalid",
                    "persisted provenance or independence differs from closure",
                )
            normalized_rows.extend(
                _normalized_row(
                    "provenance_event",
                    item,
                    json_columns=(
                        "origin_json",
                        "activity_json",
                        "agent_json",
                        "tool_json",
                        "input_json",
                        "transformation_json",
                        "custody_json",
                    ),
                )
                for item in provenance_rows
            )
            normalized_rows.extend(
                _normalized_row(
                    "independence_disclosure",
                    item,
                    json_columns=(
                        "model_json",
                        "exposure_json",
                        "method_json",
                        "sources_json",
                        "implementation_json",
                        "environment_json",
                        "correlation_json",
                    ),
                )
                for item in disclosure_rows
            )

        relation_rows = tuple(
            connection.execute("SELECT * FROM evidence_relation ORDER BY relation_id")
        )
        for row in relation_rows:
            source = (str(row["source_evidence_id"]), int(row["source_revision"]))
            target = (str(row["target_evidence_id"]), int(row["target_revision"]))
            if (source in evidence_refs) != (target in evidence_refs):
                raise RecoveryError(
                    "persisted_evidence_invalid",
                    "closure cuts across a persisted Evidence relation",
                )
            if source in evidence_refs:
                normalized_rows.append(
                    _normalized_row(
                        "evidence_relation", row, json_columns=("scope_json",)
                    )
                )

        directive_rows = tuple(
            connection.execute("SELECT * FROM deletion_directive ORDER BY directive_id")
        )
        tombstone_rows = tuple(
            connection.execute("SELECT * FROM evidence_tombstone ORDER BY tombstone_id")
        )
        directives = tuple(_directive_from_row(row) for row in directive_rows)
        tombstones = tuple(_tombstone_from_row(row) for row in tombstone_rows)
        normalized_rows.extend(
            _normalized_row(
                "deletion_directive",
                row,
                json_columns=("authorization_json", "exact_scope_json"),
            )
            for row in directive_rows
        )
        normalized_rows.extend(
            _normalized_row("evidence_tombstone", row, json_columns=("reason_json",))
            for row in tombstone_rows
        )
    finally:
        connection.close()
    try:
        verify_closure_manifest(
            closure,
            blobs={digest: blobs[digest] for digest in closure.blob_sha256s},
            cas=cas,
            deletion_directives=directives,
            tombstones=tombstones,
        )
    except EvidenceStoreError as exc:
        raise RecoveryError("backup_closure_invalid", str(exc)) from exc
    metadata_sha256 = _digest(
        canonical_json_bytes(
            sorted(
                normalized_rows,
                key=lambda item: canonical_json_bytes(item),
            )
        )
    )
    return _PersistedBackupEvidence(
        closure=closure,
        blobs=blobs,
        cas_inventory_contract=cas_inventory_contract,
        deletion_directives=directives,
        tombstones=tombstones,
        metadata_sha256=metadata_sha256,
    )


def _derive_external_reconciliation_v7(
    connection: sqlite3.Connection,
) -> ExternalReconciliation:
    """Reconcile the root-v4 Attempt tables for historical v7 or current v8."""
    intents = tuple(
        connection.execute("SELECT * FROM outbox_intent ORDER BY intent_id")
    )
    attempts = tuple(
        connection.execute("SELECT * FROM attempt_reference ORDER BY attempt_id")
    )
    allocations = tuple(
        connection.execute(
            "SELECT * FROM session_attempt_allocation ORDER BY allocation_id"
        )
    )
    bindings = tuple(
        connection.execute(
            "SELECT * FROM session_attempt_binding "
            "ORDER BY session_id, session_revision, attempt_sequence, attempt_id"
        )
    )
    prepared_receipts = tuple(
        connection.execute("SELECT * FROM prepared_attempt_receipt ORDER BY attempt_id")
    )
    receipts = tuple(
        connection.execute(
            "SELECT * FROM inbox_receipt "
            "ORDER BY attempt_id, observation_sequence, receipt_id"
        )
    )
    dispositions = tuple(
        connection.execute(
            "SELECT * FROM session_attempt_disposition_transition "
            "ORDER BY session_id, session_revision, transition_sequence"
        )
    )
    evidence_bindings = tuple(
        connection.execute("SELECT * FROM attempt_evidence_binding ORDER BY binding_id")
    )
    reservations = tuple(
        connection.execute("SELECT * FROM reservation ORDER BY reservation_id")
    )
    settlements = tuple(
        connection.execute("SELECT * FROM session_settlement ORDER BY settlement_id")
    )

    def invalid(message: str) -> None:
        raise RecoveryError("external_reconciliation_invalid", message)

    intent_by_id = {str(row["intent_id"]): row for row in intents}
    attempt_by_id = {str(row["attempt_id"]): row for row in attempts}
    allocation_by_id = {str(row["allocation_id"]): row for row in allocations}
    binding_by_attempt = {str(row["attempt_id"]): row for row in bindings}
    prepared_by_attempt = {str(row["attempt_id"]): row for row in prepared_receipts}
    receipt_by_id = {str(row["receipt_id"]): row for row in receipts}
    disposition_by_id = {str(row["disposition_id"]): row for row in dispositions}
    if any(
        len(mapping) != len(rows)
        for mapping, rows in (
            (intent_by_id, intents),
            (attempt_by_id, attempts),
            (allocation_by_id, allocations),
            (binding_by_attempt, bindings),
            (prepared_by_attempt, prepared_receipts),
            (receipt_by_id, receipts),
            (disposition_by_id, dispositions),
        )
    ):
        invalid("plural Attempt persistence contains duplicate identities")

    receipts_by_attempt: dict[str, list[sqlite3.Row]] = {}
    late_attempt_ids: set[str] = set()
    observed_unknown_attempt_ids: set[str] = set()
    for receipt in receipts:
        attempt_id = str(receipt["attempt_id"])
        receipts_by_attempt.setdefault(attempt_id, []).append(receipt)
        contract_version = int(receipt["receipt_contract_version"])
        envelope = _json_value(
            receipt["envelope_json"],
            "inbox_receipt.envelope_json",
        )
        if (
            not isinstance(envelope, Mapping)
            or _digest(canonical_json_bytes(envelope))
            != str(receipt["envelope_digest"])
            or str(envelope.get("attempt_id")) != attempt_id
        ):
            invalid("Attempt receipt envelope is not exact or is cross-wired")
        if contract_version == 7:
            attempt = attempt_by_id.get(attempt_id)
            reference_contract_version = (
                int(attempt["reference_contract_version"])
                if attempt is not None
                else None
            )
            expected_result_generation = {
                7: (4, "wc.research_attempt_termination_observation.v1"),
                8: (5, "wc.research_attempt_termination_observation.v2"),
            }.get(reference_contract_version)
            if expected_result_generation is None:
                invalid(
                    "Attempt receipt lacks its exact historical or current identity generation"
                )
            expected_result_schema, expected_observation_contract = (
                expected_result_generation
            )
            lifecycle = envelope.get("lifecycle")
            terminal_lifecycles = {
                "succeeded",
                "failed",
                "cancelled",
                "unknown",
                "fenced",
            }
            nonterminal_lifecycles = {
                "prepared",
                "launch_requested",
                "running",
                "cancel_requested",
            }
            terminal = envelope.get("terminal") is True
            observation = envelope.get("termination_observation")
            observation_sha256 = envelope.get("termination_observation_sha256")
            if (
                envelope.get("schema_version") != expected_result_schema
                or envelope.get("termination_observation_contract")
                != expected_observation_contract
                or (
                    terminal
                    and (
                        lifecycle not in terminal_lifecycles
                        or not isinstance(observation, Mapping)
                        or observation_sha256
                        != receipt["termination_observation_sha256"]
                    )
                )
                or (
                    not terminal
                    and (
                        lifecycle not in nonterminal_lifecycles
                        or observation is not None
                        or observation_sha256 is not None
                        or receipt["termination_observation_sha256"] is not None
                    )
                )
            ):
                invalid(
                    "Attempt receipt lacks its generation-exact observation binding"
                )
            if terminal:
                assert isinstance(observation, Mapping)
                try:
                    from research_attempt_adapter import (
                        canonical_json_bytes as workstation_canonical_json_bytes,
                    )
                except ImportError as exc:  # pragma: no cover - package wiring failure
                    raise RecoveryError(
                        "external_reconciliation_invalid",
                        "Workstation observation digest verifier is unavailable",
                    ) from exc
                if observation_sha256 != _digest(
                    workstation_canonical_json_bytes(
                        {
                            "schema": expected_observation_contract,
                            **dict(observation),
                        }
                    )
                ):
                    invalid(
                        "Attempt receipt observation digest differs from its exact fact"
                    )
            if envelope.get("late") is True:
                late_attempt_ids.add(attempt_id)
            if lifecycle == "unknown":
                if (
                    envelope.get("unknown_effect") is not True
                    or envelope.get("provider_effect") != "unknown"
                    or not isinstance(observation, Mapping)
                    or observation.get("termination_class") != "unknown_effect"
                    or observation.get("provider_effect_certainty") != "unknown"
                ):
                    invalid("UNKNOWN receipt does not retain unknown-effect authority")
                observed_unknown_attempt_ids.add(attempt_id)
        elif contract_version != 1:
            invalid("Attempt receipt contract version is unsupported")

    dispositions_by_session: dict[tuple[str, int], list[sqlite3.Row]] = {}
    latest_disposition_by_attempt: dict[str, sqlite3.Row] = {}
    for disposition in dispositions:
        session_key = (
            str(disposition["session_id"]),
            int(disposition["session_revision"]),
        )
        chain = dispositions_by_session.setdefault(session_key, [])
        expected_sequence = len(chain) + 1
        predecessor = chain[-1] if chain else None
        if (
            int(disposition["transition_sequence"]) != expected_sequence
            or (
                predecessor is None
                and (
                    disposition["predecessor_disposition_id"] is not None
                    or disposition["predecessor_disposition_sha256"] is not None
                )
            )
            or (
                predecessor is not None
                and (
                    str(disposition["predecessor_disposition_id"])
                    != str(predecessor["disposition_id"])
                    or str(disposition["predecessor_disposition_sha256"])
                    != str(predecessor["disposition_sha256"])
                )
            )
            or str(disposition["result_receipt_id"]) not in receipt_by_id
            or str(receipt_by_id[str(disposition["result_receipt_id"])]["attempt_id"])
            != str(disposition["attempt_id"])
        ):
            invalid("Session disposition chain is noncontiguous or cross-wired")
        chain.append(disposition)
        latest_disposition_by_attempt[str(disposition["attempt_id"])] = disposition

    settlement_by_session: dict[tuple[str, int], sqlite3.Row] = {}
    legacy_settled_attempt_ids: set[str] = set()
    settlement_references: dict[str, tuple[str, ...]] = {}
    for settlement in settlements:
        references = _json_value(
            settlement["attempt_references_json"],
            "session_settlement.attempt_references",
        )
        if (
            not isinstance(references, list)
            or any(not isinstance(item, str) or not item for item in references)
            or len(set(references)) != len(references)
        ):
            invalid("settlement Attempt references are not one exact ID chain")
        settlement_id = str(settlement["settlement_id"])
        settlement_references[settlement_id] = tuple(references)
        contract_version = int(settlement["settlement_contract_version"])
        if contract_version == 1:
            legacy_settled_attempt_ids.update(references)
        elif contract_version == 7:
            key = (
                str(settlement["session_id"]),
                int(settlement["session_revision"]),
            )
            if key in settlement_by_session:
                invalid("current Session has more than one final settlement")
            settlement_by_session[key] = settlement
        else:
            invalid("Session settlement contract version is unsupported")

    current_attempts_by_chain: dict[tuple[str, int, str, str], list[sqlite3.Row]] = {}
    for attempt in attempts:
        if int(attempt["reference_contract_version"]) in {7, 8}:
            key = (
                str(attempt["session_id"]),
                int(attempt["session_revision"]),
                str(attempt["execution_intent_id"]),
                str(attempt["attempt_chain_id"]),
            )
            current_attempts_by_chain.setdefault(key, []).append(attempt)
    for chain in current_attempts_by_chain.values():
        chain.sort(key=lambda row: int(row["attempt_sequence"]))
        for index, attempt in enumerate(chain, start=1):
            previous = chain[index - 2] if index > 1 else None
            if (
                int(attempt["attempt_sequence"]) != index
                or (previous is None and attempt["previous_attempt_id"] is not None)
                or (
                    previous is not None
                    and str(attempt["previous_attempt_id"])
                    != str(previous["attempt_id"])
                )
            ):
                invalid("current Attempt identity lineage is noncontiguous")

    unresolved: set[str] = set()
    unknown: set[str] = set(observed_unknown_attempt_ids)
    for attempt_id, binding in binding_by_attempt.items():
        if attempt_id in attempt_by_id:
            continue
        if int(binding["binding_contract_version"]) == 6:
            # Retain the pre-v7 prepared-only recovery interpretation exactly.
            continue
        allocation_id = str(binding["allocation_id"])
        allocation = allocation_by_id.get(allocation_id)
        intent_id = str(binding["intent_id"])
        intent = intent_by_id.get(intent_id)
        prepared = prepared_by_attempt.get(attempt_id)
        if (
            int(binding["binding_contract_version"]) != 8
            or int(binding["workstation_journal_schema_version"]) != 6
            or allocation is None
            or str(allocation["intent_id"]) != intent_id
            or str(allocation["session_id"]) != str(binding["session_id"])
            or int(allocation["session_revision"]) != int(binding["session_revision"])
            or intent is None
            or str(intent["envelope_digest"]) != str(binding["intent_digest"])
            or prepared is None
            or int(prepared["receipt_contract_version"]) != 2
            or str(prepared["binding_id"]) != str(binding["binding_id"])
            or str(prepared["receipt_sha256"])
            != str(binding["prepared_receipt_sha256"])
        ):
            invalid(
                "current PREPARED Attempt lacks its exact allocation/binding/receipt"
            )
        unresolved.add(attempt_id)
    consumed_retry_dispositions = {
        str(row["retry_authorization_disposition_id"])
        for row in bindings
        if row["retry_authorization_disposition_id"] is not None
    }
    legacy_terminal = {"succeeded", "failed", "cancelled", "fenced"}
    for attempt in attempts:
        attempt_id = str(attempt["attempt_id"])
        contract_version = int(attempt["reference_contract_version"])
        projection = (
            attempt["result_digest"],
            attempt["observed_lifecycle"],
            attempt["settlement_state"],
            attempt["unknown_effect"],
        )
        if contract_version in {1, 6}:
            if all(value is None for value in projection):
                if contract_version != 6:
                    invalid("legacy Attempt identity lacks its result projection")
                unresolved.add(attempt_id)
                continue
            if any(value is None for value in projection):
                invalid("legacy Attempt result projection is partial")
            lifecycle = str(attempt["observed_lifecycle"])
            unknown_effect = bool(int(attempt["unknown_effect"]))
            if unknown_effect or lifecycle == "unknown":
                unknown.add(attempt_id)
            if str(attempt["settlement_state"]) == "evidence_only":
                late_attempt_ids.add(attempt_id)
            elif (
                lifecycle not in legacy_terminal
                or attempt_id not in legacy_settled_attempt_ids
            ):
                unresolved.add(attempt_id)
            continue
        if contract_version not in {7, 8} or any(
            value is not None for value in projection
        ):
            invalid("current Attempt identity carries a legacy result projection")
        binding = binding_by_attempt.get(attempt_id)
        prepared = prepared_by_attempt.get(attempt_id)
        allocation_id = str(attempt["allocation_id"])
        expected_journal_version = 5 if contract_version == 7 else 6
        expected_prepared_receipt_version = 1 if contract_version == 7 else 2
        if (
            binding is None
            or int(binding["binding_contract_version"]) != contract_version
            or int(binding["workstation_journal_schema_version"])
            != expected_journal_version
            or int(attempt["journal_schema_version"]) != expected_journal_version
            or str(binding["allocation_id"]) != allocation_id
            or str(binding["intent_id"]) != str(attempt["execution_intent_id"])
            or str(attempt["execution_intent_id"]) not in intent_by_id
            or str(intent_by_id[str(attempt["execution_intent_id"])]["envelope_digest"])
            != str(attempt["intent_sha256"])
            or str(binding["attempt_chain_id"]) != str(attempt["attempt_chain_id"])
            or int(binding["attempt_sequence"]) != int(attempt["attempt_sequence"])
            or binding["previous_attempt_id"] != attempt["previous_attempt_id"]
            or allocation_id not in allocation_by_id
            or prepared is None
            or int(prepared["receipt_contract_version"])
            != expected_prepared_receipt_version
            or str(prepared["binding_id"]) != str(binding["binding_id"])
            or str(prepared["receipt_sha256"])
            != str(attempt["prepared_receipt_sha256"])
        ):
            invalid("current Attempt lacks its exact allocation/prepared identity")
        disposition = latest_disposition_by_attempt.get(attempt_id)
        if disposition is None:
            unresolved.add(attempt_id)
            continue
        action = str(disposition["action"])
        if action == "blocked_unknown":
            unresolved.add(attempt_id)
            unknown.add(attempt_id)
        elif action == "retry_authorized":
            if str(disposition["disposition_id"]) not in consumed_retry_dispositions:
                unresolved.add(attempt_id)
        elif action in {"semantic_pending", "terminal_operational"}:
            session_key = (
                str(attempt["session_id"]),
                int(attempt["session_revision"]),
            )
            settlement = settlement_by_session.get(session_key)
            chain_key = (
                session_key[0],
                session_key[1],
                str(attempt["execution_intent_id"]),
                str(attempt["attempt_chain_id"]),
            )
            chain_ids = tuple(
                str(item["attempt_id"])
                for item in current_attempts_by_chain.get(chain_key, ())
            )
            if (
                settlement is None
                or str(settlement["final_attempt_id"]) != attempt_id
                or str(settlement["disposition_head_id"])
                != str(disposition["disposition_id"])
                or settlement_references[str(settlement["settlement_id"])] != chain_ids
                or not chain_ids
                or chain_ids[-1] != attempt_id
            ):
                unresolved.add(attempt_id)
        else:
            invalid("current Attempt disposition action is unsupported")

    unsettled_reservations = tuple(
        str(row["reservation_id"])
        for row in reservations
        if str(row["lifecycle"]) not in {"settled", "released"}
    )
    persisted_payload = {
        "intents": [
            _normalized_row(
                "outbox_intent",
                row,
                json_columns=("envelope_json",),
            )
            for row in intents
        ],
        "attempts": [_normalized_row("attempt_reference", row) for row in attempts],
        "allocations": [
            _normalized_row(
                "session_attempt_allocation",
                row,
                json_columns=(
                    "attempt_policy_json",
                    "reservation_vector_json",
                    "resource_envelope_json",
                    "resource_enforcement_json",
                ),
            )
            for row in allocations
        ],
        "bindings": [
            _normalized_row("session_attempt_binding", row) for row in bindings
        ],
        "prepared_receipts": [
            _normalized_row(
                "prepared_attempt_receipt",
                row,
                json_columns=("receipt_json",),
            )
            for row in prepared_receipts
        ],
        "receipts": [
            _normalized_row(
                "inbox_receipt",
                row,
                json_columns=("envelope_json",),
            )
            for row in receipts
        ],
        "dispositions": [
            _normalized_row("session_attempt_disposition_transition", row)
            for row in dispositions
        ],
        "evidence_bindings": [
            _normalized_row("attempt_evidence_binding", row)
            for row in evidence_bindings
        ],
        "reservations": [
            _normalized_row("reservation", row, json_columns=("ceiling_json",))
            for row in reservations
        ],
        "settlements": [
            _normalized_row(
                "session_settlement",
                row,
                json_columns=(
                    "attempt_references_json",
                    "charge_basis_json",
                    "release_conversion_json",
                ),
            )
            for row in settlements
        ],
    }
    return ExternalReconciliation(
        unresolved_attempt_ids=tuple(unresolved),
        unknown_effect_attempt_ids=tuple(unknown),
        late_result_ids=tuple(late_attempt_ids),
        unsettled_reservation_ids=unsettled_reservations,
        persisted_state_sha256=_digest(canonical_json_bytes(persisted_payload)),
    )


def _derive_external_reconciliation(
    database: Path,
    *,
    _connection_override: sqlite3.Connection | None = None,
) -> ExternalReconciliation:
    connection = _connection_override or open_snapshot_connection(database)
    owns_connection = _connection_override is None
    try:
        metadata = connection.execute(
            "SELECT schema_version FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None:
            raise RecoveryError(
                "external_reconciliation_invalid",
                "workspace metadata is absent",
            )
        if int(metadata["schema_version"]) in {7, 8, 9, 10, 12}:
            return _derive_external_reconciliation_v7(connection)
        attempts = tuple(
            connection.execute("SELECT * FROM attempt_reference ORDER BY attempt_id")
        )
        reservations = tuple(
            connection.execute("SELECT * FROM reservation ORDER BY reservation_id")
        )
        settlements = tuple(
            connection.execute(
                "SELECT * FROM session_settlement ORDER BY settlement_id"
            )
        )
        settled_attempt_ids: set[str] = set()
        for settlement in settlements:
            references = _json_value(
                settlement["attempt_references_json"],
                "session_settlement.attempt_references",
            )
            if not isinstance(references, list) or any(
                not isinstance(item, str) for item in references
            ):
                raise RecoveryError(
                    "external_reconciliation_invalid",
                    "settlement Attempt references are not a string array",
                )
            settled_attempt_ids.update(references)
        terminal = {"succeeded", "failed", "cancelled"}
        unresolved: list[str] = []
        unknown: list[str] = []
        late: list[str] = []
        for row in attempts:
            attempt_id = str(row["attempt_id"])
            lifecycle = str(row["observed_lifecycle"])
            settlement_state = str(row["settlement_state"])
            unknown_effect = bool(int(row["unknown_effect"]))
            if unknown_effect or lifecycle == "unknown":
                unknown.append(attempt_id)
            if settlement_state == "evidence_only":
                late.append(attempt_id)
            elif lifecycle not in terminal or attempt_id not in settled_attempt_ids:
                unresolved.append(attempt_id)
        unsettled_reservations = [
            str(row["reservation_id"])
            for row in reservations
            if str(row["lifecycle"]) not in {"settled", "released"}
        ]
        persisted_payload = {
            "attempts": [_normalized_row("attempt_reference", row) for row in attempts],
            "reservations": [
                _normalized_row("reservation", row, json_columns=("ceiling_json",))
                for row in reservations
            ],
            "settlements": [
                _normalized_row(
                    "session_settlement",
                    row,
                    json_columns=(
                        "attempt_references_json",
                        "charge_basis_json",
                        "release_conversion_json",
                    ),
                )
                for row in settlements
            ],
        }
    finally:
        if owns_connection:
            connection.close()
    return ExternalReconciliation(
        unresolved_attempt_ids=tuple(unresolved),
        unknown_effect_attempt_ids=tuple(unknown),
        late_result_ids=tuple(late),
        unsettled_reservation_ids=tuple(unsettled_reservations),
        persisted_state_sha256=_digest(canonical_json_bytes(persisted_payload)),
    )


def _store_binding_from_verified_snapshot(
    store: WorkspaceStore,
    connection: sqlite3.Connection,
    integrity: Any,
) -> StoreBinding:
    integrity_payload = {
        "project_id": integrity.project_id,
        "schema_version": integrity.schema_version,
        "current_writer_epoch": integrity.current_writer_epoch,
        "current_project_commit": integrity.current_project_commit,
        "current_root_digest": integrity.current_root_digest,
        "transition_head_digest": integrity.transition_head_digest,
        "revision_count": integrity.revision_count,
        "command_count": integrity.command_count,
        "journal_count": integrity.journal_count,
    }
    return _store_binding_from_snapshot_fields(store, connection, integrity_payload)


def _store_binding_from_snapshot_fields(
    store: WorkspaceStore,
    connection: sqlite3.Connection,
    integrity_payload: Mapping[str, Any],
) -> StoreBinding:
    metadata = connection.execute(
        "SELECT * FROM workspace_metadata WHERE singleton = 1"
    ).fetchone()
    if metadata is None:
        raise RecoveryError("workspace_store_changed", "backup metadata is absent")
    history = _migration_history(connection)
    history_sha256 = _digest(
        canonical_json_bytes([item.to_payload() for item in history])
    )
    history_set_digest = _history_set_digest(history)
    binding = StoreBinding(
        project_id=str(metadata["project_id"]),
        root_identity=str(metadata["root_identity"]),
        application_version=str(metadata["application_version"]),
        schema_version=int(metadata["schema_version"]),
        operating_mode=str(metadata["operating_mode"]),
        root_digest_version=int(metadata["root_digest_version"]),
        migration_digest=history_set_digest,
        migration_history=history,
        migration_history_sha256=history_sha256,
        schema_object_digest=schema_object_digest(connection),
        canonical_authority_digest=str(metadata["canonical_authority_digest"]),
        current_writer_epoch=(
            int(metadata["current_writer_epoch"])
            if metadata["current_writer_epoch"] is not None
            else None
        ),
        writer_epoch_high_watermark=int(
            connection.execute(
                "SELECT COALESCE(MAX(epoch), 0) FROM writer_epoch"
            ).fetchone()[0]
        ),
        writer_lifecycle=str(metadata["lifecycle"]),
        project_commit_id=int(metadata["current_project_commit"]),
        project_root_digest=str(metadata["current_root_digest"]),
        transition_head=(
            str(metadata["transition_head_digest"])
            if metadata["transition_head_digest"] is not None
            else None
        ),
        integrity_report_sha256=_digest(canonical_json_bytes(integrity_payload)),
    )
    if binding.project_id != store.project_id:
        raise RecoveryError("workspace_store_changed", "store project identity changed")
    return binding


def _store_binding(
    store: WorkspaceStore,
    backup_report: WorkspaceBackupReport,
    backup_database: Path,
) -> StoreBinding:
    connection = open_snapshot_connection(backup_database)
    try:
        integrity = store.verify_integrity(
            _connection_override=connection,
            _allow_sealed_delete=True,
        )
        binding = _store_binding_from_verified_snapshot(
            store,
            connection,
            integrity,
        )
    finally:
        connection.close()
    return _require_exported_store_binding(backup_report, binding)


def _store_binding_from_verified_export(
    store: WorkspaceStore,
    backup_report: WorkspaceBackupReport,
    backup_database: Path,
) -> StoreBinding:
    """Project the already audited export, retaining its exact byte binding."""

    return _require_exported_store_binding(
        backup_report,
        _store_binding_from_retained_snapshot(store, backup_database),
    )


def _store_binding_from_retained_snapshot(
    store: WorkspaceStore, backup_database: Path,
) -> StoreBinding:
    """Project metadata/counts only after the caller binds an already audited image."""

    connection = open_snapshot_connection(backup_database)
    try:
        metadata = connection.execute(
            "SELECT * FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None:
            raise RecoveryError("workspace_store_changed", "backup metadata is absent")
        integrity_payload = {
            "project_id": str(metadata["project_id"]),
            "schema_version": int(metadata["schema_version"]),
            "current_writer_epoch": metadata["current_writer_epoch"],
            "current_project_commit": int(metadata["current_project_commit"]),
            "current_root_digest": str(metadata["current_root_digest"]),
            "transition_head_digest": metadata["transition_head_digest"],
            "revision_count": sum(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "mission_revision", "branch_revision", "strategy_revision",
                    "context_revision", "session_revision", "candidate_revision",
                )
            ),
            "command_count": int(connection.execute(
                "SELECT COUNT(*) FROM command_result"
            ).fetchone()[0]),
            "journal_count": int(connection.execute(
                "SELECT COUNT(*) FROM transition_journal"
            ).fetchone()[0]),
        }
        binding = _store_binding_from_snapshot_fields(store, connection, integrity_payload)
    finally:
        connection.close()
    return binding


def _require_exported_store_binding(
    backup_report: WorkspaceBackupReport, binding: StoreBinding,
) -> StoreBinding:
    report_binding = (
        backup_report.project_id,
        backup_report.root_identity,
        backup_report.canonical_authority_digest,
        backup_report.current_writer_epoch,
        backup_report.project_commit,
        backup_report.root_digest,
        backup_report.transition_head_digest,
        backup_report.schema_version,
        backup_report.operating_mode,
        backup_report.root_digest_version,
        backup_report.migration_digest,
        backup_report.schema_object_digest,
        backup_report.writer_epoch_high_watermark,
    )
    snapshot_binding = (
        binding.project_id,
        binding.root_identity,
        binding.canonical_authority_digest,
        binding.current_writer_epoch,
        binding.project_commit_id,
        binding.project_root_digest,
        binding.transition_head,
        binding.schema_version,
        binding.operating_mode,
        binding.root_digest_version,
        binding.migration_digest,
        binding.schema_object_digest,
        binding.writer_epoch_high_watermark,
    )
    if report_binding != snapshot_binding:
        raise RecoveryError(
            "workspace_store_changed",
            "verified backup report and copied WorkspaceStore binding differ",
        )
    return binding


def _deletion_fence_payload(
    directives: Sequence[DeletionDirective],
    tombstones: Sequence[EvidenceTombstone],
) -> dict[str, Any]:
    return {
        "deletion_directives": [
            item.to_payload()
            for item in sorted(directives, key=lambda value: value.directive_id)
        ],
        "tombstones": [
            item.to_payload()
            for item in sorted(tombstones, key=lambda value: value.tombstone_id)
        ],
        "effect": "retrieval_fence_only",
        "automatic_byte_deletion": False,
        "canonical_effect": "none",
    }


def _restorable_inventory(
    inventory: Sequence[BlobRecord],
    directives: Sequence[DeletionDirective],
) -> tuple[BlobRecord, ...]:
    """Project archival closure through the active retrieval/deletion fence."""

    fenced_digests = {
        digest
        for directive in directives
        if directive.lifecycle == "active"
        for digest in directive.blob_sha256s
    }
    return tuple(record for record in inventory if record.sha256 not in fenced_digests)


def _tree_digest(root: Path, relative_files: Iterable[str]) -> str:
    entries: list[dict[str, Any]] = []
    for relative in sorted(relative_files):
        path = _contained(root, root / relative)
        digest, length = _sha256_file(path)
        entries.append({"path": relative, "sha256": digest, "length": length})
    return _digest(canonical_json_bytes(entries))


def _snapshot_tree_digest(
    root: Path,
    inventory: Sequence[BlobRecord],
) -> str:
    """Bind the exact copied database and scoped CAS tree, excluding its manifest."""

    resolved_root = root.resolve(strict=True)
    _assert_plain_tree(resolved_root, code="backup_integrity_authority_invalid")
    relative_files = {
        "workspace.sqlite3",
        *(record.logical_path for record in inventory),
    }
    actual_files = {
        item.relative_to(resolved_root).as_posix()
        for item in resolved_root.rglob("*")
        if item.is_file()
    }
    allowed_files = set(relative_files)
    if (resolved_root / "manifest.json").is_file():
        allowed_files.add("manifest.json")
    if actual_files != allowed_files:
        raise RecoveryError(
            "backup_integrity_authority_invalid",
            "backup snapshot tree contains unbound or missing files",
        )
    return _tree_digest(resolved_root, relative_files)


def _execute_restore_copy(
    *,
    source_root: Path,
    target_root: Path,
    owned_parent: Path,
    sqlite_sha256: str,
    sqlite_length: int,
    inventory: Sequence[BlobRecord],
    directives: Sequence[DeletionDirective],
    tombstones: Sequence[EvidenceTombstone],
    takeover_pending: bool = False,
    publishable_stage: bool = False,
) -> RestoreVerification:
    target_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        _copy_verified(
            source_root / "workspace.sqlite3",
            target_root / "workspace.sqlite3",
            sqlite_sha256,
            sqlite_length,
        )
        relative_files = ["workspace.sqlite3"]
        deleted_blobs = {
            digest
            for directive in directives
            if directive.lifecycle == "active"
            for digest in directive.blob_sha256s
        }
        restorable_inventory = _restorable_inventory(inventory, directives)
        for record in restorable_inventory:
            relative = cas_relative_path(record.sha256)
            _copy_verified(
                source_root / relative,
                _contained(target_root, target_root / relative),
                record.sha256,
                record.length,
                read_only=True,
            )
            relative_files.append(relative)
        fence_raw = canonical_json_bytes(
            _deletion_fence_payload(directives, tombstones)
        )
        _write_exclusive(target_root / "deletion_fence.json", fence_raw, read_only=True)
        relative_files.append("deletion_fence.json")
        # The copy above verifies the exact audited source bytes. Reopening
        # that identical image for another SQLite scan establishes no new fact.
        for digest in deleted_blobs:
            if (target_root / cas_relative_path(digest)).exists():
                raise RecoveryError(
                    "deletion_fence_violation", "deleted blob reappeared during restore"
                )
        restored_root_sha256 = _tree_digest(target_root, relative_files)
        read_only_seal_sha256 = _seal_restored_tree(
            target_root,
            relative_files,
            takeover_pending=takeover_pending,
            deny_delete=not publishable_stage,
        )
        return RestoreVerification(
            restored_root_sha256=restored_root_sha256,
            deletion_fence_sha256=_digest(fence_raw),
            read_only_seal_sha256=read_only_seal_sha256,
            sqlite_integrity="passed",
            evidence_count=len(restorable_inventory),
            verified_at=_now(),
        )
    except Exception:
        _remove_tree(
            target_root,
            owned_parent=owned_parent,
            ignore_errors=True,
        )
        raise


def _restore_test_record(verification: RestoreVerification) -> RestoreTestRecord:
    body = {
        "verification_id": f"restore-test-{uuid.uuid4().hex}",
        "verified_at": verification.verified_at,
        "sqlite_integrity": verification.sqlite_integrity,
        "evidence_count": verification.evidence_count,
        "restored_root_sha256": verification.restored_root_sha256,
        "deletion_fence_sha256": verification.deletion_fence_sha256,
        "read_only_seal_sha256": verification.read_only_seal_sha256,
        "status": "passed",
    }
    return RestoreTestRecord(**body, receipt_sha256=_digest(canonical_json_bytes(body)))


def _publish_bound_backup_stage(
    *,
    stage: Path,
    final: Path,
    backup_root: Path,
    recovery_root: Path,
    restore_test_root: Path,
    backup_id: str,
    store_binding: StoreBinding,
    canonical: CanonicalBinding,
    persisted: _PersistedBackupEvidence,
    sqlite_sha256: str,
    sqlite_length: int,
    fault_hook: Any | None,
    checkpoint_source: CheckpointSourceBinding | None = None,
) -> BackupSet:
    """Restore-test, seal, and publish the sole bound-backup representation."""

    closure = persisted.closure
    inventory = persisted.inventory
    restore_verification = _execute_restore_copy(
        source_root=stage,
        target_root=restore_test_root,
        owned_parent=recovery_root,
        sqlite_sha256=sqlite_sha256,
        sqlite_length=sqlite_length,
        inventory=inventory,
        directives=persisted.deletion_directives,
        tombstones=persisted.tombstones,
    )
    restore_test = _restore_test_record(restore_verification)
    _remove_tree(restore_test_root, owned_parent=recovery_root)
    evidence_root = _digest(
        canonical_json_bytes([item.to_payload() for item in inventory])
    )
    provisional = BackupManifest(
        backup_id=backup_id,
        created_at=_now(),
        store=store_binding,
        canonical=canonical,
        closure=closure,
        cas_inventory_contract=persisted.cas_inventory_contract,
        sqlite_relative_path="workspace.sqlite3",
        sqlite_sha256=sqlite_sha256,
        sqlite_length=sqlite_length,
        evidence_inventory=inventory,
        evidence_root_sha256=evidence_root,
        evidence_metadata_sha256=persisted.metadata_sha256,
        deletion_directives=persisted.deletion_directives,
        tombstones=persisted.tombstones,
        integrity_result="passed",
        restore_test=restore_test,
        complete=True,
        manifest_sha256="0" * 64,
        checkpoint_source=checkpoint_source,
    )
    manifest = BackupManifest(
        **{
            **provisional.__dict__,
            "manifest_sha256": _digest(
                canonical_json_bytes(provisional.body_payload())
            ),
        }
    )
    _write_exclusive(
        stage / "manifest.json", canonical_json_bytes(manifest.to_payload())
    )
    if fault_hook is not None:
        fault_hook(
            "after_manifest_write_before_protection",
            stage,
            manifest,
        )
    _protect_closed_tree(stage, deny_delete=False)
    if fault_hook is not None:
        fault_hook("before_publish", stage, manifest)
    _inspect_pinned_bound_backup(stage, manifest.manifest_sha256)
    if final.exists():
        raise RecoveryError(
            "backup_exists",
            "backup publish target appeared concurrently",
        )
    os.rename(stage, final)
    _fsync_directory(backup_root)
    # Rename retains the protected inode/tree just inspected; publication does
    # not create a different snapshot requiring another semantic audit.
    result = BackupSet(path=final.resolve(), manifest=manifest)
    if fault_hook is not None:
        fault_hook("after_publish", final, manifest)
    return result


@contextmanager
def _acquire_rebackup_lease(
    paths: WorkspacePaths,
    *,
    backup_id: str,
    source_manifest_sha256: str,
    migration_result_sha256: str,
) -> Iterator[HeldKernelLease]:
    lock_path = paths.recovery / f".rebackup-{backup_id}.lock"
    try:
        with acquire_kernel_lease(
            lock_path,
            format_name="research-workspace-rebackup-lock-v2",
            scope={
                "backup_id": backup_id,
                "source_manifest_sha256": source_manifest_sha256,
                "migration_result_sha256": migration_result_sha256,
            },
        ) as lease:
            yield lease
    except KernelLeaseError as exc:
        code = (
            "rebackup_busy"
            if exc.code == "kernel_lease_busy"
            else "rebackup_lock_unsafe"
        )
        raise RecoveryError(code, str(exc)) from exc


def _rebackup_stage_document(
    *,
    backup_id: str,
    source_manifest_sha256: str,
    migration_result_sha256: str,
) -> dict[str, Any]:
    body = {
        "format": "research-workspace-rebackup-stage-v2",
        "backup_id": backup_id,
        "source_manifest_sha256": source_manifest_sha256,
        "migration_result_sha256": migration_result_sha256,
    }
    return {**body, "binding_sha256": _digest(canonical_json_bytes(body))}


def _rebackup_stage_path(
    paths: WorkspacePaths,
    *,
    stage_document: Mapping[str, Any],
    owner_lease_id: str,
) -> Path:
    backup_id = str(stage_document["backup_id"])
    binding_sha256 = str(stage_document["binding_sha256"])
    if not valid_kernel_lease_id(owner_lease_id):
        raise RecoveryError(
            "rebackup_stage_invalid",
            "rebackup stage owner lease identity is invalid",
        )
    candidate = paths.backups / (
        f".stage-{backup_id}-{binding_sha256[:32]}-{owner_lease_id}"
    )
    if _is_reparse_point(candidate):
        raise RecoveryError(
            "rebackup_stage_invalid",
            "rebackup stage identity is a reparse point",
        )
    resolved = _contained(paths.backups, candidate)
    if (
        resolved.parent != paths.backups.resolve(strict=True)
        or resolved.name != candidate.name
    ):
        raise RecoveryError(
            "rebackup_stage_invalid",
            "rebackup stage is not one direct owning child",
        )
    return resolved


def _rebackup_stage_owner_lease_id(lease: HeldKernelLease) -> str:
    """Keep one exact same-scope abandoned owner across repeated crashes."""

    if (
        lease.prior_abandoned
        and lease.predecessor_scope_sha256 == lease.scope_sha256
        and lease.predecessor_lease_id is not None
    ):
        return lease.predecessor_lease_id
    return lease.lease_id


def _existing_rebackup_stage(
    paths: WorkspacePaths,
    *,
    backup_id: str,
    stage_document: Mapping[str, Any],
    inventory: Sequence[BlobRecord],
    lease: HeldKernelLease,
) -> tuple[Path, str | None]:
    try:
        lease.assert_held()
    except KernelLeaseError as exc:
        raise RecoveryError("rebackup_lock_unsafe", str(exc)) from exc
    stage_owner_lease_id = _rebackup_stage_owner_lease_id(lease)
    expected_stage = _rebackup_stage_path(
        paths,
        stage_document=stage_document,
        owner_lease_id=stage_owner_lease_id,
    )
    prefix = f".stage-{backup_id}-"
    candidates = tuple(
        sorted(item for item in paths.backups.iterdir() if item.name.startswith(prefix))
    )
    if not candidates:
        return expected_stage, None
    if len(candidates) != 1:
        raise RecoveryError(
            "rebackup_stage_collision",
            "foreign or multiple rebackup stages share the requested identity",
        )
    candidate = candidates[0]
    if _is_reparse_point(candidate) or not candidate.is_dir():
        raise RecoveryError(
            "rebackup_stage_invalid",
            "rebackup stage candidate is not a plain owned directory",
        )
    _assert_plain_tree(candidate, code="rebackup_stage_invalid")
    operation_prefix = (
        f".stage-{backup_id}-{str(stage_document['binding_sha256'])[:32]}-"
    )
    owner_lease_id = (
        candidate.name[len(operation_prefix) :]
        if candidate.name.startswith(operation_prefix)
        else ""
    )
    if not valid_kernel_lease_id(owner_lease_id):
        raise RecoveryError(
            "rebackup_stage_collision",
            "rebackup stage has no exact lease-bound owner identity",
        )
    if candidate != expected_stage:
        raise RecoveryError(
            "rebackup_stage_collision",
            "rebackup stage belongs to another operation owner",
        )
    if owner_lease_id != lease.lease_id:
        if (
            not lease.prior_abandoned
            or lease.predecessor_lease_id != owner_lease_id
            or lease.predecessor_scope_sha256 != lease.scope_sha256
        ):
            raise RecoveryError(
                "rebackup_stage_collision",
                "rebackup stage is not owned by the exact abandoned predecessor scope",
            )
    if _is_reparse_point(expected_stage) or not expected_stage.is_dir():
        raise RecoveryError(
            "rebackup_stage_invalid",
            "rebackup stage is not a plain owned directory",
        )
    _assert_plain_tree(expected_stage, code="rebackup_stage_invalid")
    manifest = expected_stage / "manifest.json"
    if manifest.exists():
        return expected_stage, "manifested"
    actual_files = {
        item.relative_to(expected_stage).as_posix()
        for item in expected_stage.rglob("*")
        if item.is_file()
    }
    expected_files = {
        "workspace.sqlite3",
        *(record.logical_path for record in inventory),
    }
    if actual_files == expected_files:
        return expected_stage, "copied"
    # Exact current or stable same-scope abandoned ownership authorizes removal
    # and recreation of any unmanifested partial tree, including a hard crash midway
    # through copying.  Foreign and unowned trees were rejected above.
    return expected_stage, "partial"


@dataclass(frozen=True, slots=True)
class _SelectedJournaledClosure:
    closure: ClosureManifest
    project_commit: int


def _select_newest_journaled_closure(
    candidates: Sequence[_SelectedJournaledClosure],
) -> _SelectedJournaledClosure:
    if not candidates:
        raise RecoveryError(
            "qualifying_closure_absent",
            "copied direct-Mission snapshot has no current-head journaled closure",
        )
    ordered = sorted(
        candidates,
        key=lambda item: (-item.project_commit, item.closure.closure_id),
    )
    newest = ordered[0]
    if sum(item.project_commit == newest.project_commit for item in ordered) != 1:
        raise RecoveryError(
            "closure_order_ambiguous",
            "newest qualifying closures share one authoritative project commit",
        )
    return newest


def _closure_root_matches_current_head(
    connection: sqlite3.Connection,
    closure: ClosureManifest,
) -> bool:
    root = closure.root
    if root.target_revision is None:
        return False
    root_members = tuple(
        member
        for member in closure.members
        if member.kind == root.target_kind
        and member.member_id == root.target_id
        and member.revision == root.target_revision
    )
    if len(root_members) != 1:
        return False
    table_binding = {
        "mission": ("mission_head", "object_id"),
        "context": ("context_head", "object_id"),
        "session": ("session_head", "object_id"),
        "candidate": ("candidate_head", "object_id"),
        "evidence": ("evidence_item_head", "evidence_id"),
    }.get(root.target_kind)
    if table_binding is None:
        return False
    table, identity_column = table_binding
    row = connection.execute(
        f"SELECT revision, payload_digest FROM {table} WHERE {identity_column} = ?",
        (root.target_id,),
    ).fetchone()
    return bool(
        row is not None
        and int(row["revision"]) == root.target_revision
        and str(row["payload_digest"]) == root_members[0].payload_sha256
    )


def _newest_journaled_closure_from_snapshot(
    database: Path,
) -> _SelectedJournaledClosure:
    """Select one newest current-head closure from an immutable direct-Mission cut."""

    connection = open_snapshot_connection(database)
    try:
        metadata = connection.execute(
            "SELECT project_id FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None:
            raise RecoveryError("backup_store_binding_invalid", "Store metadata is absent")
        journal_index = _validated_journal_index(
            connection, project_id=str(metadata["project_id"]),
        )
        candidates: list[_SelectedJournaledClosure] = []
        for row in connection.execute(
            "SELECT * FROM closure_manifest ORDER BY closure_id"
        ):
            closure_id = str(row["closure_id"])
            try:
                closure = _closure_from_persisted_row(row)
                origin = _validated_persisted_closure_authority_at_write(
                    connection,
                    closure_id=closure_id,
                    journal_index=journal_index,
                )
            except (EvidenceStoreError, RecoveryError, WorkspaceIntegrityError):
                continue
            if (
                origin.closure_digest == closure.manifest_sha256
                and origin.canonical_effect == "none"
                and _closure_root_matches_current_head(connection, closure)
            ):
                candidates.append(
                    _SelectedJournaledClosure(
                        closure=closure,
                        project_commit=origin.project_commit_no,
                    )
                )
        return _select_newest_journaled_closure(candidates)
    finally:
        connection.close()


def _installed_current_snapshot_facts(
    database: Path,
    *,
    mission_id: str,
    project_id: str,
    require_quiesced_source: bool = False,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    connection = open_snapshot_connection(database)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        metadata = connection.execute(
            "SELECT * FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if (
            metadata is None
            or version != int(metadata["schema_version"])
            or not is_direct_mission_generation(
                int(metadata["schema_version"]), int(metadata["root_digest_version"]),
                str(metadata["operating_mode"]),
            )
            or str(metadata["project_id"]) != project_id
            or (
                require_quiesced_source
                and (
                    str(metadata["lifecycle"]) != RecoveryLifecycle.QUIESCED.value
                    or metadata["current_writer_epoch"] is not None
                )
            )
            or (
                not require_quiesced_source
                and (
                    str(metadata["lifecycle"]) != RecoveryLifecycle.ACTIVE.value
                    or metadata["current_writer_epoch"] is None
                )
            )
        ):
            raise RecoveryError(
                "installed_backup_source_inactive",
                (
                    "installed current backup requires one quiesced checkpoint source"
                    if require_quiesced_source
                    else "installed current backup requires one active direct-Mission Store"
                ),
            )
        mission = connection.execute(
            "SELECT r.payload_json FROM mission_head AS h "
            "JOIN mission_revision AS r ON r.object_id = h.object_id "
            "AND r.revision = h.revision WHERE h.object_id = ?",
            (mission_id,),
        ).fetchone()
        if mission is None:
            raise RecoveryError(
                "installed_backup_mission_absent",
                "requested fixed Mission is absent from the copied snapshot",
            )
        try:
            mission_payload = loads_strict_json_object(str(mission["payload_json"]))
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise RecoveryError(
                "installed_backup_mission_invalid",
                "copied Mission head is not strict JSON",
            ) from exc
        if (
            mission_payload.get("mission_id") != mission_id
            or mission_payload.get("project_id") != project_id
        ):
            raise RecoveryError(
                "installed_backup_mission_invalid",
                "copied Mission head has another Mission or project identity",
            )
        checkpoint = _latest_mission_checkpoint_from_connection(
            connection,
            mission_id,
        )
        if checkpoint is None:
            raise RecoveryError(
                "checkpoint_absent",
                "active Mission snapshot has no completed continuation checkpoint",
            )
        return dict(metadata), checkpoint
    except WorkspaceIntegrityError as exc:
        raise RecoveryError("checkpoint_invalid", str(exc)) from exc
    finally:
        connection.close()


def _require_checkpoint_source_snapshot_binding(
    source: CheckpointSourceBinding,
    *,
    metadata: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> None:
    if (
        source.mission_id == ""
        or source.project_id != str(metadata["project_id"])
        or source.source_root_identity != str(metadata["root_identity"])
        or source.canonical_authority_digest
        != str(metadata["canonical_authority_digest"])
        or source.project_commit != int(metadata["current_project_commit"])
        or source.root_digest != str(metadata["current_root_digest"])
        or source.transition_head_digest
        != (
            str(metadata["transition_head_digest"])
            if metadata["transition_head_digest"] is not None
            else None
        )
        or source.checkpoint_id != str(checkpoint["checkpoint_id"])
        or source.checkpoint_sha256 != str(checkpoint["payload_digest"])
        or source.checkpoint_project_commit != int(checkpoint["project_commit_no"])
    ):
        raise RecoveryError(
            "checkpoint_source_binding_invalid",
            "BackupSet checkpoint source differs from its copied database cut",
        )


def create_bound_backup(
    *,
    store: WorkspaceStore,
    cas: EvidenceCAS,
    backup_id: str,
    authority_repo_root: Path | str,
    closure_id: str,
    fault_hook: Any | None = None,
) -> BackupSet:
    """Create a bound backup solely from persisted Store/CAS authority."""

    if not isinstance(store, WorkspaceStore):
        raise RecoveryError(
            "workspace_store_required", "backup requires the owning WorkspaceStore"
        )
    if cas.root != store.paths.root:
        raise RecoveryError(
            "workspace_root_mismatch", "CAS and WorkspaceStore roots differ"
        )
    if not isinstance(closure_id, str) or not closure_id:
        raise RecoveryError(
            "persisted_closure_absent", "backup requires a persisted closure ID"
        )
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise RecoveryError("invalid_backup_id", "backup ID is not portable")
    store.paths.revalidate_physical(require_root=True, require_database=True)
    root = store.paths.backups
    recovery_root = store.paths.recovery
    store.paths.revalidate_physical(require_root=True, require_database=True)
    final = _contained(root, root / backup_id)
    if final.exists():
        raise RecoveryError("backup_exists", "backup set already exists")
    stage = _contained(root, root / f".stage-{backup_id}-{uuid.uuid4().hex}")
    restore_test_root = _contained(
        recovery_root,
        recovery_root / f".restore-test-{backup_id}-{uuid.uuid4().hex}",
    )
    stage.mkdir(mode=0o700)
    try:
        db_target = stage / "workspace.sqlite3"
        backup_report = store.export_verified_backup(db_target)
        if backup_report.target.resolve() != db_target.resolve():
            raise RecoveryError(
                "workspace_store_changed",
                "WorkspaceStore backup report names another target",
            )
        sqlite_sha256, sqlite_length = _sha256_file(db_target)
        if (sqlite_sha256, sqlite_length) != (
            backup_report.backup_sha256,
            backup_report.byte_length,
        ):
            raise RecoveryError(
                "workspace_store_changed",
                "WorkspaceStore backup report differs from exported bytes",
            )
        if fault_hook is not None:
            fault_hook("after_sqlite_snapshot", stage, backup_report)
        authority_root = Path(authority_repo_root).resolve()
        backup_head = _git_head(authority_root)
        canonical = _canonical_binding_from_database(
            db_target,
            source_commit=backup_head,
        )
        verify_live_canonical_binding(canonical, authority_root)
        persisted = _load_persisted_backup_evidence(
            db_target,
            closure_id=closure_id,
            cas=cas,
            cas_inventory_contract=_COMMITTED_BLOB_CAS_INVENTORY,
        )
        inventory = persisted.inventory
        store_binding = _store_binding_from_verified_export(store, backup_report, db_target)
        for record in inventory:
            source = cas.path_for_digest(record.sha256)
            target = _contained(stage, stage / record.logical_path)
            _copy_verified(source, target, record.sha256, record.length, read_only=True)
        verify_live_canonical_binding(canonical, authority_root)
        return _publish_bound_backup_stage(
            stage=stage,
            final=final,
            backup_root=root,
            recovery_root=recovery_root,
            restore_test_root=restore_test_root,
            backup_id=backup_id,
            store_binding=store_binding,
            canonical=canonical,
            persisted=persisted,
            sqlite_sha256=sqlite_sha256,
            sqlite_length=sqlite_length,
            fault_hook=fault_hook,
        )
    except Exception:
        _remove_tree(stage, owned_parent=root, ignore_errors=True)
        _remove_tree(
            restore_test_root,
            owned_parent=recovery_root,
            ignore_errors=True,
        )
        raise


def create_installed_current_backup(
    *,
    store: WorkspaceStore,
    cas: EvidenceCAS,
    backup_id: str,
    mission_id: str,
    project_id: str,
    installed_release_root: Path | str,
    executing_release_sha: str,
    checkpoint_source: WorkspaceCheckpointSource | None = None,
    require_quiesced_current: bool = False,
    fault_hook: Any | None = None,
) -> tuple[BackupSet, InstalledCurrentBackupReport]:
    """Create one current BackupSet under authenticated installed-release authority."""

    if not isinstance(store, WorkspaceStore):
        raise RecoveryError(
            "workspace_store_required",
            "installed backup requires the owning WorkspaceStore",
        )
    if cas.root != store.paths.root:
        raise RecoveryError(
            "workspace_root_mismatch",
            "CAS and WorkspaceStore roots differ",
        )
    if store.project_id != project_id:
        raise RecoveryError(
            "workspace_project_mismatch",
            "WorkspaceStore belongs to another project",
        )
    if not isinstance(mission_id, str) or not mission_id:
        raise RecoveryError("invalid_mission_id", "Mission identity is required")
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise RecoveryError("invalid_backup_id", "backup ID is not portable")
    if checkpoint_source is not None and (
        type(checkpoint_source) is not WorkspaceCheckpointSource
        or checkpoint_source.state != "claimed"
        or checkpoint_source.claim_id != backup_id
        or checkpoint_source.mission_id != mission_id
        or checkpoint_source.project_id != project_id
    ):
        raise RecoveryError(
            "checkpoint_source_binding_invalid",
            "installed backup requires the exact pending-bound claimed checkpoint source",
        )
    if checkpoint_source is not None and require_quiesced_current:
        raise RecoveryError(
            "checkpoint_source_binding_invalid",
            "claimed checkpoint-source and ordinary quiesced-current modes are exclusive",
        )
    if (
        not isinstance(executing_release_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", executing_release_sha) is None
    ):
        raise RecoveryError(
            "installed_release_invalid",
            "executing installed-release SHA is invalid",
        )

    from .mission_attempt_runtime import (
        FormalAttemptBridgeError,
        installed_release_source_digest,
    )

    release_root = Path(installed_release_root).resolve(strict=True)
    release_record = release_root / ".mathematical-research-release.json"

    def authenticate_release() -> tuple[str, str]:
        try:
            record_sha256_before, record_length_before = _sha256_file(release_record)
            bundle_sha256 = installed_release_source_digest(
                release_root,
                executing_release_sha,
            )
            record_sha256, record_length = _sha256_file(release_record)
        except (FormalAttemptBridgeError, OSError) as exc:
            raise RecoveryError("installed_release_invalid", str(exc)) from exc
        if (record_sha256_before, record_length_before) != (
            record_sha256,
            record_length,
        ):
            raise RecoveryError(
                "installed_release_changed",
                "installed release record changed during authentication",
            )
        return bundle_sha256, record_sha256

    bundle_sha256, record_sha256 = authenticate_release()
    store.paths.revalidate_physical(require_root=True, require_database=True)
    backup_root = store.paths.backups
    recovery_root = store.paths.recovery
    final = _contained(backup_root, backup_root / backup_id)
    if final.exists():
        raise RecoveryError("backup_exists", "backup set already exists")
    stage = _contained(
        backup_root,
        backup_root / f".stage-{backup_id}-{uuid.uuid4().hex}",
    )
    restore_test_root = _contained(
        recovery_root,
        recovery_root / f".restore-test-{backup_id}-{uuid.uuid4().hex}",
    )
    stage.mkdir(mode=0o700)
    try:
        database = stage / "workspace.sqlite3"
        backup_report = (
            store.export_verified_backup(database)
            if checkpoint_source is None
            else store.export_checkpoint_source(checkpoint_source, database)
        )
        if backup_report.target.resolve() != database.resolve():
            raise RecoveryError(
                "workspace_store_changed",
                "WorkspaceStore backup report names another target",
            )
        sqlite_sha256, sqlite_length = _sha256_file(database)
        if (sqlite_sha256, sqlite_length) != (
            backup_report.backup_sha256,
            backup_report.byte_length,
        ):
            raise RecoveryError(
                "workspace_store_changed",
                "WorkspaceStore backup report differs from exported bytes",
            )
        if fault_hook is not None:
            fault_hook("after_sqlite_snapshot", stage, backup_report)

        metadata, checkpoint = _installed_current_snapshot_facts(
            database,
            mission_id=mission_id,
            project_id=project_id,
            require_quiesced_source=(
                checkpoint_source is not None or require_quiesced_current
            ),
        )
        checkpoint_source_binding = (
            CheckpointSourceBinding.from_source(checkpoint_source)
            if checkpoint_source is not None
            else None
        )
        if checkpoint_source_binding is not None:
            _require_checkpoint_source_snapshot_binding(
                checkpoint_source_binding,
                metadata=metadata,
                checkpoint=checkpoint,
            )
        selected = _newest_journaled_closure_from_snapshot(database)
        canonical = _canonical_binding_from_installed_snapshot(database)
        _verify_installed_release_canonical_binding(
            canonical,
            release_root,
            exact_source_commit=canonical.source_commit,
        )
        persisted = _load_persisted_backup_evidence(
            database,
            closure_id=selected.closure.closure_id,
            cas=cas,
            cas_inventory_contract=_COMMITTED_BLOB_CAS_INVENTORY,
        )
        if persisted.closure != selected.closure:
            raise RecoveryError(
                "backup_closure_invalid",
                "selected closure changed within the immutable copied snapshot",
            )
        store_binding = (
            _store_binding_from_verified_export(store, backup_report, database)
            if checkpoint_source is None
            else _store_binding(store, backup_report, database)
        )
        for record in persisted.inventory:
            source = cas.path_for_digest(record.sha256)
            target = _contained(stage, stage / record.logical_path)
            _copy_verified(source, target, record.sha256, record.length, read_only=True)

        final_bundle_sha256, final_record_sha256 = authenticate_release()
        if (final_bundle_sha256, final_record_sha256) != (
            bundle_sha256,
            record_sha256,
        ):
            raise RecoveryError(
                "installed_release_changed",
                "installed release identity changed during backup construction",
            )
        _verify_installed_release_canonical_binding(
            canonical,
            release_root,
            exact_source_commit=canonical.source_commit,
        )
        backup = _publish_bound_backup_stage(
            stage=stage,
            final=final,
            backup_root=backup_root,
            recovery_root=recovery_root,
            restore_test_root=restore_test_root,
            backup_id=backup_id,
            store_binding=store_binding,
            canonical=canonical,
            persisted=persisted,
            sqlite_sha256=sqlite_sha256,
            sqlite_length=sqlite_length,
            fault_hook=fault_hook,
            checkpoint_source=checkpoint_source_binding,
        )
        post_bundle_sha256, post_record_sha256 = authenticate_release()
        if (post_bundle_sha256, post_record_sha256) != (
            bundle_sha256,
            record_sha256,
        ):
            raise RecoveryError(
                "installed_release_changed",
                "installed release identity changed before result sealing",
            )
        _verify_installed_release_canonical_binding(
            canonical,
            release_root,
            exact_source_commit=canonical.source_commit,
        )
        return backup, InstalledCurrentBackupReport(
            schema_version="mathematical_research.installed_current_backup.v1",
            backup_id=backup.manifest.backup_id,
            manifest_sha256=backup.manifest.manifest_sha256,
            mission_id=mission_id,
            project_id=project_id,
            executing_release_sha=executing_release_sha,
            installed_release_record_sha256=record_sha256,
            installed_release_bundle_sha256=bundle_sha256,
            canonical_source_commit=canonical.source_commit,
            database_project_commit=int(metadata["current_project_commit"]),
            database_root_digest=str(metadata["current_root_digest"]),
            database_transition_head=(
                str(metadata["transition_head_digest"])
                if metadata["transition_head_digest"] is not None
                else None
            ),
            checkpoint_id=str(checkpoint["checkpoint_id"]),
            checkpoint_sha256=str(checkpoint["payload_digest"]),
            checkpoint_project_commit=int(checkpoint["project_commit_no"]),
            closure_id=selected.closure.closure_id,
            closure_sha256=selected.closure.manifest_sha256,
            cas_inventory_contract=persisted.cas_inventory_contract,
            cas_object_count=len(persisted.inventory),
            cas_total_bytes=sum(item.length for item in persisted.inventory),
            writer_leases_created=0,
            mission_mutations=0,
            lifecycle_mutations=0,
            host_effects=0,
            provider_effects=0,
        )
    except Exception:
        _remove_tree(stage, owned_parent=backup_root, ignore_errors=True)
        _remove_tree(
            restore_test_root,
            owned_parent=recovery_root,
            ignore_errors=True,
        )
        raise


def inspect_installed_current_backup(
    *, store: WorkspaceStore, backup_id: str, mission_id: str, project_id: str,
    installed_release_root: Path | str, executing_release_sha: str,
    require_quiesced_current: bool = False,
    expected_manifest_sha256: str | None = None,
) -> tuple[BackupSet, InstalledCurrentBackupReport]:
    """Read an existing source-bound cut; never create, overwrite or claim a writer.

    The operational owner must separately prove its journaled pre-effect identity.
    This seam establishes content/source authority, not operational provenance.
    An independently retained completed manifest pin reuses its prior audit;
    without that pin the existing full verification remains necessary.
    """
    from .mission_attempt_runtime import installed_release_source_digest

    if not isinstance(store, WorkspaceStore) or store.project_id != project_id:
        raise RecoveryError("workspace_project_mismatch", "wrong installed backup Store")
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise RecoveryError("invalid_backup_id", "backup ID is not portable")
    store.paths.revalidate_physical(require_root=True, require_database=True)
    release = Path(installed_release_root).resolve(strict=True)
    record = release / ".mathematical-research-release.json"
    record_before = _sha256_file(record)
    bundle = installed_release_source_digest(release, executing_release_sha)
    backup = (
        _inspect_pinned_bound_backup(store.paths.backups / backup_id, expected_manifest_sha256)
        if expected_manifest_sha256 is not None
        else _verify_backup_set_semantic(verify_backup_set(store.paths.backups / backup_id))
    )
    manifest = backup.manifest
    if require_quiesced_current and manifest.checkpoint_source is not None:
        raise RecoveryError(
            "checkpoint_source_binding_invalid",
            "ordinary quiesced-current inspection rejects claimed checkpoint-source custody",
        )
    metadata, checkpoint = _installed_current_snapshot_facts(
        backup.path / manifest.sqlite_relative_path,
        mission_id=mission_id,
        project_id=project_id,
        require_quiesced_source=(
            manifest.checkpoint_source is not None or require_quiesced_current
        ),
    )
    if manifest.checkpoint_source is not None:
        if manifest.checkpoint_source.mission_id != mission_id:
            raise RecoveryError(
                "checkpoint_source_binding_invalid",
                "BackupSet checkpoint source belongs to another Mission",
            )
        _require_checkpoint_source_snapshot_binding(
            manifest.checkpoint_source,
            metadata=metadata,
            checkpoint=checkpoint,
        )
    if manifest.store.root_identity != store.paths.root_identity:
        raise RecoveryError("backup_store_binding_invalid", "backup source root differs")
    _verify_installed_release_canonical_binding(
        manifest.canonical, release, exact_source_commit=manifest.canonical.source_commit,
    )
    if (installed_release_source_digest(release, executing_release_sha) != bundle
            or _sha256_file(record) != record_before):
        raise RecoveryError("installed_release_changed", "installed release changed")
    return backup, InstalledCurrentBackupReport(
        schema_version="mathematical_research.installed_current_backup.v1",
        backup_id=backup_id, manifest_sha256=manifest.manifest_sha256,
        mission_id=mission_id, project_id=project_id,
        executing_release_sha=executing_release_sha,
        installed_release_record_sha256=record_before[0],
        installed_release_bundle_sha256=bundle,
        canonical_source_commit=manifest.canonical.source_commit,
        database_project_commit=int(metadata["current_project_commit"]),
        database_root_digest=str(metadata["current_root_digest"]),
        database_transition_head=metadata["transition_head_digest"],
        checkpoint_id=str(checkpoint["checkpoint_id"]),
        checkpoint_sha256=str(checkpoint["payload_digest"]),
        checkpoint_project_commit=int(checkpoint["project_commit_no"]),
        closure_id=manifest.closure.closure_id,
        closure_sha256=manifest.closure.manifest_sha256,
        cas_inventory_contract=manifest.cas_inventory_contract,
        cas_object_count=len(manifest.evidence_inventory),
        cas_total_bytes=sum(item.length for item in manifest.evidence_inventory),
        writer_leases_created=0, mission_mutations=0, lifecycle_mutations=0,
        host_effects=0, provider_effects=0,
    )


def protect_portable_backup_materialization(
    backup_directory: Path | str, *, owned_parent: Path,
) -> None:
    """Protect one safely extracted disposable direct child before observation.

    This is a materialization effect, deliberately separate from the unchanged
    observation-only portable verifier. Reject links before changing permissions.
    """
    target = Path(os.path.abspath(backup_directory))
    parent = Path(os.path.abspath(owned_parent))
    if (_is_reparse_point(parent) or not parent.is_dir()
            or target.parent != parent):
        raise RecoveryError("backup_path_invalid", "materialization must be an owned direct child")
    _assert_plain_tree(target, code="backup_tree_invalid", require_independent_files=True)
    _protect_closed_tree(target, deny_delete=False, explicit_windows_acl=True)


def dispose_portable_backup_materialization(
    backup_directory: Path | str, *, owned_parent: Path,
) -> None:
    """Relax protection only for disposal of the exact owned materialization."""
    _remove_tree(Path(backup_directory), owned_parent=owned_parent)


def _reconcile_existing_migrated_backup(
    *,
    final: Path,
    backup_id: str,
    source_backup: BackupSet,
    migration_result: object,
    migration_plan: OfflineMigrationPlan,
    migration_lease: HeldKernelLease,
    rebackup_lease: HeldKernelLease,
    authority_repo_root: Path,
    installed_release_sha: str | None,
) -> BackupSet:
    from .migration_executor import OfflineMigrationResult

    if type(migration_result) is not OfflineMigrationResult:
        raise RecoveryError(
            "migration_result_invalid",
            "rebackup reconciliation requires the exact executor result",
        )
    existing = verify_backup_set(final)
    if (
        existing.path.name != backup_id
        or existing.manifest.backup_id != backup_id
        or final.name != backup_id
    ):
        raise RecoveryError(
            "rebackup_collision",
            "existing backup path and manifest do not match the requested identity",
        )
    binding = _verify_migrated_snapshot_semantics(
        source_backup=source_backup,
        migration_result=migration_result,
        migration_plan=migration_plan,
        snapshot_root=existing.path,
        backup_id=backup_id,
        migration_lease=migration_lease,
        rebackup_lease=rebackup_lease,
    )
    _require_migrated_backup_match(
        backup=existing,
        binding=binding,
        source_backup=source_backup,
        migration_result=migration_result,
    )
    _verify_rebackup_canonical_authority(
        source_backup.manifest.canonical,
        authority_repo_root,
        installed_release_sha=installed_release_sha,
    )
    _verify_rebackup_canonical_authority(
        existing.manifest.canonical,
        authority_repo_root,
        installed_release_sha=installed_release_sha,
    )
    fresh = _inspect_pinned_bound_backup(final, existing.manifest.manifest_sha256)
    if fresh != existing:
        raise RecoveryError(
            "rebackup_collision",
            "existing backup changed during lost-reply reconciliation",
        )
    _verify_rebackup_canonical_authority(
        source_backup.manifest.canonical,
        authority_repo_root,
        installed_release_sha=installed_release_sha,
    )
    _verify_rebackup_canonical_authority(
        fresh.manifest.canonical,
        authority_repo_root,
        installed_release_sha=installed_release_sha,
    )
    return fresh


def _verify_rebackup_canonical_authority(
    binding: CanonicalBinding,
    authority_repo_root: Path,
    *,
    installed_release_sha: str | None,
) -> str:
    """Preserve Git authority by default; opt into the installed archive owner."""

    if installed_release_sha is None:
        return verify_live_canonical_binding(binding, authority_repo_root)
    verified, _bundle, _record = _verify_installed_release_canonical_archive(
        binding,
        authority_repo_root,
        executing_release_sha=installed_release_sha,
    )
    return verified


def _require_migrated_backup_match(
    *,
    backup: BackupSet,
    binding: StoreBinding,
    source_backup: BackupSet,
    migration_result: object,
) -> None:
    from .migration_executor import OfflineMigrationResult

    if type(migration_result) is not OfflineMigrationResult:
        raise RecoveryError(
            "migration_result_invalid",
            "migrated backup comparison requires the exact executor result",
        )
    result_sha256, result_length = _sha256_file(migration_result.database)
    source_manifest = source_backup.manifest
    manifest = backup.manifest
    if (
        manifest.cas_inventory_contract != _COMMITTED_BLOB_CAS_INVENTORY
        or manifest.store != binding
        or manifest.store.schema_version != migration_result.target_schema_version
        or manifest.store.project_id != source_manifest.store.project_id
        or manifest.store.root_identity != source_manifest.store.root_identity
        or manifest.store.canonical_authority_digest
        != source_manifest.store.canonical_authority_digest
        or manifest.canonical != source_manifest.canonical
        or manifest.closure != source_manifest.closure
        or manifest.evidence_inventory != source_manifest.evidence_inventory
        or manifest.evidence_metadata_sha256 != source_manifest.evidence_metadata_sha256
        or manifest.deletion_directives != source_manifest.deletion_directives
        or manifest.tombstones != source_manifest.tombstones
        or (manifest.sqlite_sha256, manifest.sqlite_length)
        != (result_sha256, result_length)
    ):
        raise RecoveryError(
            "rebackup_collision",
            "existing backup ID belongs to another migration result or source",
        )


def rebackup_verified_offline_migration(
    *,
    source_backup: BackupSet,
    migration_result: object,
    backup_id: str,
    authority_repo_root: Path | str,
    installed_release_sha: str | None = None,
    fault_hook: Any | None = None,
) -> BackupSet:
    """Publish one migrated image through the existing bound-backup mechanism."""

    from .migration_executor import (
        OfflineMigrationResult,
        _acquire_migration_lease,
        _verify_issued_offline_migration_result,
    )

    if type(source_backup) is not BackupSet:
        raise RecoveryError(
            "verified_backup_required",
            "rebackup requires the exact source BackupSet",
        )
    if type(migration_result) is not OfflineMigrationResult:
        raise RecoveryError(
            "migration_result_invalid",
            "rebackup requires the exact executor result",
        )
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise RecoveryError("invalid_backup_id", "backup ID is not portable")
    migration_result.verify_issued()
    result_digest = _digest(canonical_json_bytes(migration_result.to_payload()))
    verified_source = _inspect_pinned_bound_backup(
        source_backup.path, migration_result.backup_manifest_sha256,
    )
    if verified_source != source_backup:
        raise RecoveryError("stale_backup", "migration source differs from its issued result")
    source_store = verified_source.manifest.store
    migration_pair = (
        source_store.schema_version,
        migration_result.target_schema_version,
    )
    source_generation_matches = (
        migration_pair in {(5, 6), (6, 7)} and source_store.root_digest_version == 3
    ) or (migration_pair == (7, 8) and source_store.root_digest_version == 4) or (
        migration_pair == (9, 10) and source_store.root_digest_version == 5
    ) or (
        migration_pair == (10, 12) and source_store.root_digest_version == 6
    )
    if (
        migration_pair not in {(5, 6), (6, 7), (7, 8), (9, 10), (10, 12)}
        or source_store.operating_mode != "mission_runtime"
        or not source_generation_matches
        or source_store.current_writer_epoch is not None
        or source_store.writer_lifecycle
        not in {RecoveryLifecycle.OFFLINE.value, RecoveryLifecycle.QUIESCED.value}
    ):
        raise RecoveryError(
            "migration_history_unsupported",
            "rebackup supports only exact quiesced Mission/root3 5-to-6 or "
            "6-to-7, Mission/root4 7-to-8, Mission/root5 9-to-10, "
            "or Mission/root6 10-to-12",
        )
    plan = _prepare_offline_migration_from_verified(
        backup=verified_source,
        target_schema_version=migration_result.target_schema_version,
        lifecycle=RecoveryLifecycle(source_store.writer_lifecycle),
    )
    paths = _workspace_paths_for_backup(
        verified_source.path,
        verified_source.manifest.backup_id,
    )
    final_candidate = paths.backups / backup_id
    if _is_reparse_point(final_candidate):
        raise RecoveryError(
            "rebackup_collision",
            "requested backup identity is a reparse point",
        )
    final = _contained(paths.backups, final_candidate)
    if final.parent != paths.backups.resolve(strict=True) or final.name != backup_id:
        raise RecoveryError(
            "rebackup_collision",
            "requested backup identity is not one direct owning child",
        )
    authority_root = Path(authority_repo_root).resolve(strict=True)
    _verify_rebackup_canonical_authority(
        verified_source.manifest.canonical,
        authority_root,
        installed_release_sha=installed_release_sha,
    )
    with _acquire_rebackup_lease(
        paths,
        backup_id=backup_id,
        source_manifest_sha256=verified_source.manifest.manifest_sha256,
        migration_result_sha256=result_digest,
    ) as rebackup_lease:
        with _acquire_migration_lease(
            paths,
            migration_id=migration_result.migration_id,
            actor="workspace_recovery.rebackup",
            fault_hook=None,
        ) as migration_lease:
            verified_source = _inspect_pinned_bound_backup(
                source_backup.path, migration_result.backup_manifest_sha256,
            )
            current_plan = _prepare_offline_migration_from_verified(
                backup=verified_source,
                target_schema_version=migration_result.target_schema_version,
                lifecycle=RecoveryLifecycle(
                    verified_source.manifest.store.writer_lifecycle
                ),
            )
            if current_plan != plan:
                raise RecoveryError(
                    "migration_result_invalid",
                    "migration source or plan changed under the rebackup lease",
                )
            _verify_issued_offline_migration_result(
                migration_result,
                plan=plan,
                backup=verified_source,
            )
            stage_document = _rebackup_stage_document(
                backup_id=backup_id,
                source_manifest_sha256=verified_source.manifest.manifest_sha256,
                migration_result_sha256=result_digest,
            )
            stage, stage_state = _existing_rebackup_stage(
                paths,
                backup_id=backup_id,
                stage_document=stage_document,
                inventory=verified_source.manifest.evidence_inventory,
                lease=rebackup_lease,
            )
            if final.exists() or final.is_symlink():
                if stage_state is not None:
                    raise RecoveryError(
                        "rebackup_stage_collision",
                        "published backup has an unexpected residual stage",
                    )
                return _reconcile_existing_migrated_backup(
                    final=final,
                    backup_id=backup_id,
                    source_backup=verified_source,
                    migration_result=migration_result,
                    migration_plan=plan,
                    migration_lease=migration_lease,
                    rebackup_lease=rebackup_lease,
                    authority_repo_root=authority_root,
                    installed_release_sha=installed_release_sha,
                )
            restore_test_root = _contained(
                paths.recovery,
                paths.recovery / f".restore-test-{backup_id}-{uuid.uuid4().hex}",
            )
            manifested_stage_verified_locally = False
            try:
                if stage_state == "partial":
                    _remove_tree(stage, owned_parent=paths.backups)
                    stage_state = None
                if stage_state == "manifested":
                    try:
                        staged = verify_backup_set(stage)
                    except RecoveryError:
                        _remove_tree(stage, owned_parent=paths.backups)
                        stage_state = None
                    else:
                        staged_binding = _verify_migrated_snapshot_semantics(
                            source_backup=verified_source,
                            migration_result=migration_result,
                            migration_plan=plan,
                            snapshot_root=stage,
                            backup_id=backup_id,
                            migration_lease=migration_lease,
                            rebackup_lease=rebackup_lease,
                        )
                        _require_migrated_backup_match(
                            backup=staged,
                            binding=staged_binding,
                            source_backup=verified_source,
                            migration_result=migration_result,
                        )
                        manifested_stage_verified_locally = True
                        _verify_rebackup_canonical_authority(
                            staged.manifest.canonical,
                            authority_root,
                            installed_release_sha=installed_release_sha,
                        )
                        os.rename(stage, final)
                        _fsync_directory(paths.backups)
                        return _reconcile_existing_migrated_backup(
                            final=final,
                            backup_id=backup_id,
                            source_backup=verified_source,
                            migration_result=migration_result,
                            migration_plan=plan,
                            migration_lease=migration_lease,
                            rebackup_lease=rebackup_lease,
                            authority_repo_root=authority_root,
                            installed_release_sha=installed_release_sha,
                        )
                if stage_state is None:
                    stage.mkdir(mode=0o700)
                    _fsync_directory(paths.backups)
                    if fault_hook is not None:
                        fault_hook(
                            "after_rebackup_stage_mkdir",
                            stage,
                            migration_result,
                        )
                source_database_sha256, source_database_length = _sha256_file(
                    migration_result.database
                )
                if stage_state is None:
                    _copy_verified(
                        migration_result.database,
                        stage / "workspace.sqlite3",
                        source_database_sha256,
                        source_database_length,
                        read_only=True,
                    )
                    source_cas = EvidenceCAS(
                        WorkspacePaths.from_root(verified_source.path)
                    )
                    for record in verified_source.manifest.evidence_inventory:
                        _copy_verified(
                            source_cas.path_for_digest(record.sha256),
                            _contained(stage, stage / record.logical_path),
                            record.sha256,
                            record.length,
                            read_only=True,
                        )
                    if fault_hook is not None:
                        fault_hook("after_rebackup_copy", stage, migration_result)
                    _fsync_directory(stage)
                    if fault_hook is not None:
                        fault_hook("after_rebackup_copy_bound", stage, migration_result)
                store_binding = _verify_migrated_snapshot_semantics(
                    source_backup=verified_source,
                    migration_result=migration_result,
                    migration_plan=plan,
                    snapshot_root=stage,
                    backup_id=backup_id,
                    migration_lease=migration_lease,
                    rebackup_lease=rebackup_lease,
                )
                canonical = _canonical_binding_from_database(
                    stage / "workspace.sqlite3",
                    source_commit=verified_source.manifest.canonical.source_commit,
                )
                if canonical != verified_source.manifest.canonical:
                    raise RecoveryError(
                        "workspace_canonical_binding_invalid",
                        "migrated Store changed its canonical authority",
                    )
                _verify_rebackup_canonical_authority(
                    canonical,
                    authority_root,
                    installed_release_sha=installed_release_sha,
                )
                stage_cas = EvidenceCAS(WorkspacePaths.from_root(stage))
                persisted = _load_persisted_backup_evidence(
                    stage / "workspace.sqlite3",
                    closure_id=verified_source.manifest.closure.closure_id,
                    cas=stage_cas,
                    cas_inventory_contract=_COMMITTED_BLOB_CAS_INVENTORY,
                )
                source_manifest = verified_source.manifest
                if (
                    persisted.closure != source_manifest.closure
                    or persisted.inventory != source_manifest.evidence_inventory
                    or persisted.deletion_directives
                    != source_manifest.deletion_directives
                    or persisted.tombstones != source_manifest.tombstones
                    or persisted.metadata_sha256
                    != source_manifest.evidence_metadata_sha256
                    or store_binding.schema_version
                    != migration_result.target_schema_version
                    or store_binding.project_id != source_store.project_id
                    or store_binding.root_identity != source_store.root_identity
                    or store_binding.canonical_authority_digest
                    != source_store.canonical_authority_digest
                ):
                    raise RecoveryError(
                        "rebackup_source_changed",
                        "migrated snapshot differs from its source authority",
                    )
                _verify_rebackup_canonical_authority(
                    canonical,
                    authority_root,
                    installed_release_sha=installed_release_sha,
                )
                return _publish_bound_backup_stage(
                    stage=stage,
                    final=final,
                    backup_root=paths.backups,
                    recovery_root=paths.recovery,
                    restore_test_root=restore_test_root,
                    backup_id=backup_id,
                    store_binding=store_binding,
                    canonical=canonical,
                    persisted=persisted,
                    sqlite_sha256=source_database_sha256,
                    sqlite_length=source_database_length,
                    fault_hook=fault_hook,
                )
            except Exception:
                if not manifested_stage_verified_locally:
                    _remove_tree(
                        stage,
                        owned_parent=paths.backups,
                        ignore_errors=True,
                    )
                _remove_tree(
                    restore_test_root,
                    owned_parent=paths.recovery,
                    ignore_errors=True,
                )
                raise


def _expect_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise RecoveryError("backup_manifest_invalid", f"{label} fields are not closed")


def _closure_from_payload(payload: Mapping[str, Any]) -> ClosureManifest:
    _expect_keys(
        payload,
        {
            "closure_id",
            "root",
            "members",
            "omissions",
            "conflicts",
            "canonical_pre_state",
            "canonical_effect",
            "manifest_sha256",
        },
        "closure",
    )
    root_payload = payload["root"]
    if not isinstance(root_payload, Mapping):
        raise RecoveryError("backup_manifest_invalid", "closure root must be an object")
    _expect_keys(
        root_payload,
        {"relation", "target_kind", "target_id", "target_revision"},
        "closure root",
    )
    root = ClosureReference(**root_payload)
    member_values = payload["members"]
    if not isinstance(member_values, list):
        raise RecoveryError(
            "backup_manifest_invalid", "closure members must be an array"
        )
    members: list[ClosureMember] = []
    for value in member_values:
        if not isinstance(value, Mapping):
            raise RecoveryError(
                "backup_manifest_invalid", "closure member must be an object"
            )
        _expect_keys(
            value,
            {
                "kind",
                "member_id",
                "revision",
                "payload_sha256",
                "references",
                "available",
            },
            "closure member",
        )
        references_value = value["references"]
        if not isinstance(references_value, list):
            raise RecoveryError(
                "backup_manifest_invalid", "closure references must be an array"
            )
        references: list[ClosureReference] = []
        for reference in references_value:
            if not isinstance(reference, Mapping):
                raise RecoveryError(
                    "backup_manifest_invalid", "closure reference must be an object"
                )
            _expect_keys(
                reference,
                {"relation", "target_kind", "target_id", "target_revision"},
                "closure reference",
            )
            references.append(ClosureReference(**reference))
        members.append(
            ClosureMember(
                kind=value["kind"],
                member_id=value["member_id"],
                revision=value["revision"],
                payload_sha256=value["payload_sha256"],
                references=tuple(references),
                available=value["available"],
            )
        )
    canonical = payload["canonical_pre_state"]
    if not isinstance(canonical, Mapping):
        raise RecoveryError(
            "backup_manifest_invalid", "closure canonical pre-state must be an object"
        )
    return ClosureManifest(
        closure_id=payload["closure_id"],
        root=root,
        members=tuple(members),
        omissions=tuple(payload["omissions"]),
        conflicts=tuple(payload["conflicts"]),
        canonical_pre_state=canonical,
        manifest_sha256=payload["manifest_sha256"],
        canonical_effect=payload["canonical_effect"],
    )


def _blob_from_payload(payload: Mapping[str, Any]) -> BlobRecord:
    _expect_keys(
        payload,
        {
            "sha256",
            "length",
            "media_type",
            "encoding",
            "logical_path",
            "integrity_state",
            "availability_state",
            "quarantine_state",
            "quarantine_reason",
            "first_verified_at",
            "last_verified_at",
            "mathematical_grade",
        },
        "Blob",
    )
    if payload["mathematical_grade"] is not None:
        raise RecoveryError(
            "backup_manifest_invalid", "Blob cannot carry mathematical grade"
        )
    return BlobRecord(
        **{key: value for key, value in payload.items() if key != "mathematical_grade"}
    )


def _manifest_from_payload(payload: Mapping[str, Any]) -> BackupManifest:
    inventory_contract = payload.get(
        "cas_inventory_contract",
        _LEGACY_CLOSURE_CAS_INVENTORY,
    )
    expected_manifest_keys = {
        "backup_id",
        "created_at",
        "store",
        "canonical",
        "closure",
        "sqlite_relative_path",
        "sqlite_sha256",
        "sqlite_length",
        "evidence_inventory",
        "evidence_root_sha256",
        "evidence_metadata_sha256",
        "deletion_directives",
        "tombstones",
        "integrity_result",
        "restore_test",
        "complete",
        "canonical_effect",
        "manifest_sha256",
    }
    if "cas_inventory_contract" in payload:
        expected_manifest_keys.add("cas_inventory_contract")
    if "checkpoint_source" in payload:
        expected_manifest_keys.add("checkpoint_source")
    _expect_keys(
        payload,
        expected_manifest_keys,
        "backup manifest",
    )
    store_payload = payload["store"]
    canonical_payload = payload["canonical"]
    closure_payload = payload["closure"]
    restore_payload = payload["restore_test"]
    for value, label in (
        (store_payload, "store"),
        (canonical_payload, "canonical"),
        (closure_payload, "closure"),
        (restore_payload, "restore test"),
    ):
        if not isinstance(value, Mapping):
            raise RecoveryError("backup_manifest_invalid", f"{label} must be an object")
    _expect_keys(
        store_payload,
        {
            "project_id",
            "root_identity",
            "application_version",
            "schema_version",
            "operating_mode",
            "root_digest_version",
            "migration_digest",
            "migration_history",
            "migration_history_sha256",
            "schema_object_digest",
            "canonical_authority_digest",
            "current_writer_epoch",
            "writer_epoch_high_watermark",
            "writer_lifecycle",
            "project_commit_id",
            "project_root_digest",
            "transition_head",
            "integrity_report_sha256",
        },
        "store binding",
    )
    if not isinstance(canonical_payload["authority_vector"], Mapping):
        raise RecoveryError(
            "backup_manifest_invalid", "canonical authority vector must be an object"
        )
    authority_payload = canonical_payload["authority_vector"]
    if authority_payload.get("binding_version") == 2:
        canonical_binding_keys = {
            "path",
            "sha256",
            "source_commit",
            "authority_vector",
        }
    elif "binding_version" not in authority_payload:
        canonical_binding_keys = {
            "path",
            "sha256",
            "source_commit",
            "validation_contract_sha256",
            "authority_vector",
        }
    else:
        raise RecoveryError(
            "backup_manifest_invalid",
            "unsupported canonical authority binding version",
        )
    _expect_keys(canonical_payload, canonical_binding_keys, "canonical binding")
    _expect_keys(
        restore_payload,
        {
            "verification_id",
            "verified_at",
            "sqlite_integrity",
            "evidence_count",
            "restored_root_sha256",
            "deletion_fence_sha256",
            "read_only_seal_sha256",
            "status",
            "receipt_sha256",
        },
        "restore test",
    )
    migration_history_value = store_payload["migration_history"]
    if not isinstance(migration_history_value, list):
        raise RecoveryError(
            "backup_manifest_invalid", "migration history must be an array"
        )
    migrations: list[AppliedMigrationBinding] = []
    for value in migration_history_value:
        if not isinstance(value, Mapping):
            raise RecoveryError(
                "backup_manifest_invalid", "migration history row must be an object"
            )
        _expect_keys(
            value, {"version", "name", "digest_sha256", "applied_at"}, "migration row"
        )
        migrations.append(AppliedMigrationBinding(**value))
    inventory_value = payload["evidence_inventory"]
    directives_value = payload["deletion_directives"]
    tombstones_value = payload["tombstones"]
    if not all(
        isinstance(value, list)
        for value in (inventory_value, directives_value, tombstones_value)
    ):
        raise RecoveryError(
            "backup_manifest_invalid", "manifest collections must be arrays"
        )
    inventory = tuple(_blob_from_payload(item) for item in inventory_value)
    directives: list[DeletionDirective] = []
    for value in directives_value:
        if not isinstance(value, Mapping):
            raise RecoveryError(
                "backup_manifest_invalid", "deletion directive must be an object"
            )
        _expect_keys(
            value,
            {
                "directive_id",
                "authorization_sha256",
                "reason",
                "blob_sha256s",
                "evidence_references",
                "lifecycle",
            },
            "deletion directive",
        )
        directives.append(
            DeletionDirective(
                directive_id=value["directive_id"],
                authorization_sha256=value["authorization_sha256"],
                reason=value["reason"],
                blob_sha256s=tuple(value["blob_sha256s"]),
                evidence_references=tuple(
                    tuple(item) for item in value["evidence_references"]
                ),
                lifecycle=value["lifecycle"],
            )
        )
    tombstones: list[EvidenceTombstone] = []
    for value in tombstones_value:
        if not isinstance(value, Mapping):
            raise RecoveryError(
                "backup_manifest_invalid", "tombstone must be an object"
            )
        _expect_keys(
            value,
            {
                "tombstone_id",
                "evidence_id",
                "evidence_revision",
                "directive_id",
                "reason_sha256",
            },
            "tombstone",
        )
        tombstones.append(EvidenceTombstone(**value))
    restore_test = RestoreTestRecord(**restore_payload)
    checkpoint_source_payload = payload.get("checkpoint_source")
    checkpoint_source: CheckpointSourceBinding | None = None
    if checkpoint_source_payload is not None:
        if not isinstance(checkpoint_source_payload, Mapping):
            raise RecoveryError(
                "backup_manifest_invalid",
                "checkpoint source binding must be an object",
            )
        _expect_keys(
            checkpoint_source_payload,
            {
                "schema_version",
                "source_incarnation",
                "mission_id",
                "checkpoint_id",
                "checkpoint_sha256",
                "checkpoint_project_commit",
                "project_id",
                "source_root_identity",
                "canonical_authority_digest",
                "prior_writer_epoch",
                "prior_writer_owner",
                "project_commit",
                "root_digest",
                "transition_head_digest",
                "database_sha256",
                "database_length",
            },
            "checkpoint source binding",
        )
        if checkpoint_source_payload["schema_version"] != (
            "mathematical_research.checkpoint_source.v1"
        ):
            raise RecoveryError(
                "backup_manifest_invalid",
                "checkpoint source binding schema is unsupported",
            )
        checkpoint_source = CheckpointSourceBinding(
            **{
                key: value
                for key, value in checkpoint_source_payload.items()
                if key != "schema_version"
            }
        )
    return BackupManifest(
        backup_id=payload["backup_id"],
        created_at=payload["created_at"],
        store=StoreBinding(
            **{
                **store_payload,
                "migration_history": tuple(migrations),
            }
        ),
        canonical=CanonicalBinding(**canonical_payload),
        closure=_closure_from_payload(closure_payload),
        cas_inventory_contract=inventory_contract,
        sqlite_relative_path=payload["sqlite_relative_path"],
        sqlite_sha256=payload["sqlite_sha256"],
        sqlite_length=payload["sqlite_length"],
        evidence_inventory=inventory,
        evidence_root_sha256=payload["evidence_root_sha256"],
        evidence_metadata_sha256=payload["evidence_metadata_sha256"],
        deletion_directives=tuple(directives),
        tombstones=tuple(tombstones),
        integrity_result=payload["integrity_result"],
        restore_test=restore_test,
        complete=payload["complete"],
        manifest_sha256=payload["manifest_sha256"],
        canonical_effect=payload["canonical_effect"],
        checkpoint_source=checkpoint_source,
    )


def _validate_backup_relative_paths(relative_paths: Iterable[str]) -> None:
    """Reject ambiguous path spellings before comparing a closed file set."""

    exact: set[str] = set()
    folded: dict[str, str] = {}
    for raw in relative_paths:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise RecoveryError(
                "backup_path_collision",
                "backup closure contains a non-canonical relative path",
            )
        normalized = Path(raw).as_posix()
        if normalized != raw or any(part in {"", ".", ".."} for part in raw.split("/")):
            raise RecoveryError(
                "backup_path_collision",
                "backup closure contains a non-canonical relative path",
            )
        key = raw.casefold()
        if raw in exact or (key in folded and folded[key] != raw):
            raise RecoveryError(
                "backup_path_collision",
                "backup closure contains duplicate or case-colliding paths",
            )
        exact.add(raw)
        folded[key] = raw


def _verify_backup_tree_contents(
    backup_directory: Path | str,
    *,
    enforce_source_location: bool,
) -> BackupSet:
    """Audit an unpinned BackupSet, including its persisted Store/Evidence binding."""

    root = Path(backup_directory)
    _assert_plain_tree(
        root, code="backup_manifest_invalid",
        require_independent_files=not enforce_source_location,
    )
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RecoveryError("backup_incomplete", "backup manifest is absent")
    try:
        payload = loads_strict_json_bytes(manifest_path.read_bytes())
        manifest = _manifest_from_payload(payload)
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError("backup_manifest_invalid", "backup manifest is invalid") from exc
    backup = _inspect_backup_tree_contents(
        root,
        enforce_source_location=enforce_source_location,
        expected_manifest_sha256=manifest.manifest_sha256,
    )
    db_path = backup.path / manifest.sqlite_relative_path
    _sqlite_integrity(db_path)
    _verify_sqlite_store_binding(db_path, manifest.store, manifest.canonical)
    if manifest.checkpoint_source is not None:
        metadata, checkpoint = _installed_current_snapshot_facts(
            db_path,
            mission_id=manifest.checkpoint_source.mission_id,
            project_id=manifest.checkpoint_source.project_id,
            require_quiesced_source=True,
        )
        _require_checkpoint_source_snapshot_binding(
            manifest.checkpoint_source, metadata=metadata, checkpoint=checkpoint,
        )
    blobs = {item.sha256: item for item in manifest.evidence_inventory}
    backup_cas = EvidenceCAS(WorkspacePaths.from_root(backup.path))
    persisted = _load_persisted_backup_evidence(
        db_path,
        closure_id=manifest.closure.closure_id,
        cas=backup_cas,
        cas_inventory_contract=manifest.cas_inventory_contract,
    )
    if (
        persisted.closure != manifest.closure
        or tuple(persisted.blobs) != tuple(blobs)
        or persisted.deletion_directives != manifest.deletion_directives
        or persisted.tombstones != manifest.tombstones
        or persisted.metadata_sha256 != manifest.evidence_metadata_sha256
    ):
        raise RecoveryError(
            "backup_evidence_metadata_invalid",
            "backup manifest does not match persisted Evidence metadata",
        )
    try:
        verify_closure_manifest(
            manifest.closure,
            blobs={digest: blobs[digest] for digest in manifest.closure.blob_sha256s},
            cas=backup_cas,
            deletion_directives=manifest.deletion_directives,
            tombstones=manifest.tombstones,
        )
    except EvidenceStoreError as exc:
        raise RecoveryError("backup_closure_invalid", str(exc)) from exc
    return backup


def _require_pinned_completed_backup_manifest(
    manifest: BackupManifest, expected_manifest_sha256: str,
) -> None:
    if (
        not isinstance(expected_manifest_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256) is None
    ):
        raise RecoveryError("backup_manifest_invalid", "an exact manifest pin is required")
    if manifest.manifest_sha256 != expected_manifest_sha256:
        raise RecoveryError("stale_backup", "BackupSet differs from the retained manifest pin")
    if (
        not manifest.complete
        or manifest.canonical_effect != "none"
        or manifest.integrity_result != "passed"
    ):
        raise RecoveryError("backup_incomplete", "backup is incomplete or not proof-neutral")
    if _digest(canonical_json_bytes(manifest.body_payload())) != manifest.manifest_sha256:
        raise RecoveryError("backup_manifest_corrupt", "backup manifest digest mismatch")
    restore = manifest.restore_test
    if (
        restore.status != "passed"
        or restore.sqlite_integrity != "passed"
        or _digest(canonical_json_bytes(restore.body_payload())) != restore.receipt_sha256
    ):
        raise RecoveryError("backup_not_restore_tested", "backup lacks its passed restore-test receipt")


def _read_pinned_completed_backup_manifest(
    backup_directory: Path | str,
    *,
    expected_manifest_sha256: str,
) -> BackupManifest:
    """Read only protected source-bound identity for an already accepted backup.

    This is not portable verification and reports no current database/CAS facts.
    Finalizing an exact checkpoint-source claim needs the independently retained
    accepted manifest, not another scan of the exported data it already names.
    """

    provided = Path(backup_directory)
    if _is_reparse_point(provided) or not provided.is_dir():
        raise RecoveryError("backup_manifest_invalid", "backup root is not a plain directory")
    root = provided.resolve(strict=True)
    manifest_path = root / "manifest.json"
    try:
        entry = manifest_path.lstat()
        if (
            _is_reparse_point(manifest_path)
            or not stat.S_ISREG(entry.st_mode)
            or entry.st_nlink != 1
        ):
            raise RecoveryError("backup_manifest_invalid", "backup manifest is not an independent plain file")
        if _write_bit_present((entry.st_mode,)) or (
            os.name != "nt" and _write_bit_present((root.stat().st_mode,))
        ):
            raise RecoveryError("recovery_tree_mutable", "backup manifest custody is writable")
        _verify_windows_paths_write_denied((root, manifest_path), deny_delete=False)
        payload = loads_strict_json_bytes(manifest_path.read_bytes())
        manifest = _manifest_from_payload(payload)
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError("backup_manifest_invalid", "backup manifest is unavailable or invalid") from exc
    _require_pinned_completed_backup_manifest(manifest, expected_manifest_sha256)
    owning_paths = _workspace_paths_for_backup(root, manifest.backup_id)
    if owning_paths.root_identity != manifest.store.root_identity:
        raise RecoveryError("backup_store_binding_invalid", "backup moved to another workspace root")
    return manifest


def _inspect_backup_tree_contents(
    backup_directory: Path | str,
    *,
    enforce_source_location: bool,
    expected_manifest_sha256: str,
) -> BackupSet:
    """Bind physical custody to exact completed-manifest bytes.

    Callers separately establish the pin's provenance: accepted request, current
    creation, or the issued migration result's exact source/target correspondence.
    Reading a candidate's self-hash alone does not establish prior semantics.
    """

    if (
        not isinstance(expected_manifest_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256) is None
    ):
        raise RecoveryError("backup_manifest_invalid", "an exact manifest pin is required")
    provided = Path(backup_directory)
    if not enforce_source_location:
        # Inspect the supplied root before resolution, and the complete tree
        # before opening even the manifest. Resolution hides junction roots;
        # opening a special file can block before later closure validation.
        _assert_plain_tree(
            provided, code="backup_manifest_invalid", require_independent_files=True
        )
    if provided.is_symlink():
        raise RecoveryError("backup_incomplete", "backup root may not be a symlink")
    root = provided.resolve()
    if not root.is_dir():
        raise RecoveryError("backup_incomplete", "backup directory is absent")
    manifest_path = _contained(root, root / "manifest.json")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RecoveryError("backup_incomplete", "backup manifest is absent")
    try:
        payload = loads_strict_json_bytes(manifest_path.read_bytes())
        if not isinstance(payload, Mapping):
            raise TypeError("top-level manifest must be an object")
        manifest = _manifest_from_payload(payload)
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError(
            "backup_manifest_invalid", f"manifest parse failed: {exc}"
        ) from exc
    _require_pinned_completed_backup_manifest(manifest, expected_manifest_sha256)
    if enforce_source_location:
        owning_paths = _workspace_paths_for_backup(root, manifest.backup_id)
        if owning_paths.root_identity != manifest.store.root_identity:
            raise RecoveryError(
                "backup_store_binding_invalid", "backup moved to another workspace root"
            )
    _assert_plain_tree(
        root,
        code="backup_manifest_invalid",
        require_independent_files=not enforce_source_location,
    )
    restore = manifest.restore_test
    if manifest.sqlite_relative_path != "workspace.sqlite3":
        raise RecoveryError("backup_manifest_invalid", "unexpected SQLite path")
    db_path = _contained(root, root / "workspace.sqlite3")
    if db_path.is_symlink() or not db_path.is_file():
        raise RecoveryError("backup_incomplete", "backup SQLite image is absent")
    db_digest, db_length = _sha256_file(db_path)
    if (db_digest, db_length) != (manifest.sqlite_sha256, manifest.sqlite_length):
        raise RecoveryError("backup_sqlite_corrupt", "backup SQLite bytes changed")
    inventory_payload = [item.to_payload() for item in manifest.evidence_inventory]
    if (
        _digest(canonical_json_bytes(inventory_payload))
        != manifest.evidence_root_sha256
    ):
        raise RecoveryError(
            "backup_evidence_inventory_corrupt", "Evidence inventory root mismatch"
        )
    expected_fence_raw = canonical_json_bytes(
        _deletion_fence_payload(manifest.deletion_directives, manifest.tombstones)
    )
    restorable_inventory = _restorable_inventory(
        manifest.evidence_inventory,
        manifest.deletion_directives,
    )
    expected_restore_entries = [
        {
            "path": "workspace.sqlite3",
            "sha256": manifest.sqlite_sha256,
            "length": manifest.sqlite_length,
        },
        {
            "path": "deletion_fence.json",
            "sha256": _digest(expected_fence_raw),
            "length": len(expected_fence_raw),
        },
        *(
            {
                "path": item.logical_path,
                "sha256": item.sha256,
                "length": item.length,
            }
            for item in restorable_inventory
        ),
    ]
    expected_restore_root = _digest(
        canonical_json_bytes(
            sorted(expected_restore_entries, key=lambda item: str(item["path"]))
        )
    )
    expected_read_only_seal = _digest(
        canonical_json_bytes(
            {
                "format": "research-workspace-read-only-v1",
                "lifecycle": RecoveryLifecycle.VERIFIED_READ_ONLY.value,
                "protected_files": sorted(
                    [
                        "workspace.sqlite3",
                        "deletion_fence.json",
                        *(item.logical_path for item in restorable_inventory),
                    ]
                ),
                "protected_root_sha256": expected_restore_root,
                "writable": False,
                "mission_auto_resume": False,
                "canonical_effect": "none",
            }
        )
    )
    if (
        restore.evidence_count != len(restorable_inventory)
        or restore.deletion_fence_sha256 != _digest(expected_fence_raw)
        or restore.restored_root_sha256 != expected_restore_root
        or restore.read_only_seal_sha256 != expected_read_only_seal
    ):
        raise RecoveryError(
            "backup_not_restore_tested",
            "restore-test receipt does not bind the exact backup closure",
        )
    blobs = {item.sha256: item for item in manifest.evidence_inventory}
    if len(blobs) != len(manifest.evidence_inventory):
        raise RecoveryError("backup_manifest_invalid", "duplicate Blob inventory")
    backup_cas = EvidenceCAS(WorkspacePaths.from_root(root))
    expected_files = {manifest_path.resolve(), db_path.resolve()}
    for record in manifest.evidence_inventory:
        blob_path = _contained(root, root / record.logical_path)
        try:
            backup_cas.verify_record(record)
        except EvidenceStoreError as exc:
            raise RecoveryError("backup_cas_invalid", str(exc)) from exc
        if stat.S_IMODE(blob_path.stat().st_mode) & stat.S_IWUSR:
            raise RecoveryError(
                "backup_evidence_mutable", "backup CAS object is owner-writable"
            )
        expected_files.add(blob_path.resolve())
    expected_entries = set(expected_files)
    if not enforce_source_location:
        for file in expected_files:
            parent = file.parent
            while parent != root:
                expected_entries.add(parent)
                parent = parent.parent
    actual_entries: set[Path] = set()
    relative_paths: list[str] = []
    for item in root.rglob("*"):
        if item.is_symlink():
            raise RecoveryError(
                "backup_manifest_invalid", "backup closure contains a symlink"
            )
        if item.is_file() or (not enforce_source_location and item.is_dir()):
            actual_entries.add(item.resolve())
            relative_paths.append(item.relative_to(root).as_posix())
    _validate_backup_relative_paths(relative_paths)
    if actual_entries != expected_entries:
        unexpected = sorted(
            path.relative_to(root).as_posix()
            for path in actual_entries - expected_entries
        )
        missing = sorted(
            path.relative_to(root).as_posix()
            for path in expected_entries - actual_entries
        )
        raise RecoveryError(
            "backup_closure_unexpected",
            f"backup contains unbound or missing files; unexpected={unexpected}, missing={missing}",
        )
    _verify_closed_tree_protection(root, deny_delete=False)
    return BackupSet(path=root, manifest=manifest)


def verify_backup_set(backup_directory: Path | str) -> BackupSet:
    """Verify one BackupSet at its original source-root-bound location."""

    portable_scope = _STAGING_PORTABLE_MIGRATION_SCOPE.get()
    if (
        portable_scope is not None
        and portable_scope._kind(Path(backup_directory)) is not None
    ):
        verified = _verify_backup_tree_contents(
            backup_directory,
            enforce_source_location=False,
        )
        if portable_scope.authorize(verified):
            return verified
    return _verify_backup_tree_contents(
        backup_directory,
        enforce_source_location=True,
    )


def _inspect_pinned_bound_backup(
    backup_directory: Path | str, expected_manifest_sha256: str,
) -> BackupSet:
    portable_scope = _STAGING_PORTABLE_MIGRATION_SCOPE.get()
    portable = portable_scope is not None and portable_scope._kind(Path(backup_directory)) is not None
    backup = _inspect_backup_tree_contents(
        backup_directory,
        enforce_source_location=not portable,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    if portable:
        assert portable_scope is not None
        portable_scope.authorize(backup)
    return backup


def _offline_migration_root_identity(
    backup: BackupSet,
    paths: WorkspacePaths,
) -> str:
    """Select physical custody except for Recovery's exact portable source scope."""

    portable_scope = _STAGING_PORTABLE_MIGRATION_SCOPE.get()
    if portable_scope is None or portable_scope._kind(backup.path) is None:
        return paths.root_identity
    if (
        portable_scope._kind(backup.path) != "source"
        or paths.backups.resolve(strict=True) != portable_scope.backups_root
        or not portable_scope.authorize(backup)
    ):
        raise RecoveryError(
            "portable_migration_scope_invalid",
            "portable migration execution escaped its exact source BackupSet",
        )
    return backup.manifest.store.root_identity


def _issue_verified_backup_integrity_authority(
    backup: BackupSet,
    *,
    portable_materialization: bool = False,
    migration_result: object | None = None,
    migration_plan: OfflineMigrationPlan | None = None,
    snapshot_root: Path | None = None,
    migration_lease: HeldKernelLease | None = None,
    rebackup_lease: HeldKernelLease | None = None,
    rebackup_id: str | None = None,
) -> _VerifiedBackupIntegrityAuthority:
    """Issue one scope only from re-read disk and an exact live result seal."""

    if type(backup) is not BackupSet:
        raise RecoveryError(
            "verified_backup_required",
            "backup integrity authority requires an exact BackupSet",
        )
    if type(portable_materialization) is not bool:
        raise RecoveryError(
            "verified_backup_required",
            "portable materialization selector must be a boolean",
        )
    if portable_materialization and migration_result is not None:
        raise RecoveryError(
            "migration_result_invalid",
            "portable verification cannot issue migration authority",
        )
    portable_scope = _STAGING_PORTABLE_MIGRATION_SCOPE.get()
    scoped_portable = (
        portable_scope is not None and portable_scope.authorize(backup)
    )
    verified = _inspect_backup_tree_contents(
        backup.path,
        enforce_source_location=not (portable_materialization or scoped_portable),
        expected_manifest_sha256=backup.manifest.manifest_sha256,
    )
    if scoped_portable:
        assert portable_scope is not None
        portable_scope.authorize(verified)
    if verified != backup:
        raise RecoveryError(
            "stale_backup",
            "BackupSet object differs from its current disk witness",
        )
    result_sha256: str | None = None
    if migration_result is None:
        if (
            migration_plan is not None
            or snapshot_root is not None
            or migration_lease is not None
            or rebackup_lease is not None
            or rebackup_id is not None
        ):
            raise RecoveryError(
                "migration_result_invalid",
                "migration plan cannot authorize a non-migrated backup snapshot",
            )
        database = verified.path / verified.manifest.sqlite_relative_path
    else:
        from .migration_executor import (
            MigrationExecutionError,
            OfflineMigrationResult,
            _verify_offline_migration_result_copy,
            _verify_issued_offline_migration_result,
        )

        if type(migration_result) is not OfflineMigrationResult:
            raise RecoveryError(
                "migration_result_invalid",
                "backup integrity authority requires the exact executor result",
            )
        if type(migration_plan) is not OfflineMigrationPlan:
            raise RecoveryError(
                "migration_result_invalid",
                "migration result requires its exact Recovery migration plan",
            )
        try:
            _verify_issued_offline_migration_result(
                migration_result,
                plan=migration_plan,
                backup=verified,
            )
        except MigrationExecutionError as exc:
            raise RecoveryError("migration_result_invalid", str(exc)) from exc
        if (
            migration_result.backup_manifest_sha256 != verified.manifest.manifest_sha256
            or migration_result.source_schema_version
            != migration_plan.current_schema_version
            or migration_result.target_schema_version
            != migration_plan.target_schema_version
        ):
            raise RecoveryError(
                "migration_result_invalid",
                "migration result differs from its exact source BackupSet or plan",
            )
        if (
            type(migration_lease) is not HeldKernelLease
            or type(rebackup_lease) is not HeldKernelLease
            or not isinstance(rebackup_id, str)
            or BACKUP_ID_RE.fullmatch(rebackup_id) is None
        ):
            raise RecoveryError(
                "migration_result_invalid",
                "migrated snapshot requires both exact held leases and backup ID",
            )
        try:
            migration_lease.assert_held()
            rebackup_lease.assert_held()
        except KernelLeaseError as exc:
            raise RecoveryError("migration_result_invalid", str(exc)) from exc
        owning_paths = _workspace_paths_for_backup(
            verified.path,
            verified.manifest.backup_id,
        )
        expected_lease_path = (
            owning_paths.recovery
            / f".migration-{migration_result.migration_id}.executor.lock"
        ).resolve(strict=True)
        if migration_lease.path.resolve(strict=True) != expected_lease_path:
            raise RecoveryError(
                "migration_result_invalid",
                "migration result lease belongs to another artifact",
            )
        expected_rebackup_path = (
            owning_paths.recovery / f".rebackup-{rebackup_id}.lock"
        ).resolve(strict=True)
        if rebackup_lease.path.resolve(strict=True) != expected_rebackup_path:
            raise RecoveryError(
                "migration_result_invalid",
                "rebackup lease belongs to another backup identity",
            )
        if snapshot_root is None:
            raise RecoveryError(
                "migration_result_invalid",
                "migrated authority requires one copied snapshot root",
            )
        cas_root = Path(snapshot_root).resolve(strict=True)
        if (
            cas_root.parent != owning_paths.backups.resolve(strict=True)
            or cas_root == verified.path
            or cas_root.name not in {rebackup_id}
            and not cas_root.name.startswith(f".stage-{rebackup_id}-")
        ):
            raise RecoveryError(
                "migration_result_invalid",
                "migrated snapshot root is outside its exact owning backup identity",
            )
        _assert_plain_tree(cas_root, code="migration_result_invalid")
        database = (cas_root / "workspace.sqlite3").resolve(strict=True)
        try:
            _verify_offline_migration_result_copy(
                migration_result,
                plan=migration_plan,
                backup=verified,
                database=database,
            )
            snapshot_cas = EvidenceCAS(WorkspacePaths.from_root(cas_root))
            for record in verified.manifest.evidence_inventory:
                snapshot_cas.verify_record(record)
            closure_blob_digests = set(verified.manifest.closure.blob_sha256s)
            verify_closure_manifest(
                verified.manifest.closure,
                blobs={
                    item.sha256: item
                    for item in verified.manifest.evidence_inventory
                    if item.sha256 in closure_blob_digests
                },
                cas=snapshot_cas,
                deletion_directives=verified.manifest.deletion_directives,
                tombstones=verified.manifest.tombstones,
            )
            _assert_plain_tree(cas_root, code="migration_result_invalid")
        except (MigrationExecutionError, EvidenceStoreError) as exc:
            raise RecoveryError("migration_result_invalid", str(exc)) from exc
        result_sha256 = _digest(canonical_json_bytes(migration_result.to_payload()))

    backup_path = verified.path.resolve(strict=True)
    database = Path(database).resolve(strict=True)
    if migration_result is None:
        cas_root = backup_path
    inventory = tuple(verified.manifest.evidence_inventory)
    database_sha256, database_length = _sha256_file(database)
    snapshot_tree_sha256 = _snapshot_tree_digest(cas_root, inventory)
    migration_lease_path = (
        str(migration_lease.path.resolve(strict=True))
        if migration_lease is not None
        else None
    )
    migration_lease_id = (
        migration_lease.lease_id if migration_lease is not None else None
    )
    rebackup_lease_path = (
        str(rebackup_lease.path.resolve(strict=True))
        if rebackup_lease is not None
        else None
    )
    rebackup_lease_id = rebackup_lease.lease_id if rebackup_lease is not None else None
    seal = _verified_backup_integrity_authority_seal(
        backup_path=backup_path,
        database=database,
        cas_root=cas_root,
        source_root_identity=verified.manifest.store.root_identity,
        portable_materialization=portable_materialization,
        closure=verified.manifest.closure,
        inventory=inventory,
        source_backup_manifest_sha256=verified.manifest.manifest_sha256,
        migration_result_sha256=result_sha256,
        database_sha256=database_sha256,
        database_length=database_length,
        snapshot_tree_sha256=snapshot_tree_sha256,
        migration_lease_path=migration_lease_path,
        migration_lease_id=migration_lease_id,
        rebackup_lease_path=rebackup_lease_path,
        rebackup_lease_id=rebackup_lease_id,
    )
    return _VerifiedBackupIntegrityAuthority(
        backup_path=backup_path,
        database=database,
        cas_root=cas_root,
        source_root_identity=verified.manifest.store.root_identity,
        portable_materialization=portable_materialization,
        closure=verified.manifest.closure,
        inventory=inventory,
        source_backup_manifest_sha256=verified.manifest.manifest_sha256,
        migration_result_sha256=result_sha256,
        database_sha256=database_sha256,
        database_length=database_length,
        snapshot_tree_sha256=snapshot_tree_sha256,
        migration_lease_path=migration_lease_path,
        migration_lease_id=migration_lease_id,
        rebackup_lease_path=rebackup_lease_path,
        rebackup_lease_id=rebackup_lease_id,
        _issuer_token=_RECOVERY_ISSUER_TOKEN,
        _issuer_seal=seal,
    )


def _verify_recovery_snapshot_semantics(
    authority: _VerifiedBackupIntegrityAuthority,
    *,
    migration_lease: HeldKernelLease | None = None,
    rebackup_lease: HeldKernelLease | None = None,
    _source_projection_sink: Callable[[str, Any], None] | None = None,
) -> StoreBinding:
    """Run the one Store-owned semantic verifier over an immutable backup view."""

    if type(authority) is not _VerifiedBackupIntegrityAuthority:
        raise RecoveryError(
            "backup_integrity_authority_unissued",
            "semantic verification requires exact Recovery authority",
        )
    authority.verify_issued()
    if _source_projection_sink is not None and (
        not callable(_source_projection_sink)
        or not authority.portable_materialization
        or authority.migration_result_sha256 is not None
        or migration_lease is not None
        or rebackup_lease is not None
    ):
        raise RecoveryError(
            "source_characterization_scope_invalid",
            "source characterization requires an ordinary portable backup authority",
        )
    database_identity_before = _sha256_file(authority.database)
    tree_identity_before = _snapshot_tree_digest(
        authority.cas_root,
        authority.inventory,
    )
    if (
        database_identity_before
        != (authority.database_sha256, authority.database_length)
        or tree_identity_before != authority.snapshot_tree_sha256
    ):
        raise RecoveryError(
            "backup_integrity_authority_stale",
            "backup snapshot changed after integrity authority issuance",
        )
    portable_scope = _STAGING_PORTABLE_MIGRATION_SCOPE.get()
    scoped_portable = (
        portable_scope is not None
        and portable_scope._kind(authority.backup_path) is not None
    )
    source = _inspect_backup_tree_contents(
        authority.backup_path,
        enforce_source_location=not (
            authority.portable_materialization or scoped_portable
        ),
        expected_manifest_sha256=authority.source_backup_manifest_sha256,
    )
    if _source_projection_sink is not None and (
        source.manifest.store.schema_version != 10
        or source.manifest.store.root_digest_version != 6
        or source.manifest.store.current_writer_epoch is not None
    ):
        raise RecoveryError(
            "source_characterization_scope_invalid",
            "source characterization requires a quiesced schema10/root6 backup",
        )
    if scoped_portable:
        assert portable_scope is not None
        portable_scope.authorize(source)
    if (
        source.manifest.manifest_sha256 != authority.source_backup_manifest_sha256
        or source.manifest.closure != authority.closure
        or source.manifest.evidence_inventory != authority.inventory
        or (
            authority.migration_result_sha256 is None
            and source.path != authority.cas_root
        )
    ):
        raise RecoveryError(
            "stale_backup",
            "semantic verification authority differs from current backup disk",
        )
    if authority.migration_result_sha256 is not None:
        if (
            type(migration_lease) is not HeldKernelLease
            or type(rebackup_lease) is not HeldKernelLease
        ):
            raise RecoveryError(
                "migration_result_invalid",
                "migrated Store verification requires both exact held leases",
            )
        try:
            migration_lease.assert_held()
            rebackup_lease.assert_held()
        except KernelLeaseError as exc:
            raise RecoveryError("migration_result_invalid", str(exc)) from exc
        if (
            str(migration_lease.path.resolve(strict=True))
            != authority.migration_lease_path
            or migration_lease.lease_id != authority.migration_lease_id
            or str(rebackup_lease.path.resolve(strict=True))
            != authority.rebackup_lease_path
            or rebackup_lease.lease_id != authority.rebackup_lease_id
        ):
            raise RecoveryError(
                "migration_result_invalid",
                "migrated Store verification received foreign live leases",
            )
    elif migration_lease is not None or rebackup_lease is not None:
        raise RecoveryError(
            "migration_result_invalid",
            "ordinary BackupSet verification cannot consume migration leases",
        )
    owning_paths = (
        WorkspacePaths.from_root(source.path)
        if authority.portable_materialization
        else _workspace_paths_for_backup(
            source.path,
            source.manifest.backup_id,
        )
    )
    store = WorkspaceStore(
        owning_paths,
        source.manifest.store.project_id,
    )
    scope = _issue_recovery_backup_integrity_scope(
        verified_backup_authority=authority,
    )
    connection = open_snapshot_connection(authority.database)
    try:
        target_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if target_version == 6:
            historical_schema6_authority: object | None = (
                _HISTORICAL_SCHEMA6_IMMUTABLE_SNAPSHOT_AUTHORITY
            )
            historical_schema7_authority: object | None = None
            historical_schema8_authority: object | None = None
        elif target_version == 7:
            historical_schema6_authority = None
            historical_schema7_authority = (
                _HISTORICAL_SCHEMA7_IMMUTABLE_SNAPSHOT_AUTHORITY
            )
            historical_schema8_authority = None
        elif target_version == 8:
            historical_schema6_authority = None
            historical_schema7_authority = None
            historical_schema8_authority = (
                _HISTORICAL_SCHEMA8_IMMUTABLE_SNAPSHOT_AUTHORITY
            )
        elif target_version in {9, 10, 12}:
            historical_schema6_authority = None
            historical_schema7_authority = None
            historical_schema8_authority = None
        else:
            raise RecoveryError(
                "backup_semantic_schema_unsupported",
                "strong backup semantics support only schema6/root3, "
                "historical schema7-8/root4, retained schema9/root5 and "
                "schema10/root6, or current schema12/root6",
            )
        integrity = store.verify_integrity(
            _connection_override=connection,
            _backup_scope_override=scope,
            _historical_schema6_authority=historical_schema6_authority,
            _historical_schema7_authority=historical_schema7_authority,
            _historical_schema8_authority=historical_schema8_authority,
            _allow_sealed_delete=True,
            **({"_source_projection_sink": _source_projection_sink}
               if _source_projection_sink is not None else {}),
        )
        binding = _store_binding_from_verified_snapshot(store, connection, integrity)
        if migration_lease is not None:
            try:
                migration_lease.assert_held()
                assert rebackup_lease is not None
                rebackup_lease.assert_held()
            except KernelLeaseError as exc:
                raise RecoveryError("migration_result_invalid", str(exc)) from exc
        if (
            _sha256_file(authority.database) != database_identity_before
            or _snapshot_tree_digest(authority.cas_root, authority.inventory)
            != tree_identity_before
        ):
            raise RecoveryError(
                "backup_integrity_authority_stale",
                "backup snapshot changed during semantic verification",
            )
        return binding
    except (
        EvidenceStoreError,
        KeyError,
        TypeError,
        ValueError,
        WorkspaceIntegrityError,
    ) as exc:
        raise RecoveryError("backup_semantic_integrity_failed", str(exc)) from exc
    finally:
        connection.close()


def _verify_migrated_snapshot_semantics(
    *,
    source_backup: BackupSet,
    migration_result: object,
    migration_plan: OfflineMigrationPlan,
    snapshot_root: Path,
    backup_id: str,
    migration_lease: HeldKernelLease,
    rebackup_lease: HeldKernelLease,
) -> StoreBinding:
    """Verify one copied result without allowing its authority to escape the leases."""

    try:
        migration_lease.assert_held()
        rebackup_lease.assert_held()
    except KernelLeaseError as exc:
        raise RecoveryError("migration_result_invalid", str(exc)) from exc
    authority = _issue_verified_backup_integrity_authority(
        source_backup,
        migration_result=migration_result,
        migration_plan=migration_plan,
        snapshot_root=snapshot_root,
        migration_lease=migration_lease,
        rebackup_lease=rebackup_lease,
        rebackup_id=backup_id,
    )
    # The executor already audited the changed target. Its issued result and
    # copy authority above bind this database/CAS exactly; rebackup changes no
    # logical rows, so project the same audited image instead of re-auditing it.
    binding = _store_binding_from_retained_snapshot(
        WorkspaceStore(WorkspacePaths.from_root(snapshot_root), source_backup.manifest.store.project_id),
        authority.database,
    )
    try:
        migration_lease.assert_held()
        rebackup_lease.assert_held()
    except KernelLeaseError as exc:
        raise RecoveryError("migration_result_invalid", str(exc)) from exc
    return binding


def _verify_backup_set_semantic(backup: BackupSet) -> BackupSet:
    """Strongly verify one supported BackupSet at a recovery trust boundary."""

    authority = _issue_verified_backup_integrity_authority(backup)
    binding = _verify_recovery_snapshot_semantics(authority)
    verified = _inspect_pinned_bound_backup(backup.path, backup.manifest.manifest_sha256)
    if binding != verified.manifest.store:
        raise RecoveryError(
            "backup_store_binding_invalid",
            "full Store integrity report differs from the BackupSet binding",
        )
    return verified


def _latest_checkpoint_identity_from_snapshot(
    database: Path,
) -> tuple[str, str, int] | None:
    """Return the newest validated checkpoint identity from one immutable cut."""

    connection = open_snapshot_connection(database)
    try:
        row = connection.execute(
            "SELECT mission_id FROM continuation_checkpoint "
            "ORDER BY project_commit_no DESC, checkpoint_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        checkpoint = _latest_mission_checkpoint_from_connection(
            connection,
            str(row["mission_id"]),
        )
        if checkpoint is None:
            raise RecoveryError(
                "checkpoint_absent",
                "checkpoint selection changed within an immutable snapshot",
            )
        return (
            str(checkpoint["checkpoint_id"]),
            str(checkpoint["payload_digest"]),
            int(checkpoint["project_commit_no"]),
        )
    except WorkspaceIntegrityError as exc:
        raise RecoveryError("checkpoint_invalid", str(exc)) from exc
    finally:
        connection.close()


def _portable_backup_verification_error(error: RecoveryError) -> RecoveryError | None:
    """Keep the portable verifier's closed public error translations."""

    if error.code in {
        "backup_evidence_mutable", "recovery_tree_mutable",
        "restore_not_physically_read_only",
    }:
        return RecoveryError(
            "backup_tree_protection_invalid",
            "portable BackupSet is not a complete read-only tree",
        )
    if error.code in {
        "backup_closure_invalid", "backup_evidence_metadata_invalid",
        "checkpoint_invalid", "persisted_deletion_invalid",
    }:
        return RecoveryError(
            "backup_semantic_integrity_failed",
            "portable BackupSet semantic state is invalid",
        )
    return None


def verify_portable_backup_set(
    backup_directory: Path | str,
) -> PortableBackupVerificationReport:
    """Fully verify one read-only BackupSet independently of its materialized root."""

    return _verify_portable_backup_set(backup_directory)


def _verify_portable_backup_set(
    backup_directory: Path | str,
    *,
    _source_projection_sink: Callable[[str, Any], None] | None = None,
) -> PortableBackupVerificationReport:
    """One verification implementation; the fixed probe may consume sources.

    The synchronous sink runs inside the Store's normal authenticated audit.
    Its observations remain provisional until this function's normal physical
    closeout and manifest/Store comparison succeed. It grants no read authority.
    """

    try:
        backup = _verify_backup_tree_contents(
            backup_directory,
            enforce_source_location=False,
        )
        authority = _issue_verified_backup_integrity_authority(
            backup,
            portable_materialization=True,
        )
        binding = _verify_recovery_snapshot_semantics(
            authority,
            **({"_source_projection_sink": _source_projection_sink}
               if _source_projection_sink is not None else {}),
        )
        reread = _revalidate_unchanged_portable_backup(backup)
    except RecoveryError as exc:
        translated = _portable_backup_verification_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    if binding != reread.manifest.store:
        raise RecoveryError(
            "backup_store_binding_invalid",
            "full Store integrity report differs from the portable BackupSet binding",
        )
    return _portable_backup_report_from_verified(reread)


def _portable_backup_report_from_verified(backup: BackupSet) -> PortableBackupVerificationReport:
    """Describe earned semantics plus current exact byte/custody binding; no write authority."""

    checkpoint = _latest_checkpoint_identity_from_snapshot(
        backup.path / backup.manifest.sqlite_relative_path,
    )
    manifest = backup.manifest
    return PortableBackupVerificationReport(
        schema_version="mathematical_research.portable_backup_verification.v1",
        backup_schema_version=manifest.store.schema_version,
        backup_id=manifest.backup_id,
        manifest_sha256=manifest.manifest_sha256,
        database_sha256=manifest.sqlite_sha256,
        database_length=manifest.sqlite_length,
        source_root_identity=manifest.store.root_identity,
        project_id=manifest.store.project_id,
        project_commit=manifest.store.project_commit_id,
        project_root_digest=manifest.store.project_root_digest,
        transition_head=manifest.store.transition_head,
        canonical_path=manifest.canonical.path,
        canonical_sha256=manifest.canonical.sha256,
        canonical_source_commit=manifest.canonical.source_commit,
        closure_id=manifest.closure.closure_id,
        closure_sha256=manifest.closure.manifest_sha256,
        cas_object_count=len(manifest.evidence_inventory),
        cas_total_bytes=sum(item.length for item in manifest.evidence_inventory),
        checkpoint_id=checkpoint[0] if checkpoint is not None else None,
        checkpoint_sha256=checkpoint[1] if checkpoint is not None else None,
        checkpoint_project_commit=(checkpoint[2] if checkpoint is not None else None),
        structural_verification_passed=True,
        semantic_verification_passed=True,
        read_only_tree_verified=True,
        capability_bearing_values_returned=False,
    )


def inspect_portable_backup_set(
    backup_directory: Path | str,
    *,
    expected_manifest_sha256: str,
) -> tuple[BackupSet, PortableBackupVerificationReport]:
    """Inspect current bytes/custody of an independently pinned completed backup.

    The pin must come from the accepted preparation or transfer request, not
    from the candidate manifest being inspected. Its completed manifest and
    restore receipt retain the original semantic result; this operation checks
    that the exact database and CAS bytes remain protected and unchanged. It
    creates no new proof record and grants no write capability. Use the full
    portable verifier when there is no independently retained completed pin.
    """

    backup = _inspect_backup_tree_contents(
        backup_directory,
        enforce_source_location=False,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    return backup, _portable_backup_report_from_verified(backup)


def _portable_backup_report_from_unchanged_verified(
    backup: BackupSet,
) -> PortableBackupVerificationReport:
    """Recheck closeout bytes/protection without repeating earned semantics."""

    try:
        reread = _revalidate_unchanged_portable_backup(backup)
    except RecoveryError as exc:
        translated = _portable_backup_verification_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    return _portable_backup_report_from_verified(reread)


def _revalidate_unchanged_portable_backup(backup: BackupSet) -> BackupSet:
    """Recheck exact bytes/protection against the already accepted manifest."""

    reread = _inspect_backup_tree_contents(
        backup.path,
        enforce_source_location=False,
        expected_manifest_sha256=backup.manifest.manifest_sha256,
    )
    if reread != backup:
        raise RecoveryError("stale_backup", "portable BackupSet changed after semantic verification")
    return reread


def _verify_portable_restore_source(
    backup: BackupSet,
    *,
    verified_source: BackupSet | None,
) -> BackupSet:
    # Only the staging import owner passes its exact accepted source. Public
    # unpinned restore/verification calls retain the full trust transition.
    if verified_source is None:
        return _verify_portable_backup_for_trust_transition(backup)
    if backup != verified_source:
        raise RecoveryError("stale_backup", "portable restore source differs from the verified import source")
    return verified_source


def _verify_backup_for_trust_transition(backup: BackupSet) -> BackupSet:
    """Use strong semantics for direct generations and quiesced bridge snapshots."""

    verified = verify_backup_set(backup.path)
    store = verified.manifest.store
    historical_bridge = (
        store.operating_mode == "mission_runtime"
        and store.current_writer_epoch is None
        and store.writer_lifecycle
        in {RecoveryLifecycle.OFFLINE.value, RecoveryLifecycle.QUIESCED.value}
        and (
            (store.schema_version == 6 and store.root_digest_version == 3)
            or (store.schema_version == 7 and store.root_digest_version == 4)
            or (store.schema_version == 8 and store.root_digest_version == 4)
        )
    )
    if is_direct_mission_generation(
        store.schema_version, store.root_digest_version, store.operating_mode
    ) or historical_bridge:
        return _verify_backup_set_semantic(verified)
    return verified


def _verify_portable_backup_for_trust_transition(backup: BackupSet) -> BackupSet:
    """Reissue trust only from the still-present portable bytes, never source paths."""

    authority = _issue_verified_backup_integrity_authority(
        backup,
        portable_materialization=True,
    )
    binding = _verify_recovery_snapshot_semantics(authority)
    verified = _verify_backup_tree_contents(
        backup.path,
        enforce_source_location=False,
    )
    if binding != verified.manifest.store:
        raise RecoveryError(
            "backup_store_binding_invalid",
            "portable Store integrity differs from the BackupSet binding",
        )
    return verified


def prepare_side_by_side_restore(
    *,
    backup: BackupSet,
    authority_repo_root: Path | str,
    expected_transition_head: str | None = None,
    minimum_project_commit_id: int | None = None,
    observer: WorkspaceObserver = NOOP_WORKSPACE_OBSERVER,
    observation_environment: ObservationEnvironment = ObservationEnvironment.LOCAL,
) -> RestoredWorkspace:
    if not isinstance(backup, BackupSet):
        raise RecoveryError(
            "verified_backup_required", "restore requires a verified BackupSet"
        )
    verified = _verify_backup_for_trust_transition(backup)
    if verified.manifest.manifest_sha256 != backup.manifest.manifest_sha256:
        raise RecoveryError("stale_backup", "BackupSet object is stale")
    verify_live_canonical_binding(verified.manifest.canonical, authority_repo_root)
    if (
        expected_transition_head is not None
        and verified.manifest.store.transition_head != expected_transition_head
    ):
        raise RecoveryError("stale_backup", "backup transition head differs")
    if (
        minimum_project_commit_id is not None
        and verified.manifest.store.project_commit_id < minimum_project_commit_id
    ):
        raise RecoveryError(
            "stale_backup", "backup project commit predates recovery floor"
        )
    owning_paths = _workspace_paths_for_backup(
        verified.path, verified.manifest.backup_id
    )
    if owning_paths.root_identity != verified.manifest.store.root_identity:
        raise RecoveryError(
            "backup_store_binding_invalid", "backup source root identity changed"
        )
    owning_paths.revalidate_physical(require_root=True)
    parent = owning_paths.recovery
    reconciliation = _derive_external_reconciliation(
        verified.path / "workspace.sqlite3"
    )
    restored_root = _contained(
        parent,
        parent / f"restore-{verified.manifest.backup_id}-{uuid.uuid4().hex}",
    )
    verification = _execute_restore_copy(
        source_root=verified.path,
        target_root=restored_root,
        owned_parent=parent,
        sqlite_sha256=verified.manifest.sqlite_sha256,
        sqlite_length=verified.manifest.sqlite_length,
        inventory=verified.manifest.evidence_inventory,
        directives=verified.manifest.deletion_directives,
        tombstones=verified.manifest.tombstones,
    )
    restored = RestoredWorkspace(
        root=restored_root,
        backup=verified,
        lifecycle=RecoveryLifecycle.VERIFIED_READ_ONLY,
        reconciliation=reconciliation,
        verification=verification,
        restore_parent=parent,
    )
    emit_event_safely(
        observer,
        kind=EventKind.RESTORE_CHANGED,
        occurred_at=_now(),
        environment=ObservationEnvironment(observation_environment),
        attributes={
            "project_id": verified.manifest.store.project_id,
            "backup_id": verified.manifest.backup_id,
            "project_commit": verified.manifest.store.project_commit_id,
            "root_digest": verified.manifest.store.project_root_digest,
            "status": "verified_read_only",
        },
    )
    return restored


def prepare_portable_side_by_side_restore(
    *,
    backup_directory: Path | str,
    restore_parent: Path | str,
    authority_repo_root: Path | str,
    expected_transition_head: str | None = None,
    minimum_project_commit_id: int | None = None,
    observer: WorkspaceObserver = NOOP_WORKSPACE_OBSERVER,
    observation_environment: ObservationEnvironment = ObservationEnvironment.LOCAL,
) -> RestoredWorkspace:
    """Import a moved, read-only BackupSet into one explicit disposable parent.

    The public portable report remains non-capability-bearing.  This same-owner
    trust transition re-reads and strongly verifies the materialization, keeps
    its process object internal to Recovery, and never consults the vanished
    original WorkspacePaths.
    """

    source = _verify_backup_tree_contents(
        backup_directory,
        enforce_source_location=False,
    )
    verified = _verify_portable_backup_for_trust_transition(source)
    verify_live_canonical_binding(verified.manifest.canonical, authority_repo_root)
    if (
        expected_transition_head is not None
        and verified.manifest.store.transition_head != expected_transition_head
    ):
        raise RecoveryError("stale_backup", "backup transition head differs")
    if (
        minimum_project_commit_id is not None
        and verified.manifest.store.project_commit_id < minimum_project_commit_id
    ):
        raise RecoveryError(
            "stale_backup", "backup project commit predates recovery floor"
        )
    parent_input = Path(restore_parent).expanduser()
    if not parent_input.is_absolute():
        raise RecoveryError(
            "restore_path_invalid", "portable restore parent must be absolute"
        )
    if _is_reparse_point(parent_input):
        raise RecoveryError(
            "restore_path_invalid", "portable restore parent cannot be a link"
        )
    try:
        parent = parent_input.resolve(strict=True)
    except OSError as exc:
        raise RecoveryError(
            "restore_path_invalid", "portable restore parent is unavailable"
        ) from exc
    if not parent.is_dir():
        raise RecoveryError(
            "restore_path_invalid", "portable restore parent must be a directory"
        )
    reconciliation = _derive_external_reconciliation(
        verified.path / "workspace.sqlite3"
    )
    restored_root = _contained(
        parent,
        parent / f"restore-{verified.manifest.backup_id}-{uuid.uuid4().hex}",
    )
    verification = _execute_restore_copy(
        source_root=verified.path,
        target_root=restored_root,
        owned_parent=parent,
        sqlite_sha256=verified.manifest.sqlite_sha256,
        sqlite_length=verified.manifest.sqlite_length,
        inventory=verified.manifest.evidence_inventory,
        directives=verified.manifest.deletion_directives,
        tombstones=verified.manifest.tombstones,
    )
    restored = RestoredWorkspace(
        root=restored_root,
        backup=verified,
        lifecycle=RecoveryLifecycle.VERIFIED_READ_ONLY,
        reconciliation=reconciliation,
        verification=verification,
        source_materialization_portable=True,
        restore_parent=parent,
    )
    emit_event_safely(
        observer,
        kind=EventKind.RESTORE_CHANGED,
        occurred_at=_now(),
        environment=ObservationEnvironment(observation_environment),
        attributes={
            "project_id": verified.manifest.store.project_id,
            "backup_id": verified.manifest.backup_id,
            "project_commit": verified.manifest.store.project_commit_id,
            "root_digest": verified.manifest.store.project_root_digest,
            "status": "verified_read_only",
        },
    )
    verify_restored_workspace(restored)
    return restored


def _expected_restored_materialization(
    manifest: BackupManifest,
) -> tuple[tuple[str, ...], str, str, int]:
    restorable = _restorable_inventory(
        manifest.evidence_inventory,
        manifest.deletion_directives,
    )
    fence_raw = canonical_json_bytes(
        _deletion_fence_payload(manifest.deletion_directives, manifest.tombstones)
    )
    entries = [
        {
            "path": "workspace.sqlite3",
            "sha256": manifest.sqlite_sha256,
            "length": manifest.sqlite_length,
        },
        {
            "path": "deletion_fence.json",
            "sha256": _digest(fence_raw),
            "length": len(fence_raw),
        },
        *(
            {
                "path": item.logical_path,
                "sha256": item.sha256,
                "length": item.length,
            }
            for item in restorable
        ),
    ]
    protected = tuple(sorted(str(item["path"]) for item in entries))
    return (
        protected,
        _digest(canonical_json_bytes(sorted(entries, key=lambda item: item["path"]))),
        _digest(fence_raw),
        len(restorable),
    )


def _fixed_target_restore_artifacts(target_root: Path | str) -> tuple[Path, Path]:
    """Return the one fixed sibling stage and kernel-backed operation state."""

    target = Path(target_root)
    token = _digest(
        canonical_json_bytes(
            {
                "format": "research-portable-fixed-target-restore-name-v1",
                "target_root": os.path.normcase(str(target)),
            }
        )
    )[:32]
    return (
        target.parent / f".portable-fixed-restore-{token}.stage",
        target.parent / f".portable-fixed-restore-{token}.lock",
    )


def _fixed_target_restore_scope(
    *,
    target_root: Path,
    parent_identity: tuple[int, int],
    manifest: BackupManifest,
    executing_release_sha: str,
    release_binding_sha256: str,
    release_bundle_sha256: str,
    release_record_sha256: str,
) -> dict[str, Any]:
    return {
        "target_root": os.path.normcase(str(target_root)),
        "target_parent_device": parent_identity[0],
        "target_parent_inode": parent_identity[1],
        "backup_id": manifest.backup_id,
        "backup_manifest_sha256": manifest.manifest_sha256,
        "executing_release_sha": executing_release_sha,
        "release_binding_sha256": release_binding_sha256,
        "release_bundle_sha256": release_bundle_sha256,
        "release_record_sha256": release_record_sha256,
    }


def _revalidate_fixed_target_parent(
    target: Path,
    parent: Path,
    expected_identity: tuple[int, int],
) -> None:
    _reject_fixed_target_alias_chain(target)
    try:
        current = os.lstat(parent)
    except OSError as exc:
        raise RecoveryError(
            "restore_path_invalid", "fixed restore parent became unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or _is_reparse_point(parent)
        or (int(current.st_dev), int(current.st_ino)) != expected_identity
    ):
        raise RecoveryError(
            "restore_path_invalid", "fixed restore parent identity changed"
        )


def _fixed_target_seal_name(root: Path, manifest: BackupManifest) -> str:
    expected = (
        TAKEOVER_UNSEAL_MARKER
        if manifest.checkpoint_source is not None
        else _READ_ONLY_SEAL
    )
    alternate = (
        _READ_ONLY_SEAL if expected == TAKEOVER_UNSEAL_MARKER else TAKEOVER_UNSEAL_MARKER
    )
    expected_path = root / expected
    alternate_path = root / alternate
    if (
        expected_path.is_symlink()
        or alternate_path.is_symlink()
        or not expected_path.is_file()
        or alternate_path.exists()
    ):
        raise RecoveryError(
            "restore_not_physically_read_only",
            "restore does not retain its exact owner-selected seal",
        )
    return expected


def _require_fixed_restore_original_closure(
    root: Path,
    manifest: BackupManifest,
) -> None:
    """Authenticate an interrupted publication before changing its protection."""

    seal_name = (
        TAKEOVER_UNSEAL_MARKER
        if manifest.checkpoint_source is not None
        else _READ_ONLY_SEAL
    )
    seal_path = root / seal_name
    protected, root_sha256, _fence_sha256, _evidence_count = (
        _expected_restored_materialization(manifest)
    )
    expected_seal = canonical_json_bytes(
        {
            "format": "research-workspace-read-only-v1",
            "lifecycle": RecoveryLifecycle.VERIFIED_READ_ONLY.value,
            "protected_files": list(protected),
            "protected_root_sha256": root_sha256,
            "writable": False,
            "mission_auto_resume": False,
            "canonical_effect": "none",
        }
    )
    try:
        before = os.lstat(seal_path)
        raw = seal_path.read_bytes()
        after = os.lstat(seal_path)
    except OSError as exc:
        raise RecoveryError(
            "restore_state_conflict",
            "interrupted fixed restore lacks its exact publication seal",
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or int(before.st_nlink) != 1
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or raw != expected_seal
    ):
        raise RecoveryError(
            "restore_state_conflict",
            "interrupted fixed restore publication seal changed",
        )
    _assert_plain_tree(
        root,
        code="restore_state_conflict",
        require_independent_files=True,
    )
    actual_files = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file()
    }
    if actual_files != {*protected, seal_name} or (
        _tree_digest(root, protected) != root_sha256
    ):
        raise RecoveryError(
            "restore_state_conflict",
            "interrupted fixed restore differs from the exact source closure",
        )


def prepare_portable_fixed_target_restore(
    *,
    backup_directory: Path | str,
    target_root: Path | str,
    installed_release_root: Path | str,
    executing_release_sha: str,
    expected_transition_head: str | None = None,
    minimum_project_commit_id: int | None = None,
    observer: WorkspaceObserver = NOOP_WORKSPACE_OBSERVER,
    observation_environment: ObservationEnvironment = ObservationEnvironment.LOCAL,
) -> RestoredWorkspace:
    """Import a portable BackupSet through one crash-replayable fixed stage."""

    return _prepare_portable_fixed_target_restore(
        backup_directory=backup_directory,
        target_root=target_root,
        installed_release_root=installed_release_root,
        executing_release_sha=executing_release_sha,
        expected_transition_head=expected_transition_head,
        minimum_project_commit_id=minimum_project_commit_id,
        observer=observer,
        observation_environment=observation_environment,
        verified_source=None,
    )


def _prepare_portable_fixed_target_restore(
    *,
    backup_directory: Path | str,
    target_root: Path | str,
    installed_release_root: Path | str,
    executing_release_sha: str,
    expected_transition_head: str | None = None,
    minimum_project_commit_id: int | None = None,
    observer: WorkspaceObserver = NOOP_WORKSPACE_OBSERVER,
    observation_environment: ObservationEnvironment = ObservationEnvironment.LOCAL,
    verified_source: BackupSet | None,
) -> RestoredWorkspace:
    restored_root, parent, parent_identity = _validated_fixed_target_path(target_root)
    # Staging already admitted this exact completed source against its retained
    # manifest pin. Recheck its bytes/custody, not SQLite and Evidence history.
    source = (
        _verify_backup_tree_contents(
            backup_directory,
            enforce_source_location=False,
        )
        if verified_source is None
        else _inspect_backup_tree_contents(
            backup_directory,
            enforce_source_location=False,
            expected_manifest_sha256=verified_source.manifest.manifest_sha256,
        )
    )
    verified = _verify_portable_restore_source(source, verified_source=verified_source)
    (
        release_binding_sha256,
        release_bundle_sha256,
        release_record_sha256,
    ) = _verify_installed_release_canonical_archive(
        verified.manifest.canonical,
        installed_release_root,
        executing_release_sha=executing_release_sha,
    )
    if (
        expected_transition_head is not None
        and verified.manifest.store.transition_head != expected_transition_head
    ):
        raise RecoveryError("stale_backup", "backup transition head differs")
    if (
        minimum_project_commit_id is not None
        and verified.manifest.store.project_commit_id < minimum_project_commit_id
    ):
        raise RecoveryError(
            "stale_backup", "backup project commit predates recovery floor"
        )
    _ = _derive_external_reconciliation(
        verified.path / "workspace.sqlite3"
    )
    stage, lock_path = _fixed_target_restore_artifacts(restored_root)
    scope = _fixed_target_restore_scope(
        target_root=restored_root,
        parent_identity=parent_identity,
        manifest=verified.manifest,
        executing_release_sha=executing_release_sha,
        release_binding_sha256=release_binding_sha256,
        release_bundle_sha256=release_bundle_sha256,
        release_record_sha256=release_record_sha256,
    )
    try:
        with acquire_kernel_lease(
            lock_path,
            format_name=_FIXED_RESTORE_LOCK_FORMAT,
            scope=scope,
        ) as lease:
            lease.assert_held()
            _revalidate_fixed_target_parent(
                restored_root,
                parent,
                parent_identity,
            )
            try:
                target_entry = os.lstat(restored_root)
            except FileNotFoundError:
                target_entry = None
            try:
                stage_entry = os.lstat(stage)
            except FileNotFoundError:
                stage_entry = None
            exact_abandoned_owner = (
                lease.prior_abandoned
                and lease.predecessor_scope_sha256 == lease.scope_sha256
            )
            if target_entry is not None:
                if (
                    not exact_abandoned_owner
                    or stage_entry is not None
                    or not stat.S_ISDIR(target_entry.st_mode)
                    or _is_reparse_point(restored_root)
                ):
                    raise RecoveryError(
                        "restore_path_collision",
                        "fixed restore target already exists outside exact crash replay",
                    )
                # Death after atomic publication may precede final delete-deny
                # normalization.  The in-tree marker already gates Store access.
                _require_fixed_restore_original_closure(
                    restored_root,
                    verified.manifest,
                )
                if os.name == "nt":
                    _remove_windows_write_deny(restored_root, recursive=True)
                _protect_closed_tree(restored_root)
            else:
                if stage_entry is not None:
                    if (
                        not exact_abandoned_owner
                        or not stat.S_ISDIR(stage_entry.st_mode)
                        or _is_reparse_point(stage)
                    ):
                        raise RecoveryError(
                            "restore_path_collision",
                            "fixed restore stage lacks exact abandoned-operation authority",
                        )
                    _assert_plain_tree(
                        stage,
                        code="recovery_cleanup_unsafe",
                        require_independent_files=True,
                    )
                    _remove_tree(stage, owned_parent=parent)
                    _fsync_directory(parent)
                _execute_restore_copy(
                    source_root=verified.path,
                    target_root=stage,
                    owned_parent=parent,
                    sqlite_sha256=verified.manifest.sqlite_sha256,
                    sqlite_length=verified.manifest.sqlite_length,
                    inventory=verified.manifest.evidence_inventory,
                    directives=verified.manifest.deletion_directives,
                    tombstones=verified.manifest.tombstones,
                    takeover_pending=verified.manifest.checkpoint_source is not None,
                    publishable_stage=True,
                )
                lease.assert_held()
                _revalidate_fixed_target_parent(
                    restored_root,
                    parent,
                    parent_identity,
                )
                try:
                    os.lstat(restored_root)
                except FileNotFoundError:
                    pass
                else:
                    raise RecoveryError(
                        "restore_path_collision",
                        "fixed restore target appeared before atomic publication",
                    )
                _assert_plain_tree(
                    stage,
                    code="restored_workspace_corrupt",
                    require_independent_files=True,
                )
                os.rename(stage, restored_root)
                _fsync_directory(parent)
                if os.name == "nt":
                    _remove_windows_write_deny(restored_root, recursive=True)
                _protect_closed_tree(restored_root)
            restored = _inspect_portable_fixed_target_restore(
                backup=verified,
                target_root=restored_root,
                verified_source=verified_source,
            )
            lease.assert_held()
            _revalidate_fixed_target_parent(
                restored_root,
                parent,
                parent_identity,
            )
    except KernelLeaseError as exc:
        code = (
            "restore_target_busy"
            if exc.code == "kernel_lease_busy"
            else "restore_path_invalid"
        )
        raise RecoveryError(code, str(exc)) from exc
    emit_event_safely(
        observer,
        kind=EventKind.RESTORE_CHANGED,
        occurred_at=_now(),
        environment=ObservationEnvironment(observation_environment),
        attributes={
            "project_id": verified.manifest.store.project_id,
            "backup_id": verified.manifest.backup_id,
            "project_commit": verified.manifest.store.project_commit_id,
            "root_digest": verified.manifest.store.project_root_digest,
            "status": "verified_read_only_fixed_target",
        },
    )
    _verify_restored_workspace(restored, verified_source=verified_source)
    return restored


def _inspect_portable_fixed_target_restore(
    *,
    backup: BackupSet,
    target_root: Path | str,
    verified_source: BackupSet | None = None,
) -> RestoredWorkspace:
    """Reissue only an exact, still-sealed fixed-target restore object."""

    root, parent, _parent_identity = _validated_fixed_target_path(target_root)
    verified = _verify_portable_restore_source(backup, verified_source=verified_source)
    try:
        root_entry = os.lstat(root)
    except OSError as exc:
        raise RecoveryError(
            "restore_path_invalid", "fixed restore target is unavailable"
        ) from exc
    if not stat.S_ISDIR(root_entry.st_mode) or _is_reparse_point(root):
        raise RecoveryError(
            "restore_path_invalid", "fixed restore target is not a plain directory"
        )
    paths = WorkspacePaths.from_root(root)
    paths.revalidate_physical(require_root=True, require_database=True)
    _assert_plain_tree(
        root,
        code="restored_workspace_corrupt",
        require_independent_files=True,
    )
    protected, root_sha256, fence_sha256, evidence_count = (
        _expected_restored_materialization(verified.manifest)
    )
    seal_name = _fixed_target_seal_name(root, verified.manifest)
    seal_path = root / seal_name
    expected_seal = canonical_json_bytes(
        {
            "format": "research-workspace-read-only-v1",
            "lifecycle": RecoveryLifecycle.VERIFIED_READ_ONLY.value,
            "protected_files": list(protected),
            "protected_root_sha256": root_sha256,
            "writable": False,
            "mission_auto_resume": False,
            "canonical_effect": "none",
        }
    )
    if (
        seal_path.is_symlink()
        or not seal_path.is_file()
        or seal_path.read_bytes() != expected_seal
    ):
        raise RecoveryError(
            "restore_not_physically_read_only",
            "fixed restore does not retain its exact sealed closure",
        )
    restored = RestoredWorkspace(
        root=root,
        backup=verified,
        lifecycle=RecoveryLifecycle.VERIFIED_READ_ONLY,
        reconciliation=_derive_external_reconciliation(paths.database),
        verification=RestoreVerification(
            restored_root_sha256=root_sha256,
            deletion_fence_sha256=fence_sha256,
            read_only_seal_sha256=_digest(expected_seal),
            sqlite_integrity="passed",
            evidence_count=evidence_count,
            verified_at=_now(),
        ),
        source_materialization_portable=True,
        restore_parent=parent,
        caller_selected_target=True,
        _fixed_target_token=_RECOVERY_ISSUER_TOKEN,
    )
    _verify_restored_workspace(restored, verified_source=verified_source)
    return restored


def _validated_staging_target_path(
    target_root: Path | str,
    *,
    mission_id: str,
) -> tuple[Path, Path, tuple[int, int]]:
    """Require the one staging-only ``rh-staging/workspaces/<Mission>`` shape."""

    if (
        not isinstance(mission_id, str)
        or not mission_id
        or BACKUP_ID_RE.fullmatch(mission_id) is None
    ):
        raise RecoveryError(
            "staging_identity_invalid",
            "staging import requires one portable Mission identity",
        )
    target, parent, parent_identity = _validated_fixed_target_path(target_root)
    if (
        target.name != mission_id
        or parent.name != _STAGING_WORKSPACES_COMPONENT
        or parent.parent.name != _STAGING_PATH_COMPONENT
    ):
        raise RecoveryError(
            "staging_path_invalid",
            "staging Workspace must be exactly rh-staging/workspaces/<Mission>",
        )
    if any(part.casefold() in _PRODUCTION_PATH_MARKERS for part in target.parts):
        raise RecoveryError(
            "staging_path_invalid",
            "staging Workspace path contains a production-only marker",
        )
    return target, parent, parent_identity


def _reject_production_path(path: Path | str, *, label: str) -> None:
    supplied = Path(path).expanduser()
    try:
        resolved = supplied.resolve(strict=False)
    except OSError as exc:
        raise RecoveryError(
            "staging_path_invalid",
            f"{label} is not a usable staging path",
        ) from exc
    if any(
        part.casefold() in _PRODUCTION_PATH_MARKERS
        for candidate in (supplied.absolute(), resolved)
        for part in candidate.parts
    ):
        raise RecoveryError(
            "staging_path_invalid",
            f"{label} contains a production-only path marker",
        )


def _reject_staging_credential_material(root: Path) -> None:
    """Reject credential-shaped filesystem members without reading their bytes."""

    if root.exists() or root.is_symlink():
        for item in (root, *root.rglob("*")):
            if item.name.casefold() in _STAGING_CREDENTIAL_FILENAMES:
                raise RecoveryError(
                    "staging_credential_material_forbidden",
                    "staging import tree contains credential-shaped material",
                )


def _require_provider_free_staging_environment(
    *,
    target_root: Path,
    mission_id: str,
) -> None:
    inherited = tuple(
        key for key in _STAGING_FORBIDDEN_ENVIRONMENT_KEYS if os.environ.get(key)
    )
    if inherited:
        raise RecoveryError(
            "staging_credential_environment_forbidden",
            "staging import refuses inherited provider, Git, or Codex credentials",
        )
    production_markers = tuple(
        key
        for key in _STAGING_ENVIRONMENT_MARKER_KEYS
        if os.environ.get(key, "").strip().casefold()
        in _PRODUCTION_ENVIRONMENT_MARKERS
    )
    if production_markers:
        raise RecoveryError(
            "staging_environment_forbidden",
            "staging import refuses an inherited production environment marker",
        )
    configured_mission = os.environ.get("RH_MISSION_HOST_MISSION_ID")
    if configured_mission is not None and configured_mission != mission_id:
        raise RecoveryError(
            "staging_environment_mismatch",
            "inherited Mission identity differs from the staging import",
        )
    configured_workspace = os.environ.get("RH_MISSION_HOST_WORKSPACE_ROOT")
    if configured_workspace is not None:
        try:
            configured = Path(configured_workspace).expanduser().resolve(strict=False)
        except OSError as exc:
            raise RecoveryError(
                "staging_environment_mismatch",
                "inherited Workspace root is not a usable staging path",
            ) from exc
        if configured != target_root:
            raise RecoveryError(
                "staging_environment_mismatch",
                "inherited Workspace root differs from the staging import target",
            )


def _staging_mission_binding_from_connection(
    connection: sqlite3.Connection,
    *,
    mission_id: str,
) -> tuple[int, str]:
    rows = tuple(
        connection.execute(
            "SELECT h.object_id, h.revision, h.payload_digest, r.payload_json "
            "FROM mission_head h JOIN mission_revision r "
            "ON r.object_id = h.object_id AND r.revision = h.revision "
            "ORDER BY h.object_id"
        )
    )
    if len(rows) != 1 or str(rows[0]["object_id"]) != mission_id:
        raise RecoveryError(
            "staging_mission_mismatch",
            "portable BackupSet does not have the one requested current Mission",
        )
    row = rows[0]
    try:
        payload = loads_strict_json_object(str(row["payload_json"]).encode("utf-8"))
    except Exception as exc:
        raise RecoveryError(
            "staging_mission_mismatch",
            "portable BackupSet Mission payload is invalid",
        ) from exc
    if (
        payload.get("mission_id") != mission_id
        or _digest(canonical_json_bytes(payload)) != str(row["payload_digest"])
    ):
        raise RecoveryError(
            "staging_mission_mismatch",
            "portable BackupSet Mission identity or digest differs",
        )
    return int(row["revision"]), str(row["payload_digest"])


def _staging_import_seal_raw(manifest: BackupManifest) -> bytes:
    protected, root_sha256, _fence_sha256, _evidence_count = (
        _expected_restored_materialization(manifest)
    )
    return canonical_json_bytes(
        {
            "format": "research-workspace-read-only-v1",
            "lifecycle": RecoveryLifecycle.VERIFIED_READ_ONLY.value,
            "protected_files": list(protected),
            "protected_root_sha256": root_sha256,
            "writable": False,
            "mission_auto_resume": False,
            "canonical_effect": "none",
        }
    )


def _staging_import_lock_path(target_root: Path) -> Path:
    token = _digest(
        canonical_json_bytes(
            {
                "format": "research-portable-staging-import-name-v1",
                "target_root": os.path.normcase(str(target_root)),
            }
        )
    )[:32]
    return target_root.parent / f".portable-staging-import-{token}.lock"


def _staging_import_evidence(
    *,
    manifest: BackupManifest,
    target_root_identity: str,
    mission_id: str,
    installed_release_sha: str,
    release_binding_sha256: str,
    release_bundle_sha256: str,
    release_record_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": _STAGING_IMPORT_EVIDENCE_SCHEMA,
        "environment": "disposable_staging",
        "mission_id": mission_id,
        "backup_id": manifest.backup_id,
        "backup_manifest_sha256": manifest.manifest_sha256,
        "source_root_identity": manifest.store.root_identity,
        "target_root_identity": target_root_identity,
        "installed_release_sha": installed_release_sha,
        "release_binding_sha256": release_binding_sha256,
        "release_bundle_sha256": release_bundle_sha256,
        "release_record_sha256": release_record_sha256,
        "source_writer_exclusion_claim": "none_independent_copy_only",
        "mission_auto_resume": False,
        "provider_effect": False,
        "canonical_effect": "none",
    }


def _staging_source_and_target_are_disjoint(source: Path, target: Path) -> None:
    try:
        source.relative_to(target)
    except ValueError:
        pass
    else:
        raise RecoveryError(
            "staging_path_invalid",
            "portable source cannot be inside the staging Workspace target",
        )
    try:
        target.relative_to(source)
    except ValueError:
        return
    raise RecoveryError(
        "staging_path_invalid",
        "staging Workspace target cannot be inside the portable source",
    )


def _classify_staging_import_database(
    *,
    paths: WorkspacePaths,
    manifest: BackupManifest,
    mission_id: str,
    writer_owner: str,
    writer_evidence: Mapping[str, Any],
) -> tuple[str, int, int, str]:
    connection = open_snapshot_connection(paths.database)
    try:
        metadata = connection.execute(
            "SELECT * FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None:
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging import target has no Workspace metadata",
            )
        mission_revision, mission_payload_sha256 = (
            _staging_mission_binding_from_connection(
                connection,
                mission_id=mission_id,
            )
        )
        if (
            str(metadata["project_id"]) != _RH_STAGING_PROJECT_ID
            or int(metadata["current_project_commit"])
            != manifest.store.project_commit_id
            or str(metadata["current_root_digest"])
            != manifest.store.project_root_digest
            or metadata["transition_head_digest"]
            != manifest.store.transition_head
            or str(metadata["canonical_authority_digest"])
            != manifest.store.canonical_authority_digest
            or int(metadata["schema_version"]) != manifest.store.schema_version
            or str(metadata["operating_mode"]) != manifest.store.operating_mode
            or int(metadata["root_digest_version"]) != manifest.store.root_digest_version
        ):
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging import target differs from the verified BackupSet cut",
            )
        tail = _validated_writer_epoch_tail(
            connection,
            project_id=_RH_STAGING_PROJECT_ID,
        )
        source_prestate = (
            str(metadata["root_identity"]) == manifest.store.root_identity
            and metadata["current_writer_epoch"] is None
            and str(metadata["lifecycle"])
            in {RecoveryLifecycle.OFFLINE.value, RecoveryLifecycle.QUIESCED.value}
            and (
                (tail is None and manifest.store.writer_epoch_high_watermark == 0)
                or (
                    tail is not None
                    and int(tail["epoch"])
                    == manifest.store.writer_epoch_high_watermark
                )
            )
        )
        if source_prestate:
            return (
                "source_prestate",
                manifest.store.writer_epoch_high_watermark,
                mission_revision,
                mission_payload_sha256,
            )
        expected_epoch = manifest.store.writer_epoch_high_watermark + 1
        try:
            persisted_evidence = (
                loads_strict_json_object(
                    str(tail["takeover_evidence_json"]).encode("utf-8")
                )
                if tail is not None and tail["takeover_evidence_json"] is not None
                else None
            )
        except Exception as exc:
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging writer provenance is invalid",
            ) from exc
        committed = (
            str(metadata["root_identity"]) == paths.root_identity
            and metadata["current_writer_epoch"] is None
            and str(metadata["lifecycle"]) == RecoveryLifecycle.QUIESCED.value
            and tail is not None
            and int(tail["epoch"]) == expected_epoch
            and int(tail["predecessor_epoch"] or 0)
            == manifest.store.writer_epoch_high_watermark
            and str(tail["owner"]) == writer_owner
            and str(tail["lifecycle"]) == RecoveryLifecycle.QUIESCED.value
            and str(tail["creation_basis"])
            == f"staging-portable-import:{manifest.manifest_sha256}"
            and tail["closed_at"] is not None
            and persisted_evidence == dict(writer_evidence)
        )
        if committed:
            return (
                "committed",
                expected_epoch,
                mission_revision,
                mission_payload_sha256,
            )
    finally:
        connection.close()
    raise RecoveryError(
        "staging_import_state_invalid",
        "target is neither the verified source copy nor its exact staging rebind",
    )


def _make_staging_workspace_writable(paths: WorkspacePaths) -> None:
    _remove_windows_write_deny(paths.root, recursive=True)
    _make_owner_writable(paths.root)
    for directory in sorted(
        (item for item in paths.root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
    ):
        _make_owner_writable(directory)
    _make_owner_writable(paths.database)
    # A failed completion may already have entered WAL before containment
    # protected the entire derivative. Resume only these SQLite-owned mutable
    # files; protected Evidence and source BackupSet bytes stay untouched.
    for suffix in ("-wal", "-shm"):
        sidecar = paths.database.with_name(paths.database.name + suffix)
        if os.path.lexists(sidecar):
            entry = sidecar.lstat()
            if (_is_reparse_point(sidecar) or not stat.S_ISREG(entry.st_mode)
                    or int(entry.st_nlink) != 1):
                raise RecoveryError(
                    "staging_import_state_invalid",
                    "staging SQLite sidecar is not a plain independent file",
                )
            _make_owner_writable(sidecar)


def _unseal_staging_import_prestate(
    *,
    restored: RestoredWorkspace,
    paths: WorkspacePaths,
    verified_source: BackupSet,
) -> bytes:
    # The same import owner just inspected this exact protected target under
    # its held lease. Unsealing does not need another full byte/custody scan.
    if restored.backup != verified_source:
        raise RecoveryError("stale_backup", "staging restore source changed")
    manifest = restored.backup.manifest
    expected_raw = _staging_import_seal_raw(manifest)
    seal_name = (
        TAKEOVER_UNSEAL_MARKER
        if manifest.checkpoint_source is not None
        else _READ_ONLY_SEAL
    )
    seal_path = paths.root / seal_name
    marker_path = paths.root / TAKEOVER_UNSEAL_MARKER
    try:
        if seal_path.is_symlink() or not seal_path.is_file():
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging import source seal is unavailable",
            )
        if seal_path.read_bytes() != expected_raw:
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging import source seal differs from the verified closure",
            )
        if seal_name != TAKEOVER_UNSEAL_MARKER:
            if marker_path.exists() or marker_path.is_symlink():
                raise RecoveryError(
                    "staging_import_state_invalid",
                    "staging import marker already exists",
                )
            _remove_windows_write_deny(paths.root, recursive=True)
            _make_owner_writable(paths.root)
            _make_owner_writable(seal_path)
            seal_path.replace(marker_path)
            _fsync_directory(paths.root)
        _make_staging_workspace_writable(paths)
        _make_owner_writable(marker_path)
        if marker_path.read_bytes() != expected_raw:
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging import marker differs from the verified source closure",
            )
    except BaseException as exc:
        try:
            _restore_staging_prestate_protection(paths=paths, manifest=manifest)
        except BaseException as protection_error:
            protection_error.add_note(
                "staging unseal also raised " f"{type(exc).__name__}"
            )
            raise protection_error from exc
        raise
    return expected_raw


def _restore_staging_prestate_protection(
    *,
    paths: WorkspacePaths,
    manifest: BackupManifest,
) -> None:
    marker_path = paths.root / TAKEOVER_UNSEAL_MARKER
    expected_name = (
        TAKEOVER_UNSEAL_MARKER
        if manifest.checkpoint_source is not None
        else _READ_ONLY_SEAL
    )
    expected_path = paths.root / expected_name
    try:
        _remove_windows_write_deny(paths.root, recursive=True)
        _make_owner_writable(paths.root)
        if expected_name != TAKEOVER_UNSEAL_MARKER and marker_path.exists():
            _make_owner_writable(marker_path)
            marker_path.replace(expected_path)
        _strictly_protect_closed_tree_or_force(paths.root)
    except BaseException:
        _force_owner_read_only_tree(paths.root)
        raise


def _commit_staging_root_rebind(
    *,
    paths: WorkspacePaths,
    manifest: BackupManifest,
    mission_id: str,
    writer_owner: str,
    writer_evidence: Mapping[str, Any],
) -> int:
    """Atomically rebind only physical custody and append a quiesced writer."""

    connection = sqlite3.connect(paths.database, timeout=0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 0")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA synchronous = FULL")
    try:
        connection.execute("BEGIN IMMEDIATE")
        metadata = connection.execute(
            "SELECT * FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        tail = _validated_writer_epoch_tail(
            connection,
            project_id=_RH_STAGING_PROJECT_ID,
        )
        _staging_mission_binding_from_connection(
            connection,
            mission_id=mission_id,
        )
        if (
            metadata is None
            or str(metadata["project_id"]) != _RH_STAGING_PROJECT_ID
            or str(metadata["root_identity"]) != manifest.store.root_identity
            or metadata["current_writer_epoch"] is not None
            or str(metadata["lifecycle"])
            not in {RecoveryLifecycle.OFFLINE.value, RecoveryLifecycle.QUIESCED.value}
            or int(metadata["current_project_commit"])
            != manifest.store.project_commit_id
            or str(metadata["current_root_digest"])
            != manifest.store.project_root_digest
            or metadata["transition_head_digest"]
            != manifest.store.transition_head
            or str(metadata["canonical_authority_digest"])
            != manifest.store.canonical_authority_digest
            or (
                (tail is None and manifest.store.writer_epoch_high_watermark != 0)
                or (
                    tail is not None
                    and int(tail["epoch"])
                    != manifest.store.writer_epoch_high_watermark
                )
            )
        ):
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging rebind source no longer matches the verified BackupSet",
            )
        now = _now()
        writer_epoch = manifest.store.writer_epoch_high_watermark + 1
        writer_values = {
            "epoch": writer_epoch,
            "project_id": _RH_STAGING_PROJECT_ID,
            "owner": writer_owner,
            "lifecycle": RecoveryLifecycle.QUIESCED.value,
            "creation_basis": (
                f"staging-portable-import:{manifest.manifest_sha256}"
            ),
            "predecessor_epoch": (
                manifest.store.writer_epoch_high_watermark or None
            ),
            "takeover_evidence_json": canonical_json_bytes(
                deep_thaw(writer_evidence)
            ).decode("utf-8"),
            "created_at": now,
            "closed_at": now,
        }
        connection.execute(
            "INSERT INTO writer_epoch("
            "epoch, project_id, owner, lifecycle, creation_basis, "
            "predecessor_epoch, takeover_evidence_json, created_at, closed_at, "
            "row_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                *(writer_values[column] for column in _WRITER_DIGEST_COLUMNS),
                _writer_digest(writer_values),
            ),
        )
        connection.execute(
            "UPDATE workspace_metadata SET root_identity = ?, "
            "current_writer_epoch = NULL, lifecycle = ?, updated_at = ? "
            "WHERE singleton = 1",
            (paths.root_identity, RecoveryLifecycle.QUIESCED.value, now),
        )
        connection.execute("COMMIT")
        return writer_epoch
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _normalize_staging_database_profile(paths: WorkspacePaths) -> None:
    """Move a committed staging copy from sealed DELETE mode to active WAL."""

    connection = sqlite3.connect(paths.database, timeout=0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        connection.execute("PRAGMA trusted_schema = OFF")
        mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if mode.casefold() != "wal":
            raise RecoveryError(
                "staging_import_state_invalid",
                "committed staging Store could not enter the required WAL mode",
            )
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _complete_staging_import(
    *,
    source: BackupSet,
    paths: WorkspacePaths,
    manifest: BackupManifest,
    mission_id: str,
    writer_owner: str,
    writer_evidence: Mapping[str, Any],
    writer_epoch: int,
    installed_release_root: Path | str,
    installed_release_sha: str,
    seal_raw: bytes,
) -> StagingPortableImportReport:
    marker = paths.root / TAKEOVER_UNSEAL_MARKER
    try:
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != seal_raw:
                raise RecoveryError(
                    "staging_import_state_invalid",
                    "committed staging import marker is unsafe or changed",
                )
        else:
            _write_exclusive(marker, seal_raw)
            _fsync_directory(paths.root)
        _make_staging_workspace_writable(paths)
        _make_owner_writable(marker)
        _normalize_staging_database_profile(paths)
        for directory in (paths.staging, paths.backups, paths.recovery):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.revalidate_physical(require_root=True, require_database=True)
        _reject_staging_credential_material(paths.root)
        store = WorkspaceStore(paths, _RH_STAGING_PROJECT_ID)
        # The protected exact copy has already earned its semantic result.
        # The only writer operation is the transaction in
        # _commit_staging_root_rebind (one writer row and physical metadata),
        # followed by SQLite's journal-mode change. Read back that exact
        # bounded transformation; do not audit unchanged mathematical history.
        metadata = store.read_metadata()
        mission_revision, mission_payload_sha256 = (
            _staging_mission_binding_from_database(paths.database, mission_id=mission_id)
        )
        state, exact_epoch, _revision, _payload_digest = (
            _classify_staging_import_database(
                paths=paths,
                manifest=manifest,
                mission_id=mission_id,
                writer_owner=writer_owner,
                writer_evidence=writer_evidence,
            )
        )
        if state != "committed" or exact_epoch != writer_epoch:
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging import did not retain its exact quiesced writer rebind",
            )
        cas = EvidenceCAS(paths)
        for record in _restorable_inventory(
            manifest.evidence_inventory,
            manifest.deletion_directives,
        ):
            cas.verify_record(record)
        try:
            _revalidate_unchanged_portable_backup(source)
        except RecoveryError as exc:
            if exc.code == "stale_backup":
                raise RecoveryError(
                    "staging_source_changed",
                    "portable source changed during staging import",
                ) from exc
            raise
        _verify_installed_release_canonical_archive(
            manifest.canonical,
            installed_release_root,
            executing_release_sha=installed_release_sha,
        )
        target_database_sha256, _target_database_length = _sha256_file(paths.database)
        if (
            int(metadata["current_project_commit"]) != manifest.store.project_commit_id
            or str(metadata["current_root_digest"])
            != manifest.store.project_root_digest
            or metadata["transition_head_digest"]
            != manifest.store.transition_head
            or str(metadata["canonical_authority_digest"])
            != manifest.store.canonical_authority_digest
        ):
            raise RecoveryError(
                "staging_import_state_invalid",
                "staging import changed semantic or canonical Store identity",
            )
        report = StagingPortableImportReport(
            schema_version=_STAGING_IMPORT_REPORT_SCHEMA,
            status="staging_imported_quiesced",
            mission_id=mission_id,
            project_id=_RH_STAGING_PROJECT_ID,
            backup_id=manifest.backup_id,
            source_manifest_sha256=manifest.manifest_sha256,
            source_database_sha256=manifest.sqlite_sha256,
            source_root_identity=manifest.store.root_identity,
            target_database_sha256=target_database_sha256,
            target_root_identity=paths.root_identity,
            project_commit=manifest.store.project_commit_id,
            project_root_digest=manifest.store.project_root_digest,
            transition_head=manifest.store.transition_head,
            canonical_authority_digest=manifest.store.canonical_authority_digest,
            mission_revision=mission_revision,
            mission_payload_sha256=mission_payload_sha256,
            writer_owner=writer_owner,
            writer_epoch=writer_epoch,
            writer_lifecycle=RecoveryLifecycle.QUIESCED.value,
            portable_verification_passed=True,
            structural_verification_passed=True,
            semantic_verification_passed=True,
            cas_verification_passed=True,
            source_material_unchanged=True,
            mission_mutations=0,
            provider_effect=False,
            canonical_effect="none",
        )
        marker.unlink()
        _fsync_directory(paths.root)
        return report
    except BaseException as exc:
        containment_error: BaseException | None = None
        try:
            _remove_windows_write_deny(paths.root, recursive=True)
            _make_owner_writable(paths.root)
            if not marker.exists() and not marker.is_symlink():
                _write_exclusive(marker, seal_raw)
            _strictly_protect_closed_tree_or_force(paths.root)
        except BaseException as failure:
            containment_error = failure
        if containment_error is not None:
            containment_error.add_note(
                "staging post-import verification also raised "
                f"{type(exc).__name__}"
            )
            raise containment_error from exc
        raise


def _staging_mission_binding_from_database(
    database: Path,
    *,
    mission_id: str,
) -> tuple[int, str]:
    connection = open_snapshot_connection(database)
    try:
        return _staging_mission_binding_from_connection(
            connection,
            mission_id=mission_id,
        )
    finally:
        connection.close()


def _staging_source_writer_owner(database: Path) -> str | None:
    connection = open_snapshot_connection(database)
    try:
        tail = _validated_writer_epoch_tail(
            connection,
            project_id=_RH_STAGING_PROJECT_ID,
        )
        return None if tail is None else str(tail["owner"])
    finally:
        connection.close()


def import_portable_backup_to_staging(
    *,
    backup_directory: Path | str,
    target_root: Path | str,
    installed_release_root: Path | str,
    installed_release_sha: str,
    mission_id: str,
    expected_backup_id: str,
    expected_manifest_sha256: str,
    observer: WorkspaceObserver = NOOP_WORKSPACE_OBSERVER,
    observation_environment: ObservationEnvironment = ObservationEnvironment.STAGING,
) -> StagingPortableImportReport:
    """Materialize one verified BackupSet as a quiesced disposable staging copy.

    This route deliberately does not call ``prepare_takeover`` and makes no
    source-writer exclusion claim.  The independent copy receives a new
    physical root and a staging-principal writer-history boundary while all
    Mission, mathematical, checkpoint, canonical, and CAS meaning stays exact.
    ``installed_release_*`` authenticates compatible canonical source bytes; it
    does not claim the identity of the Python release executing this function.
    """

    try:
        environment = ObservationEnvironment(observation_environment)
    except ValueError as exc:
        raise RecoveryError(
            "staging_environment_forbidden",
            "staging import requires the staging observation environment",
        ) from exc
    if environment is not ObservationEnvironment.STAGING:
        raise RecoveryError(
            "staging_environment_forbidden",
            "staging import cannot report as a local, CI, or production operation",
        )
    try:
        _require_digest(expected_manifest_sha256, "staging expected manifest")
    except ValueError as exc:
        raise RecoveryError(
            "staging_identity_invalid",
            "staging import expected manifest is invalid",
        ) from exc
    if (
        not isinstance(expected_backup_id, str)
        or BACKUP_ID_RE.fullmatch(expected_backup_id) is None
    ):
        raise RecoveryError(
            "staging_identity_invalid",
            "staging import expected BackupSet identity is invalid",
        )
    target, parent, parent_identity = _validated_staging_target_path(
        target_root,
        mission_id=mission_id,
    )
    _require_provider_free_staging_environment(
        target_root=target,
        mission_id=mission_id,
    )
    verified, _source_report = inspect_portable_backup_set(
        backup_directory,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    _reject_production_path(verified.path, label="portable BackupSet")
    _reject_staging_credential_material(verified.path)
    manifest = verified.manifest
    if (
        manifest.backup_id != expected_backup_id
        or manifest.manifest_sha256 != expected_manifest_sha256
    ):
        raise RecoveryError(
            "staging_backup_mismatch",
            "portable BackupSet differs from the exact staging request",
        )
    if manifest.store.project_id != _RH_STAGING_PROJECT_ID:
        raise RecoveryError(
            "staging_project_mismatch",
            "portable BackupSet is not the RH project Store",
        )
    if not _is_current_takeover_contract(manifest):
        raise RecoveryError(
            "staging_backup_unsupported",
            "staging import requires a supported direct Mission generation",
        )
    if (
        manifest.store.current_writer_epoch is not None
        or manifest.store.writer_lifecycle
        not in {RecoveryLifecycle.OFFLINE.value, RecoveryLifecycle.QUIESCED.value}
    ):
        raise RecoveryError(
            "staging_backup_not_quiesced",
            "staging import requires an offline or quiesced copied Store",
        )
    if (
        manifest.checkpoint_source is not None
        and manifest.checkpoint_source.mission_id != mission_id
    ):
        raise RecoveryError(
            "staging_mission_mismatch",
            "checkpoint source names another Mission",
        )
    mission_revision, _mission_payload_sha256 = (
        _staging_mission_binding_from_database(
            verified.path / "workspace.sqlite3",
            mission_id=mission_id,
        )
    )
    if mission_revision < 1:
        raise RecoveryError(
            "staging_mission_mismatch",
            "portable BackupSet has no current Mission revision",
        )
    _staging_source_and_target_are_disjoint(verified.path, target)
    _reject_production_path(installed_release_root, label="installed release root")
    (
        release_binding_sha256,
        release_bundle_sha256,
        release_record_sha256,
    ) = _verify_installed_release_canonical_archive(
        manifest.canonical,
        installed_release_root,
        executing_release_sha=installed_release_sha,
    )
    writer_owner = stable_principal_owner_binding(attest_current_principal())
    if _staging_source_writer_owner(
        verified.path / "workspace.sqlite3"
    ) == writer_owner:
        raise RecoveryError(
            "staging_principal_not_distinct",
            "staging import principal must differ from source writer custody",
        )
    lock_path = _staging_import_lock_path(target)
    scope = {
        "target_root": os.path.normcase(str(target)),
        "target_parent_device": parent_identity[0],
        "target_parent_inode": parent_identity[1],
        "mission_id": mission_id,
        "backup_id": manifest.backup_id,
        "backup_manifest_sha256": manifest.manifest_sha256,
        "installed_release_sha": installed_release_sha,
        "release_binding_sha256": release_binding_sha256,
        "release_bundle_sha256": release_bundle_sha256,
        "release_record_sha256": release_record_sha256,
        "writer_owner": writer_owner,
    }
    try:
        with acquire_kernel_lease(
            lock_path,
            format_name=_STAGING_IMPORT_LOCK_FORMAT,
            scope=scope,
        ) as lease:
            lease.assert_held()
            _revalidate_fixed_target_parent(target, parent, parent_identity)
            if not target.exists():
                _prepare_portable_fixed_target_restore(
                    backup_directory=verified.path,
                    target_root=target,
                    installed_release_root=installed_release_root,
                    executing_release_sha=installed_release_sha,
                    observer=observer,
                    observation_environment=environment,
                    verified_source=verified,
                )
            paths = WorkspacePaths.from_root(target)
            if paths.root_identity == manifest.store.root_identity:
                raise RecoveryError(
                    "staging_root_identity_invalid",
                    "staging copy did not acquire an independent physical root",
                )
            writer_evidence = _staging_import_evidence(
                manifest=manifest,
                target_root_identity=paths.root_identity,
                mission_id=mission_id,
                installed_release_sha=installed_release_sha,
                release_binding_sha256=release_binding_sha256,
                release_bundle_sha256=release_bundle_sha256,
                release_record_sha256=release_record_sha256,
            )
            exact_abandoned_owner = (
                lease.prior_abandoned
                and lease.predecessor_scope_sha256 == lease.scope_sha256
            )
            with _takeover_target_operation(
                paths,
                project_id=_RH_STAGING_PROJECT_ID,
            ):
                journal = paths.database.with_name(paths.database.name + "-journal")
                if journal.exists() or journal.is_symlink():
                    if not exact_abandoned_owner:
                        raise RecoveryError(
                            "staging_import_state_invalid",
                            "staging target has an unauthorized rollback journal",
                        )
                    if not _recover_interrupted_takeover_rollback_journal(
                        paths,
                        verified,
                    ):
                        raise RecoveryError(
                            "staging_import_state_invalid",
                            "staging rollback-journal recovery was not applicable",
                        )
                state, writer_epoch, _revision, _payload_digest = (
                    _classify_staging_import_database(
                        paths=paths,
                        manifest=manifest,
                        mission_id=mission_id,
                        writer_owner=writer_owner,
                        writer_evidence=writer_evidence,
                    )
                )
                seal_raw = _staging_import_seal_raw(manifest)
                if state == "source_prestate":
                    try:
                        restored = _inspect_portable_fixed_target_restore(
                            backup=verified,
                            target_root=target,
                            verified_source=verified,
                        )
                    except RecoveryError:
                        if not exact_abandoned_owner:
                            raise
                        marker = paths.root / TAKEOVER_UNSEAL_MARKER
                        original_seal = paths.root / (
                            TAKEOVER_UNSEAL_MARKER
                            if manifest.checkpoint_source is not None
                            else _READ_ONLY_SEAL
                        )
                        candidates = tuple(
                            item
                            for item in {marker, original_seal}
                            if item.exists() or item.is_symlink()
                        )
                        if (
                            len(candidates) != 1
                            or candidates[0].is_symlink()
                            or not candidates[0].is_file()
                        ):
                            raise
                        if candidates[0].read_bytes() != seal_raw:
                            raise
                        _restore_staging_prestate_protection(
                            paths=paths,
                            manifest=manifest,
                        )
                        restored = _inspect_portable_fixed_target_restore(
                            backup=verified,
                            target_root=target,
                            verified_source=verified,
                        )
                    _unseal_staging_import_prestate(
                        restored=restored, paths=paths, verified_source=verified,
                    )
                    try:
                        writer_epoch = _commit_staging_root_rebind(
                            paths=paths,
                            manifest=manifest,
                            mission_id=mission_id,
                            writer_owner=writer_owner,
                            writer_evidence=writer_evidence,
                        )
                    except BaseException:
                        try:
                            current_state, writer_epoch, _revision, _payload_digest = (
                                _classify_staging_import_database(
                                    paths=paths,
                                    manifest=manifest,
                                    mission_id=mission_id,
                                    writer_owner=writer_owner,
                                    writer_evidence=writer_evidence,
                                )
                            )
                        except BaseException:
                            current_state = "unknown"
                        if current_state == "source_prestate":
                            _restore_staging_prestate_protection(
                                paths=paths,
                                manifest=manifest,
                            )
                        else:
                            _force_owner_read_only_tree(paths.root)
                        raise
                lease.assert_held()
                report = _complete_staging_import(
                    source=verified,
                    paths=paths,
                    manifest=manifest,
                    mission_id=mission_id,
                    writer_owner=writer_owner,
                    writer_evidence=writer_evidence,
                    writer_epoch=writer_epoch,
                    installed_release_root=installed_release_root,
                    installed_release_sha=installed_release_sha,
                    seal_raw=seal_raw,
                )
            lease.assert_held()
            _revalidate_fixed_target_parent(target, parent, parent_identity)
    except KernelLeaseError as exc:
        code = (
            "staging_import_busy"
            if exc.code == "kernel_lease_busy"
            else "staging_path_invalid"
        )
        raise RecoveryError(code, str(exc)) from exc
    emit_event_safely(
        observer,
        kind=EventKind.RESTORE_CHANGED,
        occurred_at=_now(),
        environment=environment,
        attributes={
            "project_id": manifest.store.project_id,
            "backup_id": manifest.backup_id,
            "project_commit": manifest.store.project_commit_id,
            "root_digest": manifest.store.project_root_digest,
            "status": "staging_imported_quiesced",
        },
    )
    return report


def _reconcile_fixed_target_restore_owner_state(
    *,
    backup: BackupSet,
    target_root: Path,
    parent: Path,
    parent_identity: tuple[int, int],
    executing_release_sha: str,
    release_binding_sha256: str,
    release_bundle_sha256: str,
    release_record_sha256: str,
) -> None:
    """Release one exact abandoned restore owner before Store activation."""

    stage, lock_path = _fixed_target_restore_artifacts(target_root)
    scope = _fixed_target_restore_scope(
        target_root=target_root,
        parent_identity=parent_identity,
        manifest=backup.manifest,
        executing_release_sha=executing_release_sha,
        release_binding_sha256=release_binding_sha256,
        release_bundle_sha256=release_bundle_sha256,
        release_record_sha256=release_record_sha256,
    )
    try:
        with acquire_kernel_lease(
            lock_path,
            format_name=_FIXED_RESTORE_LOCK_FORMAT,
            scope=scope,
        ) as lease:
            lease.assert_held()
            _revalidate_fixed_target_parent(target_root, parent, parent_identity)
            if lease.prior_abandoned and (
                lease.predecessor_scope_sha256 != lease.scope_sha256
            ):
                raise RecoveryError(
                    "restore_state_conflict",
                    "abandoned fixed restore state belongs to another exact operation",
                )
            try:
                os.lstat(stage)
            except FileNotFoundError:
                pass
            else:
                raise RecoveryError(
                    "restore_state_conflict",
                    "published fixed target cannot coexist with a restore stage",
                )
            try:
                target_entry = os.lstat(target_root)
            except OSError as exc:
                raise RecoveryError(
                    "restore_state_conflict",
                    "fixed restore target disappeared during owner reconciliation",
                ) from exc
            if not stat.S_ISDIR(target_entry.st_mode) or _is_reparse_point(target_root):
                raise RecoveryError(
                    "restore_state_conflict",
                    "fixed restore target is not a plain owned directory",
                )
            if lease.prior_abandoned:
                paths = WorkspacePaths.from_root(target_root)
                paths.revalidate_physical(require_root=True, require_database=True)
                _takeover_marker_restore_binding(
                    paths,
                    backup.manifest,
                    require_original_closure=True,
                )
                if os.name == "nt":
                    _remove_windows_write_deny(target_root, recursive=True)
                _protect_closed_tree(target_root)
                _inspect_portable_fixed_target_restore(
                    backup=backup,
                    target_root=target_root,
                )
            lease.assert_held()
            _revalidate_fixed_target_parent(target_root, parent, parent_identity)
    except KernelLeaseError as exc:
        code = (
            "restore_target_busy"
            if exc.code == "kernel_lease_busy"
            else "restore_path_invalid"
        )
        raise RecoveryError(code, str(exc)) from exc


def verify_restored_workspace(restored: RestoredWorkspace) -> None:
    _verify_restored_workspace(restored, verified_source=None)


def _verify_restored_workspace(
    restored: RestoredWorkspace,
    *,
    verified_source: BackupSet | None,
) -> None:
    if restored.caller_selected_target and (
        restored._fixed_target_token is not _RECOVERY_ISSUER_TOKEN
    ):
        raise RecoveryError(
            "restore_path_invalid",
            "caller-selected restore target lacks same-owner recovery authority",
        )
    if restored.caller_selected_target:
        restored_root, fixed_parent, _fixed_parent_identity = (
            _validated_fixed_target_path(restored.root)
        )
        if restored.restore_parent is None or (
            Path(restored.root).absolute() != restored_root
            or Path(restored.restore_parent).absolute() != fixed_parent
        ):
            raise RecoveryError(
                "restore_path_invalid",
                "caller-selected restore target lost its exact plain-root binding",
            )
    else:
        restored_root = restored.root.resolve(strict=True)
    verified_backup = (
        _verify_portable_restore_source(restored.backup, verified_source=verified_source)
        if restored.source_materialization_portable
        else _verify_backup_for_trust_transition(restored.backup)
    )
    if (
        verified_backup.manifest.manifest_sha256
        != restored.backup.manifest.manifest_sha256
    ):
        raise RecoveryError("stale_takeover_plan", "source backup changed")
    manifest = verified_backup.manifest
    if restored.caller_selected_target:
        expected_parent = fixed_parent
    elif restored.source_materialization_portable:
        if restored.restore_parent is None:
            raise RecoveryError(
                "restore_path_invalid",
                "portable restore lost its explicit owning parent",
            )
        expected_parent = Path(restored.restore_parent).resolve(strict=True)
    else:
        source_paths = _workspace_paths_for_backup(
            verified_backup.path,
            manifest.backup_id,
        )
        source_paths.revalidate_physical(require_root=True)
        expected_parent = source_paths.recovery.resolve(strict=True)
    random_name_invalid = (
        not restored.caller_selected_target
        and not restored_root.name.startswith(f"restore-{manifest.backup_id}-")
    )
    if restored_root.parent != expected_parent or random_name_invalid:
        raise RecoveryError(
            "restore_path_invalid", "restore is outside WorkspacePaths.recovery"
        )
    _assert_plain_tree(restored_root, code="restored_workspace_corrupt")
    restorable_inventory = _restorable_inventory(
        manifest.evidence_inventory,
        manifest.deletion_directives,
    )
    protected_files = {"workspace.sqlite3", "deletion_fence.json"}
    protected_files.update(item.logical_path for item in restorable_inventory)
    seal_name = (
        _fixed_target_seal_name(restored_root, manifest)
        if restored.caller_selected_target
        else _READ_ONLY_SEAL
    )
    expected_files = {*protected_files, seal_name}
    actual_files: set[str] = set()
    for path in restored.root.rglob("*"):
        if path.is_symlink():
            raise RecoveryError(
                "restored_workspace_corrupt", "restored root contains a symlink"
            )
        if path.is_file():
            actual_files.add(path.relative_to(restored.root).as_posix())
    if actual_files != expected_files:
        raise RecoveryError(
            "restored_workspace_corrupt", "restored root closure changed"
        )
    db_digest, db_length = _sha256_file(restored_root / "workspace.sqlite3")
    if (db_digest, db_length) != (manifest.sqlite_sha256, manifest.sqlite_length):
        raise RecoveryError(
            "restored_workspace_corrupt", "restored SQLite bytes changed"
        )
    # Exact database bytes above preserve the source's verified SQLite state.
    fence_raw = canonical_json_bytes(
        _deletion_fence_payload(manifest.deletion_directives, manifest.tombstones)
    )
    if (restored_root / "deletion_fence.json").read_bytes() != fence_raw:
        raise RecoveryError("deletion_fence_violation", "deletion fence changed")
    restore_cas = EvidenceCAS(WorkspacePaths.from_root(restored_root))
    for record in restorable_inventory:
        try:
            restore_cas.verify_record(record)
        except EvidenceStoreError as exc:
            raise RecoveryError("restored_workspace_corrupt", str(exc)) from exc
        if (
            stat.S_IMODE(restore_cas.path_for_digest(record.sha256).stat().st_mode)
            & stat.S_IWUSR
        ):
            raise RecoveryError(
                "restored_workspace_corrupt", "restored CAS object is mutable"
            )
    actual_root_digest = _tree_digest(restored_root, protected_files)
    if actual_root_digest != restored.verification.restored_root_sha256:
        raise RecoveryError(
            "restored_workspace_corrupt", "restored root digest changed"
        )
    if _digest(fence_raw) != restored.verification.deletion_fence_sha256:
        raise RecoveryError("deletion_fence_violation", "deletion fence digest changed")
    _verify_read_only_seal(
        restored_root,
        protected_files,
        restored.verification.read_only_seal_sha256,
        seal_name=seal_name,
    )
    actual_reconciliation = _derive_external_reconciliation(
        restored_root / "workspace.sqlite3"
    )
    if actual_reconciliation != restored.reconciliation:
        raise RecoveryError(
            "external_reconciliation_stale",
            "persisted Attempt/reservation state differs from reconciliation",
        )


def _verify_persisted_writer_fence(
    restored: RestoredWorkspace,
    reference: WriterFenceProof,
) -> tuple[int | None, str | None]:
    database = restored.root / "workspace.sqlite3"
    connection = open_snapshot_connection(database)
    try:
        high_watermark = int(
            connection.execute(
                "SELECT COALESCE(MAX(epoch), 0) FROM writer_epoch"
            ).fetchone()[0]
        )
        prior_epoch = high_watermark or None
        prior_owner: str | None = None
        if prior_epoch is not None:
            writer = connection.execute(
                "SELECT * FROM writer_epoch WHERE epoch = ?", (prior_epoch,)
            ).fetchone()
            if writer is None:
                raise RecoveryError(
                    "writer_epoch_ambiguous", "prior writer epoch is absent"
                )
            prior_owner = str(writer["owner"])
            if str(writer["lifecycle"]) == "active":
                raise RecoveryError(
                    "old_writer_ambiguous",
                    "backup still records an active writer; it was not quiesced before backup",
                )
        row = connection.execute(
            "SELECT * FROM evidence_item_revision WHERE evidence_id = ? AND revision = ?",
            (reference.evidence_id, reference.evidence_revision),
        ).fetchone()
        head = connection.execute(
            "SELECT * FROM evidence_item_head WHERE evidence_id = ?",
            (reference.evidence_id,),
        ).fetchone()
        if (
            row is None
            or head is None
            or int(head["revision"]) < reference.evidence_revision
            or str(row["payload_digest"]) != reference.evidence_payload_sha256
            or str(row["subtype"]) != "writer_fence_proof"
            or str(row["canonical_effect"]) != "none"
            or (
                "availability_state" in row.keys()
                and str(row["availability_state"]) != "verified_available"
            )
        ):
            raise RecoveryError(
                "writer_fence_evidence_invalid",
                "writer fence does not resolve to one available persisted Evidence revision",
            )
        scope = _json_value(row["exact_scope_json"], "writer_fence.exact_scope")
        expected_keys = {
            "project_id",
            "root_identity",
            "prior_epoch",
            "prior_owner",
            "old_writer_cannot_write",
            "fence_method",
            "verification_reference",
        }
        metadata = connection.execute(
            "SELECT project_id, root_identity FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if (
            not isinstance(scope, Mapping)
            or set(scope) != expected_keys
            or scope["project_id"] != metadata["project_id"]
            or scope["root_identity"] != metadata["root_identity"]
            or scope["prior_epoch"] != prior_epoch
            or scope["prior_owner"] != prior_owner
            or scope["old_writer_cannot_write"] is not True
            or not isinstance(scope["fence_method"], str)
            or not scope["fence_method"]
            or not isinstance(scope["verification_reference"], str)
            or not scope["verification_reference"]
        ):
            raise RecoveryError(
                "writer_fence_evidence_invalid",
                "persisted writer-fence scope does not bind the prior writer",
            )
        if (
            not connection.execute(
                "SELECT 1 FROM provenance_event WHERE evidence_id = ? AND evidence_revision = ?",
                (reference.evidence_id, reference.evidence_revision),
            ).fetchone()
            or not connection.execute(
                "SELECT 1 FROM independence_disclosure WHERE evidence_id = ? AND evidence_revision = ?",
                (reference.evidence_id, reference.evidence_revision),
            ).fetchone()
        ):
            raise RecoveryError(
                "writer_fence_evidence_invalid",
                "writer-fence Evidence lacks provenance or independence disclosure",
            )
        active_tombstone = connection.execute(
            "SELECT 1 FROM evidence_tombstone t JOIN deletion_directive d "
            "ON d.directive_id = t.directive_id WHERE t.evidence_id = ? "
            "AND t.evidence_revision = ? AND d.lifecycle = 'active'",
            (reference.evidence_id, reference.evidence_revision),
        ).fetchone()
        if active_tombstone:
            raise RecoveryError(
                "writer_fence_evidence_invalid",
                "writer-fence Evidence is security-tombstoned",
            )
    finally:
        connection.close()
    closure_keys = {member.key for member in restored.backup.manifest.closure.members}
    if (
        "evidence",
        reference.evidence_id,
        reference.evidence_revision,
    ) not in closure_keys:
        raise RecoveryError(
            "writer_fence_evidence_invalid",
            "writer-fence Evidence is outside the verified backup closure",
        )
    return prior_epoch, prior_owner


def _restored_prior_writer(
    restored: RestoredWorkspace,
) -> tuple[int | None, str | None]:
    connection = open_snapshot_connection(restored.root / "workspace.sqlite3")
    try:
        high_watermark = int(
            connection.execute(
                "SELECT COALESCE(MAX(epoch), 0) FROM writer_epoch"
            ).fetchone()[0]
        )
        if not high_watermark:
            return None, None
        writer = connection.execute(
            "SELECT * FROM writer_epoch WHERE epoch = ?",
            (high_watermark,),
        ).fetchone()
        if writer is None:
            raise RecoveryError("writer_epoch_ambiguous", "prior writer epoch is absent")
        if str(writer["lifecycle"]) == "active":
            raise RecoveryError(
                "old_writer_ambiguous",
                "checkpoint source still records an active prior writer",
            )
        return high_watermark, str(writer["owner"])
    finally:
        connection.close()


def _require_source_incarnation_fence(
    restored: RestoredWorkspace,
    fence: SourceIncarnationFence,
) -> tuple[int, str]:
    source = _require_source_incarnation_fence_for_backup(
        restored.backup,
        fence,
    )
    prior_epoch, prior_owner = _restored_prior_writer(restored)
    if (prior_epoch, prior_owner) != (
        source.prior_writer_epoch,
        source.prior_writer_owner,
    ):
        raise RecoveryError(
            "writer_epoch_ambiguous",
            "checkpoint source prior writer differs from its manifest binding",
        )
    return source.prior_writer_epoch, source.prior_writer_owner


def _require_source_incarnation_fence_for_backup(
    backup: BackupSet,
    fence: SourceIncarnationFence,
) -> CheckpointSourceBinding:
    """Reverify one current BackupSet fence without assuming restored DB state."""

    if type(fence) is not SourceIncarnationFence:
        raise RecoveryError(
            "source_incarnation_fence_required",
            "current checkpoint backup requires an issued operational fence",
        )
    fence.verify_issued()
    fresh_control = _verify_trusted_source_control(
        backup,
        source_root=fence.physical_write_exclusion_target,
        source_unit=fence.source_unit,
    )
    if (
        fresh_control.control_verification_sha256
        != fence.control_verification_sha256
        or fresh_control.source_unit != fence.source_unit
        or fresh_control.source_unit_user != fence.source_unit_user
    ):
        raise RecoveryError(
            "source_incarnation_fence_stale",
            "source control or physical write exclusion changed after fence issuance",
        )
    source = backup.manifest.checkpoint_source
    if source is None:
        raise RecoveryError(
            "source_incarnation_fence_misplaced",
            "operational source fence cannot replace historical WriterFenceProof",
        )
    expected = (
        source.mission_id,
        source.source_incarnation,
        source.source_root_identity,
        backup.manifest.manifest_sha256,
        source.prior_writer_epoch,
        source.prior_writer_owner,
    )
    actual = (
        fence.mission_id,
        fence.source_incarnation,
        fence.source_root_identity,
        fence.backup_manifest_sha256,
        fence.prior_writer_epoch,
        fence.prior_writer_owner,
    )
    if actual != expected:
        raise RecoveryError(
            "source_incarnation_fence_binding_mismatch",
            "operational fence names another source, Mission, root, backup, or writer",
        )
    return source


def prepare_takeover(
    restored: RestoredWorkspace,
    *,
    fence_proof: WriterFenceProof | None = None,
    source_fence: SourceIncarnationFence | None = None,
) -> TakeoverPlan:
    if restored.lifecycle is not RecoveryLifecycle.VERIFIED_READ_ONLY:
        raise RecoveryError(
            "restore_not_verified_read_only",
            "takeover requires verified read-only restore",
        )
    verify_restored_workspace(restored)
    if not _is_current_takeover_contract(restored.backup.manifest):
        raise RecoveryError(
            "backup_inspection_only",
            "historical-schema backups remain read-only recovery evidence",
        )
    classification = restored.backup.manifest.takeover_classification
    if classification in {
        "inspection_only_active_writer",
        "inspection_only_historical_workspace",
        "inspection_only_historical_migration_bridge",
    }:
        raise RecoveryError(
            "backup_inspection_only",
            "non-direct or active-writer backups remain read-only inspection evidence; "
            "takeover requires a quiesced direct generation and its exact fence",
        )
    if restored.reconciliation.blocks_takeover:
        raise RecoveryError(
            "external_effects_unresolved", "external effects remain unresolved"
        )
    high_watermark = restored.backup.manifest.store.writer_epoch_high_watermark
    checkpoint_source = restored.backup.manifest.checkpoint_source
    if checkpoint_source is not None:
        if fence_proof is not None or source_fence is None:
            raise RecoveryError(
                "source_incarnation_fence_required",
                "current checkpoint backup requires only its operational source fence",
            )
        expected_epoch, prior_owner = _require_source_incarnation_fence(
            restored,
            source_fence,
        )
    else:
        if source_fence is not None or fence_proof is None:
            raise RecoveryError(
                "writer_fence_evidence_required",
                "historical backup requires its persisted WriterFenceProof",
            )
        expected_epoch, prior_owner = _verify_persisted_writer_fence(
            restored,
            fence_proof,
        )
    if expected_epoch != (high_watermark or None):
        raise RecoveryError(
            "writer_epoch_ambiguous", "persisted fence names another epoch"
        )
    target_paths = WorkspacePaths.from_root(restored.root)
    return TakeoverPlan(
        restored_root=restored.root,
        source_root_identity=restored.backup.manifest.store.root_identity,
        target_root_identity=target_paths.root_identity,
        backup_manifest_sha256=restored.backup.manifest.manifest_sha256,
        canonical=restored.backup.manifest.canonical,
        prior_epoch=expected_epoch,
        prior_owner=prior_owner,
        proposed_epoch=high_watermark + 1,
        lifecycle=RecoveryLifecycle.TAKEOVER_PENDING,
        fence_evidence_sha256=(
            fence_proof.evidence_payload_sha256 if fence_proof is not None else None
        ),
        fence_evidence_id=(fence_proof.evidence_id if fence_proof is not None else None),
        fence_evidence_revision=(
            fence_proof.evidence_revision if fence_proof is not None else None
        ),
        restored_root_sha256=restored.verification.restored_root_sha256,
        read_only_seal_sha256=restored.verification.read_only_seal_sha256,
        reconciliation=restored.reconciliation,
        source_incarnation=(
            checkpoint_source.source_incarnation
            if checkpoint_source is not None
            else None
        ),
        source_mission_id=(
            checkpoint_source.mission_id if checkpoint_source is not None else None
        ),
        operational_fence_sha256=(
            source_fence.fence_sha256 if source_fence is not None else None
        ),
        _issuer_token=_RECOVERY_ISSUER_TOKEN,
    )


def confirm_explicit_takeover(
    plan: TakeoverPlan,
    *,
    restored: RestoredWorkspace,
    command_id: str,
    actor: str,
    writer_creation_basis: str = "explicit takeover",
    expected_manifest_sha256: str,
    authority_repo_root: Path | str,
    source_fence: SourceIncarnationFence | None = None,
    installed_release_sha: str | None = None,
) -> TakeoverPermit:
    """Revalidate an exact fence and issue the existing process-local permit."""

    plan.verify_issued()
    if plan.lifecycle is not RecoveryLifecycle.TAKEOVER_PENDING:
        raise RecoveryError("takeover_not_pending", "plan is not takeover_pending")
    if not command_id or not actor or not writer_creation_basis:
        raise RecoveryError(
            "explicit_takeover_required",
            "command ID, writer actor, and creation basis are required",
        )
    principal_owner = stable_principal_owner_binding(attest_current_principal())
    if actor != principal_owner or (
        plan.source_incarnation is not None
        and plan.prior_owner != principal_owner
    ):
        raise RecoveryError(
            "takeover_actor_binding_mismatch",
            "takeover writer actor is not the stable current process principal, "
            "or a current checkpoint source belongs to another prior writer",
        )
    if (
        expected_manifest_sha256 != plan.backup_manifest_sha256
        or restored.backup.manifest.manifest_sha256 != plan.backup_manifest_sha256
        or restored.root != plan.restored_root
    ):
        raise RecoveryError("stale_takeover_plan", "takeover binding changed")
    if (
        WorkspacePaths.from_root(plan.restored_root).root_identity
        != plan.target_root_identity
    ):
        raise RecoveryError("stale_takeover_plan", "takeover target identity changed")
    verify_restored_workspace(restored)
    if not _is_current_takeover_contract(restored.backup.manifest):
        raise RecoveryError(
            "stale_takeover_plan",
            "takeover target is no longer a current Store contract",
        )
    if plan.source_incarnation is not None:
        if source_fence is None:
            raise RecoveryError(
                "source_incarnation_fence_required",
                "takeover confirmation requires the exact issued source fence",
            )
        persisted_epoch, persisted_owner = _require_source_incarnation_fence(
            restored,
            source_fence,
        )
        if (
            source_fence.fence_sha256 != plan.operational_fence_sha256
            or source_fence.source_incarnation != plan.source_incarnation
            or source_fence.mission_id != plan.source_mission_id
        ):
            raise RecoveryError(
                "stale_takeover_plan",
                "operational source fence differs from the prepared plan",
            )
    else:
        if source_fence is not None:
            raise RecoveryError(
                "source_incarnation_fence_misplaced",
                "historical takeover confirmation uses persisted WriterFenceProof",
            )
        assert plan.fence_evidence_id is not None
        assert plan.fence_evidence_revision is not None
        assert plan.fence_evidence_sha256 is not None
        persisted_epoch, persisted_owner = _verify_persisted_writer_fence(
            restored,
            WriterFenceProof(
                evidence_id=plan.fence_evidence_id,
                evidence_revision=plan.fence_evidence_revision,
                evidence_payload_sha256=plan.fence_evidence_sha256,
            ),
        )
    if (
        persisted_epoch != plan.prior_epoch
        or persisted_owner != plan.prior_owner
        or restored.reconciliation != plan.reconciliation
        or restored.reconciliation.blocks_takeover
        or restored.verification.read_only_seal_sha256 != plan.read_only_seal_sha256
    ):
        raise RecoveryError("stale_takeover_plan", "takeover safety bindings changed")
    authority_root = Path(authority_repo_root).resolve(strict=True)
    if installed_release_sha is None:
        verify_live_canonical_binding(plan.canonical, authority_root)
        canonical_verification_kind = "git_head"
        installed_release_bundle_sha256 = None
        installed_release_record_sha256 = None
    else:
        (
            _binding_sha256,
            installed_release_bundle_sha256,
            installed_release_record_sha256,
        ) = _verify_installed_release_canonical_archive(
            plan.canonical,
            authority_root,
            executing_release_sha=installed_release_sha,
        )
        canonical_verification_kind = "installed_release_archive"
    return TakeoverPermit(
        plan=plan,
        command_id=command_id,
        actor=actor,
        writer_creation_basis=writer_creation_basis,
        explicit=True,
        canonical_freshness_sha256=_digest(
            canonical_json_bytes(plan.canonical.authority_payload())
        ),
        canonical_authority_root=authority_root,
        canonical_verification_kind=canonical_verification_kind,
        installed_release_sha=installed_release_sha,
        installed_release_bundle_sha256=installed_release_bundle_sha256,
        installed_release_record_sha256=installed_release_record_sha256,
        _issuer_token=_RECOVERY_ISSUER_TOKEN,
    )


def _takeover_marker_restore_binding(
    paths: WorkspacePaths,
    manifest: BackupManifest,
    *,
    require_original_closure: bool,
) -> tuple[str, str]:
    """Read the one marker and bind it to the deterministic restored closure."""

    marker_path = paths.root / TAKEOVER_UNSEAL_MARKER
    try:
        before = os.lstat(marker_path)
        raw = marker_path.read_bytes()
        after = os.lstat(marker_path)
    except OSError as exc:
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "takeover unseal marker is unavailable",
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or int(before.st_nlink) != 1
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "takeover unseal marker is not one stable regular file",
        )
    _assert_plain_tree(
        paths.root,
        code="takeover_unseal_state_invalid",
        require_independent_files=True,
    )
    protected, root_sha256, _fence_sha256, _evidence_count = (
        _expected_restored_materialization(manifest)
    )
    expected = canonical_json_bytes(
        {
            "format": "research-workspace-read-only-v1",
            "lifecycle": RecoveryLifecycle.VERIFIED_READ_ONLY.value,
            "protected_files": list(protected),
            "protected_root_sha256": root_sha256,
            "writable": False,
            "mission_auto_resume": False,
            "canonical_effect": "none",
        }
    )
    if raw != expected:
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "takeover unseal marker is not the BackupSet-bound restore seal",
        )
    if require_original_closure:
        actual_files = {
            item.relative_to(paths.root).as_posix()
            for item in paths.root.rglob("*")
            if item.is_file()
        }
        if actual_files != {*protected, TAKEOVER_UNSEAL_MARKER} or (
            _tree_digest(paths.root, protected) != root_sha256
        ):
            raise RecoveryError(
                "takeover_unseal_state_invalid",
                "pre-commit marker target differs from the exact restored closure",
            )
    else:
        _verify_committed_takeover_non_database_closure(
            paths,
            manifest,
            marker_required=True,
        )
    return root_sha256, _digest(raw)


def _takeover_regular_file_identity(path: Path) -> tuple[int, int]:
    """Bind one takeover artifact across a path-based SQLite recovery open."""

    try:
        before = os.lstat(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "interrupted takeover artifact is unavailable",
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or int(before.st_nlink) != 1
        or not stat.S_ISREG(opened.st_mode)
        or int(opened.st_nlink) != 1
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "interrupted takeover artifact is not one independent regular file",
        )
    return int(before.st_dev), int(before.st_ino)


def _restore_verified_database_in_place(
    *,
    source: Path,
    target: Path,
    expected_digest: str,
    expected_length: int,
    expected_target_identity: tuple[int, int],
) -> None:
    """Restore exact verified bytes without publishing another target identity."""

    source_descriptor: int | None = None
    target_descriptor: int | None = None
    digest = hashlib.sha256()
    length = 0
    try:
        source_before = os.lstat(source)
        source_descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        source_opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_before.st_mode)
            or int(source_before.st_nlink) != 1
            or not stat.S_ISREG(source_opened.st_mode)
            or int(source_opened.st_nlink) != 1
            or (int(source_before.st_dev), int(source_before.st_ino))
            != (int(source_opened.st_dev), int(source_opened.st_ino))
        ):
            raise RecoveryError(
                "takeover_rollback_recovery_failed",
                "verified BackupSet database identity changed before recovery",
            )
        target_descriptor = os.open(
            target,
            os.O_RDWR
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        target_opened = os.fstat(target_descriptor)
        if (
            not stat.S_ISREG(target_opened.st_mode)
            or int(target_opened.st_nlink) != 1
            or (int(target_opened.st_dev), int(target_opened.st_ino))
            != expected_target_identity
        ):
            raise RecoveryError(
                "takeover_rollback_recovery_failed",
                "interrupted takeover database identity changed before recovery",
            )
        os.ftruncate(target_descriptor, 0)
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(target_descriptor, remaining)
                if written <= 0:
                    raise OSError("database recovery write made no progress")
                remaining = remaining[written:]
        os.fsync(target_descriptor)
        os.lseek(target_descriptor, 0, os.SEEK_SET)
        restored_digest = hashlib.sha256()
        restored_length = 0
        while True:
            chunk = os.read(target_descriptor, 1024 * 1024)
            if not chunk:
                break
            restored_digest.update(chunk)
            restored_length += len(chunk)
        source_after = os.fstat(source_descriptor)
        target_after = os.fstat(target_descriptor)
        if (
            (int(source_after.st_dev), int(source_after.st_ino))
            != (int(source_opened.st_dev), int(source_opened.st_ino))
            or (int(target_after.st_dev), int(target_after.st_ino))
            != expected_target_identity
            or int(target_after.st_size) != expected_length
            or length != expected_length
            or digest.hexdigest() != expected_digest
            or restored_length != expected_length
            or restored_digest.hexdigest() != expected_digest
        ):
            raise RecoveryError(
                "takeover_rollback_recovery_failed",
                "interrupted takeover database recovery bytes differ from the BackupSet",
            )
    except RecoveryError:
        raise
    except OSError as exc:
        raise RecoveryError(
            "takeover_rollback_recovery_failed",
            "interrupted takeover database could not be restored from the BackupSet",
        ) from exc
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)


def _recover_interrupted_takeover_rollback_journal(
    paths: WorkspacePaths,
    backup: BackupSet,
) -> bool:
    """Recover only an exact pre-commit journal from verified BackupSet bytes.

    This runs under the same fixed kernel lease used by activation.  The marker,
    immutable members, database inode, and sole rollback-journal inode are bound
    before any mutable path is consumed.  The journal remains present until the
    database inode contains the byte-identical portable BackupSet database, so an
    interruption during recovery replays the same bounded overwrite.
    """

    manifest = backup.manifest
    marker_path = paths.root / TAKEOVER_UNSEAL_MARKER
    journal_path = paths.database.with_name(paths.database.name + "-journal")
    if not (marker_path.exists() or marker_path.is_symlink()):
        return False
    if not (journal_path.exists() or journal_path.is_symlink()):
        return False
    _takeover_marker_restore_binding(
        paths,
        manifest,
        require_original_closure=False,
    )
    database_identity = _takeover_regular_file_identity(paths.database)
    journal_identity = _takeover_regular_file_identity(journal_path)
    wal_path = paths.database.with_name(paths.database.name + "-wal")
    shared_memory_path = paths.database.with_name(paths.database.name + "-shm")
    if os.path.lexists(wal_path) or os.path.lexists(shared_memory_path):
        raise RecoveryError(
            "takeover_rollback_recovery_failed",
            "interrupted DELETE-mode takeover cannot retain WAL sidecars",
        )
    _remove_windows_write_deny(paths.root, recursive=True)
    _make_owner_writable(paths.root)
    _make_owner_writable(paths.database)
    _make_owner_writable(journal_path)
    _restore_verified_database_in_place(
        source=_contained(backup.path, backup.path / "workspace.sqlite3"),
        target=paths.database,
        expected_digest=manifest.sqlite_sha256,
        expected_length=manifest.sqlite_length,
        expected_target_identity=database_identity,
    )
    if _takeover_regular_file_identity(journal_path) != journal_identity:
        raise RecoveryError(
            "takeover_rollback_recovery_failed",
            "interrupted takeover journal identity changed during recovery",
        )
    if (
        os.path.lexists(wal_path)
        or os.path.lexists(shared_memory_path)
        or _takeover_regular_file_identity(paths.database) != database_identity
        or _sha256_file(paths.database)
        != (manifest.sqlite_sha256, manifest.sqlite_length)
    ):
        raise RecoveryError(
            "takeover_rollback_recovery_failed",
            "interrupted takeover database path differs before journal consumption",
        )
    journal_path.unlink()
    _fsync_directory(paths.root)
    if (
        journal_path.exists()
        or journal_path.is_symlink()
        or os.path.lexists(wal_path)
        or os.path.lexists(shared_memory_path)
        or _takeover_regular_file_identity(paths.database) != database_identity
        or _sha256_file(paths.database)
        != (manifest.sqlite_sha256, manifest.sqlite_length)
    ):
        raise RecoveryError(
            "takeover_rollback_recovery_failed",
            "interrupted takeover did not normalize to the exact BackupSet database",
        )
    return True


@contextmanager
def _takeover_state_snapshot(
    paths: WorkspacePaths,
    *,
    manifest: BackupManifest | None = None,
    permit: TakeoverPermit | None = None,
) -> Iterator[sqlite3.Connection]:
    """Read one takeover target without ignoring a durable WAL."""

    if (manifest is None) == (permit is None):
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "takeover snapshot requires exactly one marker authority",
        )
    marker = paths.root / TAKEOVER_UNSEAL_MARKER
    marker_present = marker.exists() or marker.is_symlink()
    if marker_present:
        if permit is not None:
            _load_takeover_unseal_marker(paths, permit)
        else:
            assert manifest is not None
            _takeover_marker_restore_binding(
                paths,
                manifest,
                require_original_closure=False,
            )
    elif manifest is not None:
        _verify_committed_takeover_non_database_closure(
            paths,
            manifest,
            marker_required=False,
        )
    database_identity = _takeover_regular_file_identity(paths.database)
    journal = paths.database.with_name(paths.database.name + "-journal")
    wal = paths.database.with_name(paths.database.name + "-wal")
    shared_memory = paths.database.with_name(paths.database.name + "-shm")
    if os.path.lexists(journal):
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "takeover state snapshot cannot ignore a rollback journal",
        )
    wal_present = os.path.lexists(wal)
    shared_memory_present = os.path.lexists(shared_memory)
    wal_identity: tuple[int, int] | None = None
    shared_memory_identity: tuple[int, int] | None = None
    temporarily_writable = False
    connection: sqlite3.Connection | None = None
    try:
        if shared_memory_present:
            shared_memory_identity = _takeover_regular_file_identity(shared_memory)
        if not wal_present:
            connection = open_immutable_snapshot_connection(paths.database)
        else:
            wal_identity = _takeover_regular_file_identity(wal)
            if not shared_memory_present and marker_present:
                temporarily_writable = True
                _remove_windows_write_deny(paths.root, recursive=True)
                _make_owner_writable(paths.root)
                _make_owner_writable(paths.database)
                _make_owner_writable(wal)
            connection = open_snapshot_connection(paths.database)
        yield connection
        connection.close()
        connection = None
        if _takeover_regular_file_identity(paths.database) != database_identity:
            raise RecoveryError(
                "takeover_unseal_state_invalid",
                "takeover database identity changed during snapshot read",
            )
        if wal_identity is None:
            if os.path.lexists(wal):
                raise RecoveryError(
                    "takeover_unseal_state_invalid",
                    "takeover WAL appeared during immutable snapshot read",
                )
            if shared_memory_identity is None:
                if os.path.lexists(shared_memory):
                    raise RecoveryError(
                        "takeover_unseal_state_invalid",
                        "takeover WAL-index appeared during immutable snapshot read",
                    )
            elif (
                _takeover_regular_file_identity(shared_memory)
                != shared_memory_identity
            ):
                raise RecoveryError(
                    "takeover_unseal_state_invalid",
                    "takeover WAL-index identity changed during immutable snapshot read",
                )
        else:
            if _takeover_regular_file_identity(wal) != wal_identity:
                raise RecoveryError(
                    "takeover_unseal_state_invalid",
                    "takeover WAL identity changed during snapshot read",
                )
            if shared_memory_identity is None:
                if os.path.lexists(shared_memory):
                    _takeover_regular_file_identity(shared_memory)
            elif (
                _takeover_regular_file_identity(shared_memory)
                != shared_memory_identity
            ):
                raise RecoveryError(
                    "takeover_unseal_state_invalid",
                    "takeover WAL-index identity changed during snapshot read",
                )
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                try:
                    connection.close()
                except BaseException:
                    pass
        if temporarily_writable:
            _strictly_protect_closed_tree_or_force(paths.root)


def _verify_committed_takeover_non_database_closure(
    paths: WorkspacePaths,
    manifest: BackupManifest,
    *,
    marker_required: bool,
) -> None:
    """Verify every immutable restored member untouched by the writer commit."""

    restorable = _restorable_inventory(
        manifest.evidence_inventory,
        manifest.deletion_directives,
    )
    fence_raw = canonical_json_bytes(
        _deletion_fence_payload(manifest.deletion_directives, manifest.tombstones)
    )
    expected = {
        "deletion_fence.json": (_digest(fence_raw), len(fence_raw)),
        **{
            item.logical_path: (item.sha256, item.length)
            for item in restorable
        },
    }
    required_files = {"workspace.sqlite3", *expected}
    if marker_required:
        required_files.add(TAKEOVER_UNSEAL_MARKER)
    permitted_sidecars = {
        "workspace.sqlite3-wal",
        "workspace.sqlite3-shm",
        "workspace.sqlite3-journal",
    }
    actual_files = {
        item.relative_to(paths.root).as_posix()
        for item in paths.root.rglob("*")
        if item.is_file()
    }
    if not required_files.issubset(actual_files) or (
        actual_files - required_files - permitted_sidecars
    ):
        raise RecoveryError(
            "takeover_completion_invalid",
            "committed takeover target has missing or unbound files",
        )
    for relative, expected_binding in expected.items():
        if _sha256_file(_contained(paths.root, paths.root / relative)) != expected_binding:
            raise RecoveryError(
                "takeover_completion_invalid",
                "committed takeover changed an immutable restored member",
            )


def _committed_takeover_restore_binding(
    paths: WorkspacePaths,
    manifest: BackupManifest,
) -> tuple[str, str]:
    """Read only the two historical restore digests from a committed writer row."""

    with _takeover_state_snapshot(paths, manifest=manifest) as connection:
        metadata = connection.execute(
            "SELECT current_writer_epoch FROM workspace_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None or metadata["current_writer_epoch"] is None:
            raise RecoveryError(
                "takeover_completion_invalid",
                "target has no committed successor writer",
            )
        writer = connection.execute(
            "SELECT takeover_evidence_json FROM writer_epoch WHERE epoch = ?",
            (int(metadata["current_writer_epoch"]),),
        ).fetchone()
        if writer is None or writer["takeover_evidence_json"] is None:
            raise RecoveryError(
                "takeover_completion_invalid",
                "successor writer lacks takeover evidence",
            )
        try:
            evidence = loads_strict_json_object(
                str(writer["takeover_evidence_json"]).encode("utf-8")
            )
        except Exception as exc:
            raise RecoveryError(
                "takeover_completion_invalid",
                "successor writer takeover evidence is invalid",
            ) from exc
    root_sha256 = evidence.get("restored_root_sha256")
    seal_sha256 = evidence.get("read_only_seal_sha256")
    if (
        not isinstance(root_sha256, str)
        or not isinstance(seal_sha256, str)
        or SHA256_RE.fullmatch(root_sha256) is None
        or SHA256_RE.fullmatch(seal_sha256) is None
    ):
        raise RecoveryError(
            "takeover_completion_invalid",
            "successor writer lacks exact restored-closure bindings",
        )
    protected, expected_root, _fence_sha256, _evidence_count = (
        _expected_restored_materialization(manifest)
    )
    expected_seal_sha256 = _digest(
        canonical_json_bytes(
            {
                "format": "research-workspace-read-only-v1",
                "lifecycle": RecoveryLifecycle.VERIFIED_READ_ONLY.value,
                "protected_files": list(protected),
                "protected_root_sha256": expected_root,
                "writable": False,
                "mission_auto_resume": False,
                "canonical_effect": "none",
            }
        )
    )
    if (root_sha256, seal_sha256) != (expected_root, expected_seal_sha256):
        raise RecoveryError(
            "takeover_completion_invalid",
            "successor writer restore bindings differ from the portable BackupSet",
        )
    _verify_committed_takeover_non_database_closure(
        paths,
        manifest,
        marker_required=False,
    )
    return root_sha256, seal_sha256


@contextmanager
def _takeover_target_operation(
    paths: WorkspacePaths,
    *,
    project_id: str,
) -> Iterator[None]:
    """Use the Store's one kernel exclusion while reconstructing takeover authority."""

    gate = WorkspaceStore(paths, project_id)
    activation_token = gate._takeover_activation.set(True)
    try:
        with gate._checkpoint_source_operation():
            yield
    finally:
        gate._takeover_activation.reset(activation_token)


def _takeover_target_is_committed(
    paths: WorkspacePaths,
    manifest: BackupManifest,
) -> bool:
    """Classify only the manifest-bound pre-state or active successor state."""

    journal = paths.database.with_name(paths.database.name + "-journal")
    if journal.exists() or journal.is_symlink():
        return False
    try:
        with _takeover_state_snapshot(paths, manifest=manifest) as connection:
            metadata = connection.execute(
                "SELECT root_identity, current_writer_epoch, lifecycle "
                "FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "takeover target state cannot be classified safely",
        ) from exc
    if metadata is None:
        raise RecoveryError(
            "takeover_unseal_state_invalid", "takeover target has no Store metadata"
        )
    if (
        str(metadata["root_identity"]) == paths.root_identity
        and metadata["current_writer_epoch"] is not None
        and str(metadata["lifecycle"]) == RecoveryLifecycle.ACTIVE.value
    ):
        return True
    if (
        str(metadata["root_identity"]) == manifest.store.root_identity
        and metadata["current_writer_epoch"] is None
        and str(metadata["lifecycle"]) == RecoveryLifecycle.QUIESCED.value
    ):
        return False
    raise RecoveryError(
        "takeover_unseal_state_invalid",
        "takeover target is neither the BackupSet pre-state nor an active successor",
    )


def _reissue_committed_checkpoint_source_takeover_permit(
    *,
    backup: BackupSet,
    target_root: Path | str,
    command_id: str,
    installed_release_root: Path | str,
    executing_release_sha: str,
) -> tuple[WorkspaceStore, TakeoverPermit]:
    """Reissue exact committed authority without the retired source incarnation."""

    verified = _verify_portable_backup_for_trust_transition(backup)
    source = verified.manifest.checkpoint_source
    if source is None:
        raise RecoveryError(
            "source_incarnation_fence_unavailable",
            "committed current-source replay requires checkpoint-source identity",
        )
    actor = stable_principal_owner_binding(attest_current_principal())
    if actor != source.prior_writer_owner:
        raise RecoveryError(
            "takeover_actor_binding_mismatch",
            "active successor is not owned by the checkpoint source principal",
        )
    paths = WorkspacePaths.from_root(Path(target_root).resolve(strict=True))
    paths.revalidate_physical(require_root=True, require_database=True)
    with _takeover_target_operation(
        paths,
        project_id=verified.manifest.store.project_id,
    ):
        _assert_plain_tree(
            paths.root,
            code="takeover_completion_invalid",
            require_independent_files=True,
        )
        if not _takeover_target_is_committed(paths, verified.manifest):
            raise RecoveryError(
                "source_incarnation_fence_required",
                "pre-commit takeover replay still requires the live source fence",
            )
        marker = paths.root / TAKEOVER_UNSEAL_MARKER
        if marker.exists() or marker.is_symlink():
            restored_root_sha256, read_only_seal_sha256 = (
                _takeover_marker_restore_binding(
                    paths,
                    verified.manifest,
                    require_original_closure=False,
                )
            )
        else:
            restored_root_sha256, read_only_seal_sha256 = (
                _committed_takeover_restore_binding(paths, verified.manifest)
            )
        (
            _binding_sha256,
            release_bundle_sha256,
            release_record_sha256,
        ) = _verify_installed_release_canonical_archive(
            verified.manifest.canonical,
            installed_release_root,
            executing_release_sha=executing_release_sha,
        )
        with _takeover_state_snapshot(
            paths,
            manifest=verified.manifest,
        ) as connection:
            reconciliation = _derive_external_reconciliation(
                paths.database,
                _connection_override=connection,
            )
            metadata = connection.execute(
                "SELECT current_writer_epoch FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
            tail = _validated_writer_epoch_tail(
                connection,
                project_id=verified.manifest.store.project_id,
            )
        if (
            metadata is None
            or metadata["current_writer_epoch"] != source.prior_writer_epoch + 1
            or tail is None
            or int(tail["epoch"]) != source.prior_writer_epoch + 1
            or str(tail["owner"]) != actor
            or str(tail["creation_basis"]) != _PORTABLE_TAKEOVER_CREATION_BASIS
            or str(tail["lifecycle"]) != RecoveryLifecycle.ACTIVE.value
        ):
            raise RecoveryError(
                "takeover_completion_invalid",
                "active successor writer does not match the portable source identity",
            )
        try:
            persisted_evidence = loads_strict_json_object(
                str(tail["takeover_evidence_json"]).encode("utf-8")
            )
        except Exception as exc:
            raise RecoveryError(
                "takeover_completion_invalid",
                "active successor writer evidence is invalid",
            ) from exc
        operational_fence_sha256 = persisted_evidence.get(
            "operational_fence_sha256"
        )
        if (
            not isinstance(operational_fence_sha256, str)
            or SHA256_RE.fullmatch(operational_fence_sha256) is None
        ):
            raise RecoveryError(
                "takeover_completion_invalid",
                "active successor lacks its original source-fence binding",
            )
        plan = TakeoverPlan(
            restored_root=paths.root,
            source_root_identity=verified.manifest.store.root_identity,
            target_root_identity=paths.root_identity,
            backup_manifest_sha256=verified.manifest.manifest_sha256,
            canonical=verified.manifest.canonical,
            prior_epoch=source.prior_writer_epoch,
            prior_owner=source.prior_writer_owner,
            proposed_epoch=source.prior_writer_epoch + 1,
            lifecycle=RecoveryLifecycle.TAKEOVER_PENDING,
            fence_evidence_sha256=None,
            fence_evidence_id=None,
            fence_evidence_revision=None,
            restored_root_sha256=restored_root_sha256,
            read_only_seal_sha256=read_only_seal_sha256,
            reconciliation=reconciliation,
            source_incarnation=source.source_incarnation,
            source_mission_id=source.mission_id,
            operational_fence_sha256=operational_fence_sha256,
            _issuer_token=_RECOVERY_ISSUER_TOKEN,
        )
        authority_root = Path(installed_release_root).resolve(strict=True)
        permit = TakeoverPermit(
            plan=plan,
            command_id=command_id,
            actor=actor,
            writer_creation_basis=_PORTABLE_TAKEOVER_CREATION_BASIS,
            explicit=True,
            canonical_freshness_sha256=_digest(
                canonical_json_bytes(plan.canonical.authority_payload())
            ),
            canonical_authority_root=authority_root,
            canonical_verification_kind="installed_release_archive",
            installed_release_sha=executing_release_sha,
            installed_release_bundle_sha256=release_bundle_sha256,
            installed_release_record_sha256=release_record_sha256,
            _issuer_token=_RECOVERY_ISSUER_TOKEN,
        )
        if persisted_evidence != deep_thaw(
            WorkspaceStore._takeover_evidence_for_permit(permit)
        ):
            raise RecoveryError(
                "takeover_completion_invalid",
                "active successor writer evidence differs from this exact replay",
            )
        store = WorkspaceStore._open_takeover_target_under_operation(
            paths,
            permit,
            expected_project_id=verified.manifest.store.project_id,
        )
    return store, permit


def _reissue_checkpoint_source_takeover_permit(
    *,
    backup: BackupSet,
    target_root: Path | str,
    command_id: str,
    installed_release_root: Path | str,
    executing_release_sha: str,
    source_fence: SourceIncarnationFence,
) -> tuple[WorkspaceStore, TakeoverPermit]:
    """Reissue an exact pre-commit marker permit from current owner readbacks."""

    verified = _verify_portable_backup_for_trust_transition(backup)
    source = _require_source_incarnation_fence_for_backup(verified, source_fence)
    actor = stable_principal_owner_binding(attest_current_principal())
    if actor != source.prior_writer_owner:
        raise RecoveryError(
            "takeover_actor_binding_mismatch",
            "successor writer is not the checkpoint source prior principal",
        )
    paths = WorkspacePaths.from_root(Path(target_root).resolve(strict=True))
    paths.revalidate_physical(require_root=True, require_database=True)
    with _takeover_target_operation(
        paths,
        project_id=verified.manifest.store.project_id,
    ):
        _assert_plain_tree(
            paths.root,
            code="takeover_unseal_state_invalid",
            require_independent_files=True,
        )
        try:
            recovered_rollback = _recover_interrupted_takeover_rollback_journal(
                paths,
                verified,
            )
        except BaseException as recovery_error:
            try:
                _strictly_protect_closed_tree_or_force(paths.root)
            except BaseException as protection_error:
                raise protection_error from recovery_error
            raise
        if recovered_rollback:
            _strictly_protect_closed_tree_or_force(paths.root)
        marker = paths.root / TAKEOVER_UNSEAL_MARKER
        if not marker.exists() or marker.is_symlink():
            raise RecoveryError(
                "takeover_unseal_state_invalid",
                "pre-commit takeover replay requires its exact unseal marker",
            )
        with _takeover_state_snapshot(
            paths,
            manifest=verified.manifest,
        ) as state_connection:
            metadata = state_connection.execute(
                "SELECT root_identity, current_writer_epoch, lifecycle "
                "FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
        if metadata is None:
            raise RecoveryError(
                "takeover_unseal_state_invalid",
                "marker target has no Store metadata",
            )
        precommit = (
            str(metadata["root_identity"]) == verified.manifest.store.root_identity
            and metadata["current_writer_epoch"] is None
            and str(metadata["lifecycle"]) == RecoveryLifecycle.QUIESCED.value
        )
        if not precommit:
            raise RecoveryError(
                "takeover_completion_invalid",
                "live source fence cannot authorize an already-committed replay",
            )
        restored_root_sha256, read_only_seal_sha256 = (
            _takeover_marker_restore_binding(
                paths,
                verified.manifest,
                require_original_closure=True,
            )
        )
        reconciliation = _derive_external_reconciliation(paths.database)
        (
            _binding_sha256,
            release_bundle_sha256,
            release_record_sha256,
        ) = _verify_installed_release_canonical_archive(
            verified.manifest.canonical,
            installed_release_root,
            executing_release_sha=executing_release_sha,
        )
        plan = TakeoverPlan(
            restored_root=paths.root,
            source_root_identity=verified.manifest.store.root_identity,
            target_root_identity=paths.root_identity,
            backup_manifest_sha256=verified.manifest.manifest_sha256,
            canonical=verified.manifest.canonical,
            prior_epoch=source.prior_writer_epoch,
            prior_owner=source.prior_writer_owner,
            proposed_epoch=source.prior_writer_epoch + 1,
            lifecycle=RecoveryLifecycle.TAKEOVER_PENDING,
            fence_evidence_sha256=None,
            fence_evidence_id=None,
            fence_evidence_revision=None,
            restored_root_sha256=restored_root_sha256,
            read_only_seal_sha256=read_only_seal_sha256,
            reconciliation=reconciliation,
            source_incarnation=source.source_incarnation,
            source_mission_id=source.mission_id,
            operational_fence_sha256=source_fence.fence_sha256,
            _issuer_token=_RECOVERY_ISSUER_TOKEN,
        )
        authority_root = Path(installed_release_root).resolve(strict=True)
        permit = TakeoverPermit(
            plan=plan,
            command_id=command_id,
            actor=actor,
            writer_creation_basis=_PORTABLE_TAKEOVER_CREATION_BASIS,
            explicit=True,
            canonical_freshness_sha256=_digest(
                canonical_json_bytes(plan.canonical.authority_payload())
            ),
            canonical_authority_root=authority_root,
            canonical_verification_kind="installed_release_archive",
            installed_release_sha=executing_release_sha,
            installed_release_bundle_sha256=release_bundle_sha256,
            installed_release_record_sha256=release_record_sha256,
            _issuer_token=_RECOVERY_ISSUER_TOKEN,
        )
        if recovered_rollback:
            _reseal_restored_after_failed_takeover(paths, permit)
        store = WorkspaceStore._open_takeover_target_under_operation(
            paths,
            permit,
            expected_project_id=verified.manifest.store.project_id,
        )
    return store, permit


def activate_portable_checkpoint_source_takeover_from_owner(
    *,
    backup_directory: Path | str,
    source_root: Path | str | None = None,
    source_unit: str,
    target_root: Path | str,
    installed_release_root: Path | str,
    executing_release_sha: str,
    expected_manifest_sha256: str,
    mission_id: str,
    command_id: str,
) -> Mapping[str, Any]:
    """Restore a checkpoint source with explicit systemd custody and no auto-resume."""

    _validate_source_unit(source_unit)

    if not mission_id or not command_id:
        raise RecoveryError(
            "explicit_takeover_required",
            "Mission identity and takeover command identity are required",
        )
    target_path, target_parent, target_parent_identity = _validated_fixed_target_path(
        target_root
    )
    backup = _verify_backup_tree_contents(
        backup_directory,
        enforce_source_location=False,
    )
    verified = _verify_portable_backup_for_trust_transition(backup)
    checkpoint_source = verified.manifest.checkpoint_source
    if (
        verified.manifest.manifest_sha256 != expected_manifest_sha256
        or checkpoint_source is None
        or checkpoint_source.mission_id != mission_id
    ):
        raise RecoveryError(
            "stale_takeover_plan",
            "portable BackupSet differs from the exact owner request",
        )
    (
        release_binding_sha256,
        release_bundle_sha256,
        release_record_sha256,
    ) = _verify_installed_release_canonical_archive(
        verified.manifest.canonical,
        installed_release_root,
        executing_release_sha=executing_release_sha,
    )
    actor = stable_principal_owner_binding(attest_current_principal())

    def require_live_source_fence() -> SourceIncarnationFence:
        if source_root is None:
            raise RecoveryError(
                "source_incarnation_fence_required",
                "new or pre-commit takeover requires the live protected source root",
            )
        source_path = Path(source_root)
        if not source_path.is_absolute():
            raise RecoveryError(
                "restore_path_invalid", "source root must be one absolute path"
            )
        source_path = source_path.absolute()
        verification = _verify_trusted_source_control(
            verified,
            source_root=source_path,
            source_unit=source_unit,
        )
        authenticated_source = Path(
            verification.physical_write_exclusion_target
        )
        if (
            authenticated_source == target_path
            or authenticated_source in target_path.parents
            or target_path in authenticated_source.parents
        ):
            raise RecoveryError(
                "restore_path_invalid", "source and successor roots must be disjoint"
            )
        return _issue_source_incarnation_fence(
            verified,
            trusted_control_verification=verification,
        )

    disposition: str
    _revalidate_fixed_target_parent(
        target_path,
        target_parent,
        target_parent_identity,
    )
    try:
        target_entry = os.lstat(target_path)
    except FileNotFoundError:
        target_entry = None
    if target_entry is None:
        source_fence = require_live_source_fence()
        restored = prepare_portable_fixed_target_restore(
            backup_directory=verified.path,
            target_root=target_path,
            installed_release_root=installed_release_root,
            executing_release_sha=executing_release_sha,
        )
        plan = prepare_takeover(restored, source_fence=source_fence)
        permit = confirm_explicit_takeover(
            plan,
            restored=restored,
            command_id=command_id,
            actor=actor,
            writer_creation_basis=_PORTABLE_TAKEOVER_CREATION_BASIS,
            expected_manifest_sha256=expected_manifest_sha256,
            authority_repo_root=installed_release_root,
            source_fence=source_fence,
            installed_release_sha=executing_release_sha,
        )
        store = WorkspaceStore.open_takeover_target(
            WorkspacePaths.from_root(restored.root),
            permit,
            expected_project_id=verified.manifest.store.project_id,
        )
        disposition = "new_fixed_target"
    else:
        if not stat.S_ISDIR(target_entry.st_mode) or _is_reparse_point(target_path):
            raise RecoveryError(
                "restore_path_invalid",
                "fixed takeover target is not a plain directory",
            )
        _revalidate_fixed_target_parent(
            target_path,
            target_parent,
            target_parent_identity,
        )
        _reconcile_fixed_target_restore_owner_state(
            backup=verified,
            target_root=target_path,
            parent=target_parent,
            parent_identity=target_parent_identity,
            executing_release_sha=executing_release_sha,
            release_binding_sha256=release_binding_sha256,
            release_bundle_sha256=release_bundle_sha256,
            release_record_sha256=release_record_sha256,
        )
        paths = WorkspacePaths.from_root(target_path)
        marker = paths.root / TAKEOVER_UNSEAL_MARKER
        seal = paths.root / "read_only_seal.json"
        if (
            not seal.is_symlink()
            and seal.is_file()
            and not marker.is_symlink()
            and not marker.exists()
        ):
            source_fence = require_live_source_fence()
            restored = _inspect_portable_fixed_target_restore(
                backup=verified,
                target_root=paths.root,
            )
            _verify_installed_release_canonical_archive(
                restored.backup.manifest.canonical,
                installed_release_root,
                executing_release_sha=executing_release_sha,
            )
            plan = prepare_takeover(restored, source_fence=source_fence)
            permit = confirm_explicit_takeover(
                plan,
                restored=restored,
                command_id=command_id,
                actor=actor,
                writer_creation_basis=_PORTABLE_TAKEOVER_CREATION_BASIS,
                expected_manifest_sha256=expected_manifest_sha256,
                authority_repo_root=installed_release_root,
                source_fence=source_fence,
                installed_release_sha=executing_release_sha,
            )
            store = WorkspaceStore.open_takeover_target(
                paths,
                permit,
                expected_project_id=verified.manifest.store.project_id,
            )
            disposition = "sealed_target_replay"
        else:
            marker_was_present = marker.is_symlink() or marker.exists()
            with _takeover_target_operation(
                paths,
                project_id=verified.manifest.store.project_id,
            ):
                target_is_committed = _takeover_target_is_committed(
                    paths,
                    verified.manifest,
                )
            if target_is_committed:
                store, permit = (
                    _reissue_committed_checkpoint_source_takeover_permit(
                        backup=verified,
                        target_root=paths.root,
                        command_id=command_id,
                        installed_release_root=installed_release_root,
                        executing_release_sha=executing_release_sha,
                    )
                )
                disposition = (
                    "incomplete_activation_replay"
                    if marker_was_present
                    else "active_replay"
                )
            else:
                source_fence = require_live_source_fence()
                store, permit = _reissue_checkpoint_source_takeover_permit(
                    backup=verified,
                    target_root=paths.root,
                    command_id=command_id,
                    installed_release_root=installed_release_root,
                    executing_release_sha=executing_release_sha,
                    source_fence=source_fence,
                )
                disposition = "incomplete_activation_replay"
    cut = verified.manifest.store
    _revalidate_fixed_target_parent(
        target_path,
        target_parent,
        target_parent_identity,
    )
    try:
        activation_entry = os.lstat(target_path)
    except OSError as exc:
        raise RecoveryError(
            "restore_path_invalid",
            "fixed takeover target became unavailable before activation",
        ) from exc
    if (
        not stat.S_ISDIR(activation_entry.st_mode)
        or _is_reparse_point(target_path)
        or WorkspacePaths.from_root(target_path).root_identity != store.paths.root_identity
    ):
        raise RecoveryError(
            "restore_path_invalid",
            "fixed takeover target identity changed before activation",
        )
    lease = store.activate_takeover(
        permit,
        creation_basis=_PORTABLE_TAKEOVER_CREATION_BASIS,
        expected_project_commit=cut.project_commit_id,
        expected_root_digest=cut.project_root_digest,
        expected_canonical_authority_digest=cut.canonical_authority_digest,
    )
    return deep_freeze(
        {
            "schema_version": _PORTABLE_TAKEOVER_RESULT_SCHEMA,
            "status": "active",
            "disposition": disposition,
            "project_id": cut.project_id,
            "mission_id": mission_id,
            "backup_id": verified.manifest.backup_id,
            "backup_manifest_sha256": verified.manifest.manifest_sha256,
            "source_incarnation": checkpoint_source.source_incarnation,
            "source_root_identity": checkpoint_source.source_root_identity,
            "target_root_identity": store.paths.root_identity,
            "project_commit": cut.project_commit_id,
            "root_digest": cut.project_root_digest,
            "transition_head": cut.transition_head,
            "writer_epoch": lease.epoch,
            "writer_owner": lease.owner,
            "command_id": command_id,
        }
    )


def _unseal_restored_for_takeover(
    paths: WorkspacePaths, permit: TakeoverPermit
) -> None:
    """Consume physical read-only protection only for an exact takeover permit.

    The sole writer calls this package-private helper immediately before its
    first mutable SQLite connection.  It does not activate the writer; failure
    leaves the restored tree sealed.
    """

    permit.verify_issued()
    if not (
        permit.__class__.__name__ == "TakeoverPermit"
        and permit.__class__.__module__.endswith(".workspace_recovery")
        and permit.explicit
        and permit.requires_writer_transaction
        and not permit.mission_auto_resume
        and permit.canonical_effect == "none"
    ):
        raise RecoveryError(
            "takeover_unseal_forbidden", "unseal requires an exact permit"
        )
    plan = permit.plan
    paths.revalidate_physical(require_root=True, require_database=True)
    _assert_plain_tree(
        paths.root,
        code="takeover_unseal_forbidden",
        require_independent_files=True,
    )
    if (
        paths.root != Path(plan.restored_root).resolve(strict=True)
        or paths.root_identity != plan.target_root_identity
        or plan.lifecycle is not RecoveryLifecycle.TAKEOVER_PENDING
        or plan.reconciliation.blocks_takeover
    ):
        raise RecoveryError(
            "takeover_unseal_forbidden", "permit target is stale or blocked"
        )
    seal_path = paths.root / "read_only_seal.json"
    try:
        seal = loads_strict_json_object(seal_path.read_bytes())
    except Exception as exc:
        raise RecoveryError(
            "takeover_unseal_forbidden", f"read-only seal is invalid: {exc}"
        ) from exc
    if set(seal) != {
        "format",
        "lifecycle",
        "protected_files",
        "protected_root_sha256",
        "writable",
        "mission_auto_resume",
        "canonical_effect",
    }:
        raise RecoveryError("takeover_unseal_forbidden", "read-only seal is not closed")
    protected = seal["protected_files"]
    if (
        seal["format"] != "research-workspace-read-only-v1"
        or seal["lifecycle"] != RecoveryLifecycle.VERIFIED_READ_ONLY.value
        or seal["writable"] is not False
        or seal["mission_auto_resume"] is not False
        or seal["canonical_effect"] != "none"
        or not isinstance(protected, list)
        or any(not isinstance(item, str) for item in protected)
        or len(set(protected)) != len(protected)
        or _digest(seal_path.read_bytes()) != plan.read_only_seal_sha256
        or seal["protected_root_sha256"] != plan.restored_root_sha256
        or _tree_digest(paths.root, protected) != plan.restored_root_sha256
    ):
        raise RecoveryError(
            "takeover_unseal_forbidden", "read-only seal binding is stale"
        )
    expected_files = {*protected, "read_only_seal.json"}
    actual_files = {
        item.relative_to(paths.root).as_posix()
        for item in paths.root.rglob("*")
        if item.is_file()
    }
    if actual_files != expected_files:
        raise RecoveryError(
            "takeover_unseal_forbidden", "restored closure changed before unseal"
        )
    _verify_read_only_seal(paths.root, protected, plan.read_only_seal_sha256)
    if _derive_external_reconciliation(paths.database) != plan.reconciliation:
        raise RecoveryError(
            "takeover_unseal_forbidden", "external reconciliation changed"
        )
    connection = open_snapshot_connection(paths.database)
    try:
        metadata = connection.execute(
            "SELECT root_identity, canonical_authority_digest, application_version, "
            "schema_version, operating_mode, root_digest_version FROM workspace_metadata "
            "WHERE singleton = 1"
        ).fetchone()
        if (
            metadata is None
            or int(connection.execute("PRAGMA user_version").fetchone()[0])
            != int(metadata["schema_version"])
            or str(metadata["application_version"]) != APPLICATION_VERSION
            or not is_direct_mission_generation(
                int(metadata["schema_version"]), int(metadata["root_digest_version"]),
                str(metadata["operating_mode"]),
            )
            or str(metadata["root_identity"]) != plan.source_root_identity
            or str(metadata["canonical_authority_digest"])
            != _digest(canonical_json_bytes(plan.canonical.authority_payload()))
        ):
            raise RecoveryError(
                "takeover_unseal_forbidden", "restored authority binding changed"
            )
    finally:
        connection.close()
    marker_path = paths.root / TAKEOVER_UNSEAL_MARKER
    if marker_path.exists():
        raise RecoveryError(
            "takeover_unseal_forbidden", "takeover unseal marker already exists"
        )
    try:
        _remove_windows_write_deny(paths.root, recursive=True)
        _make_owner_writable(paths.root)
        seal_path.replace(marker_path)
        for directory in sorted(
            (item for item in paths.root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
        ):
            _make_owner_writable(directory)
        _make_owner_writable(paths.database)
        _fsync_directory(paths.root)
    except BaseException as unseal_error:
        restoration_error: BaseException | None = None
        try:
            if marker_path.exists() and not seal_path.exists():
                marker_path.replace(seal_path)
        except BaseException as cleanup_error:
            restoration_error = cleanup_error
        try:
            _strictly_protect_closed_tree_or_force(paths.root)
        except BaseException as protection_error:
            if restoration_error is not None:
                protection_error.add_note(
                    "takeover seal restoration also raised "
                    f"{type(restoration_error).__name__}"
                )
            raise protection_error from unseal_error
        if restoration_error is not None:
            raise restoration_error from unseal_error
        raise


def _protect_committed_takeover_after_failure(
    paths: WorkspacePaths,
    permit: TakeoverPermit,
) -> None:
    """Strictly protect a still-marked committed takeover before lease release."""

    try:
        paths.revalidate_physical(require_root=True, require_database=True)
        _load_takeover_unseal_marker(paths, permit)
    except BaseException as binding_error:
        try:
            _force_owner_read_only_tree(paths.root)
        except BaseException as fallback_error:
            binding_error.add_note(
                "best-effort read-only fallback also raised "
                f"{type(fallback_error).__name__}"
            )
        raise
    _strictly_protect_closed_tree_or_force(paths.root)


def _resume_takeover_unseal_permissions(
    paths: WorkspacePaths,
    permit: TakeoverPermit,
) -> None:
    """Restore only the mutable directory/DB surface for exact marker replay."""

    _assert_plain_tree(
        paths.root,
        code="takeover_unseal_state_invalid",
        require_independent_files=True,
    )
    marker_path, _marker = _load_takeover_unseal_marker(paths, permit)
    _remove_windows_write_deny(paths.root, recursive=True)
    _make_owner_writable(paths.root)
    for directory in sorted(
        (item for item in paths.root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
    ):
        _make_owner_writable(directory)
    _make_owner_writable(paths.database)
    for suffix in ("-wal", "-shm"):
        sidecar = paths.database.with_name(paths.database.name + suffix)
        if sidecar.exists() and not sidecar.is_symlink() and sidecar.is_file():
            _make_owner_writable(sidecar)
    _make_owner_writable(marker_path)
    _fsync_directory(paths.root)


def _load_takeover_unseal_marker(
    paths: WorkspacePaths,
    permit: TakeoverPermit,
) -> tuple[Path, Mapping[str, Any]]:
    marker_path = paths.root / TAKEOVER_UNSEAL_MARKER
    if marker_path.is_symlink() or not marker_path.is_file():
        raise RecoveryError(
            "takeover_unseal_state_invalid", "takeover unseal marker is absent"
        )
    try:
        raw = marker_path.read_bytes()
        marker = loads_strict_json_object(raw)
    except Exception as exc:
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            f"takeover unseal marker is invalid: {exc}",
        ) from exc
    if (
        _digest(raw) != permit.plan.read_only_seal_sha256
        or set(marker)
        != {
            "format",
            "lifecycle",
            "protected_files",
            "protected_root_sha256",
            "writable",
            "mission_auto_resume",
            "canonical_effect",
        }
        or marker["format"] != "research-workspace-read-only-v1"
        or marker["lifecycle"] != RecoveryLifecycle.VERIFIED_READ_ONLY.value
        or marker["writable"] is not False
        or marker["mission_auto_resume"] is not False
        or marker["canonical_effect"] != "none"
        or marker["protected_root_sha256"] != permit.plan.restored_root_sha256
        or not isinstance(marker["protected_files"], list)
        or any(not isinstance(item, str) for item in marker["protected_files"])
        or len(set(marker["protected_files"])) != len(marker["protected_files"])
    ):
        raise RecoveryError(
            "takeover_unseal_state_invalid",
            "takeover unseal marker differs from the permit-bound read-only seal",
        )
    return marker_path, marker


def _reseal_restored_after_failed_takeover(
    paths: WorkspacePaths,
    permit: TakeoverPermit,
) -> None:
    """Restore the exact physical seal after a non-committed takeover attempt."""

    try:
        paths.revalidate_physical(require_root=True, require_database=True)
        if (
            paths.root != Path(permit.plan.restored_root).resolve(strict=True)
            or paths.root_identity != permit.plan.target_root_identity
        ):
            raise RecoveryError(
                "takeover_reseal_failed",
                "failed takeover target differs from its permit",
            )
        marker_path, marker = _load_takeover_unseal_marker(paths, permit)
        protected = tuple(marker["protected_files"])
        connection = open_snapshot_connection(paths.database)
        try:
            metadata = connection.execute(
                "SELECT root_identity, current_writer_epoch FROM workspace_metadata "
                "WHERE singleton = 1"
            ).fetchone()
            high_watermark = int(
                connection.execute(
                    "SELECT COALESCE(MAX(epoch), 0) FROM writer_epoch"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        if (
            metadata is None
            or str(metadata["root_identity"]) != permit.plan.source_root_identity
            or metadata["current_writer_epoch"] is not None
            or high_watermark != (permit.plan.prior_epoch or 0)
            or _tree_digest(paths.root, protected) != permit.plan.restored_root_sha256
        ):
            raise RecoveryError(
                "takeover_reseal_failed",
                "failed takeover did not roll back to the permit-bound pre-state",
            )
        expected_files = {*protected, TAKEOVER_UNSEAL_MARKER}
        permitted_sidecars = {"workspace.sqlite3-wal", "workspace.sqlite3-shm"}
        actual_files = {
            item.relative_to(paths.root).as_posix()
            for item in paths.root.rglob("*")
            if item.is_file()
        }
        unexpected = actual_files - expected_files
        if unexpected - permitted_sidecars:
            raise RecoveryError(
                "takeover_reseal_failed",
                "failed takeover left files outside the sealed closure",
            )
        for relative in sorted(unexpected & permitted_sidecars):
            sidecar = paths.root / relative
            _make_owner_writable(sidecar)
            sidecar.unlink()
        seal_path = paths.root / _READ_ONLY_SEAL
        if seal_path.exists():
            raise RecoveryError(
                "takeover_reseal_failed",
                "read-only seal unexpectedly exists beside the unseal marker",
            )
        retain_takeover_marker = (
            permit.plan.source_incarnation is not None
            and permit.writer_creation_basis == _PORTABLE_TAKEOVER_CREATION_BASIS
            and permit.canonical_verification_kind == "installed_release_archive"
        )
        if not retain_takeover_marker:
            marker_path.replace(seal_path)
        for relative in protected:
            _make_owner_read_only(_contained(paths.root, paths.root / relative))
        _make_owner_read_only(
            marker_path if retain_takeover_marker else seal_path
        )
        for directory in sorted(
            (item for item in paths.root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            _make_owner_read_only(directory)
        _make_owner_read_only(paths.root)
        _apply_windows_write_deny(paths.root)
        _fsync_directory(paths.root)
        _verify_read_only_seal(
            paths.root,
            protected,
            permit.plan.read_only_seal_sha256,
            seal_name=(
                TAKEOVER_UNSEAL_MARKER
                if retain_takeover_marker
                else _READ_ONLY_SEAL
            ),
        )
    except BaseException:
        _force_owner_read_only_tree(paths.root)
        raise


def _complete_takeover_unseal(paths: WorkspacePaths, permit: TakeoverPermit) -> None:
    """Consume the temporary unseal marker only after takeover committed."""

    paths.revalidate_physical(require_root=True, require_database=True)
    marker_path, _marker = _load_takeover_unseal_marker(paths, permit)
    connection = open_snapshot_connection(paths.database)
    try:
        metadata = connection.execute(
            "SELECT root_identity, current_writer_epoch FROM workspace_metadata "
            "WHERE singleton = 1"
        ).fetchone()
        writer = connection.execute(
            "SELECT owner, lifecycle FROM writer_epoch WHERE epoch = ?",
            (permit.plan.proposed_epoch,),
        ).fetchone()
    finally:
        connection.close()
    if (
        metadata is None
        or str(metadata["root_identity"]) != permit.plan.target_root_identity
        or int(metadata["current_writer_epoch"]) != permit.plan.proposed_epoch
        or writer is None
        or str(writer["owner"]) != permit.actor
        or str(writer["lifecycle"]) != "active"
    ):
        _force_owner_read_only_tree(paths.root)
        raise RecoveryError(
            "takeover_completion_invalid",
            "committed takeover does not match its permit",
        )
    _make_owner_writable(marker_path)
    marker_path.unlink()
    _fsync_directory(paths.root)


def prepare_offline_migration(
    *,
    backup: BackupSet,
    target_schema_version: int,
    lifecycle: RecoveryLifecycle | str,
) -> OfflineMigrationPlan:
    if not isinstance(backup, BackupSet):
        raise RecoveryError(
            "verified_backup_required", "migration requires a verified BackupSet"
        )
    verified = _verify_backup_for_trust_transition(backup)
    if verified.manifest.manifest_sha256 != backup.manifest.manifest_sha256:
        raise RecoveryError("verified_backup_required", "BackupSet binding is stale")
    return _prepare_offline_migration_from_verified(
        backup=verified,
        target_schema_version=target_schema_version,
        lifecycle=lifecycle,
    )


def _prepare_offline_migration_from_verified(
    *,
    backup: BackupSet,
    target_schema_version: int,
    lifecycle: RecoveryLifecycle | str,
) -> OfflineMigrationPlan:
    """Derive the existing plan from a just-inspected exact source, without auditing it again."""

    verified = backup
    current_schema_version = verified.manifest.store.schema_version
    if current_schema_version == 11 or target_schema_version == 11:
        raise RecoveryError(
            "migration_history_unsupported",
            "schema11 is not a retained runtime source or migration target",
        )
    registered = load_migrations()
    registered_by_version = {item.version: item for item in registered}
    for applied in verified.manifest.store.migration_history:
        expected = registered_by_version.get(applied.version)
        if (
            expected is None
            or applied.name != expected.name
            or applied.digest_sha256 != expected.digest_sha256
        ):
            raise RecoveryError(
                "migration_history_unsupported",
                "backed-up applied migration history is not a registered prefix",
            )
    state = RecoveryLifecycle(lifecycle)
    if state not in {RecoveryLifecycle.OFFLINE, RecoveryLifecycle.QUIESCED}:
        raise RecoveryError(
            "migration_requires_offline", "migration requires exclusive pre-start state"
        )
    if target_schema_version <= current_schema_version:
        code = (
            "automatic_downgrade_forbidden"
            if target_schema_version < current_schema_version
            else "migration_not_required"
        )
        raise RecoveryError(code, "migration must advance to a newer schema")
    if target_schema_version > registered[-1].version:
        raise RecoveryError(
            "migration_target_unregistered",
            "migration target exceeds the registered migration head",
        )
    if target_schema_version == 7 and (
        current_schema_version != 6
        or verified.manifest.store.operating_mode != "mission_runtime"
        or verified.manifest.store.root_digest_version != 3
        or verified.manifest.store.current_writer_epoch is not None
        or verified.manifest.store.writer_lifecycle
        not in {
            RecoveryLifecycle.OFFLINE.value,
            RecoveryLifecycle.QUIESCED.value,
        }
    ):
        raise RecoveryError(
            "migration_history_unsupported",
            "schema 7 requires an exact quiesced schema-6 Mission/root3 source",
        )
    if target_schema_version == 8 and (
        current_schema_version != 7
        or verified.manifest.store.operating_mode != "mission_runtime"
        or verified.manifest.store.root_digest_version != 4
        or verified.manifest.store.current_writer_epoch is not None
        or verified.manifest.store.writer_lifecycle
        not in {
            RecoveryLifecycle.OFFLINE.value,
            RecoveryLifecycle.QUIESCED.value,
        }
    ):
        raise RecoveryError(
            "migration_history_unsupported",
            "schema 8 requires an exact quiesced schema-7 Mission/root4 source",
        )
    if target_schema_version == 10 and (
        current_schema_version != 9
        or verified.manifest.store.operating_mode != "mission_runtime"
        or verified.manifest.store.root_digest_version != 5
        or verified.manifest.store.current_writer_epoch is not None
        or verified.manifest.store.writer_lifecycle not in {
            RecoveryLifecycle.OFFLINE.value, RecoveryLifecycle.QUIESCED.value,
        }
    ):
        raise RecoveryError(
            "migration_history_unsupported",
            "schema10 requires an exact quiesced schema9 Mission/root5 source",
        )
    if target_schema_version == 12 and (
        current_schema_version != 10
        or verified.manifest.store.operating_mode != "mission_runtime"
        or verified.manifest.store.root_digest_version != 6
        or verified.manifest.store.current_writer_epoch is not None
        or verified.manifest.store.writer_lifecycle not in {
            RecoveryLifecycle.OFFLINE.value, RecoveryLifecycle.QUIESCED.value,
        }
    ):
        raise RecoveryError(
            "migration_history_unsupported",
            "schema12 requires an exact quiesced schema10 Mission/root6 source",
        )
    pending = tuple(
        (item.version, item.name, item.digest_sha256)
        for item in registered
        if current_schema_version < item.version <= target_schema_version
    )
    if tuple(item[0] for item in pending) != tuple(
        range(current_schema_version + 1, target_schema_version + 1)
    ):
        raise RecoveryError(
            "migration_history_unsupported", "migration path is not contiguous"
        )
    return OfflineMigrationPlan(
        current_schema_version=current_schema_version,
        target_schema_version=target_schema_version,
        verified_backup_manifest_sha256=verified.manifest.manifest_sha256,
        applied_history_sha256=verified.manifest.store.migration_history_sha256,
        pending_migrations=pending,
        lifecycle=state,
    )


def migrate_portable_backup_to_staging(
    *,
    backup_directory: Path | str,
    source_generation_id: str,
    migration_operation_id: str,
    migration_executor_release_sha: str,
    mission_id: str,
    expected_backup_id: str,
    expected_manifest_sha256: str,
    installed_release_root: Path | str,
    installed_release_sha: str,
) -> StagingPortableMigrationResult:
    """Migrate one protected off-host copy through an admitted staging pair.

    The operation root is derived from the immutable source generation, source
    release, and migration-executor release.  The process-local exception names
    only that root's exact source, migrated target, and transient rebackup stage.
    Ordinary source-bound verification remains unchanged outside this call.
    """

    operation_ids = {
        schema: derive_staging_portable_migration_operation_id(
            source_generation_id=source_generation_id,
            mission_id=mission_id,
            source_backup_id=expected_backup_id,
            source_manifest_sha256=expected_manifest_sha256,
            source_release_sha=installed_release_sha,
            migration_executor_release_sha=migration_executor_release_sha,
            source_schema_version=schema,
        )
        for schema in (9, 10)
    }
    source_schema_version = next(
        (schema for schema, identity in operation_ids.items()
         if identity == migration_operation_id), None,
    )
    if source_schema_version is None:
        raise RecoveryError(
            "portable_migration_identity_invalid",
            "staging migration operation differs from its exact source/executor binding",
        )
    migration_operation_sha256 = migration_operation_id.removeprefix(
        _STAGING_MIGRATION_OPERATION_PREFIX
    )
    target_schema_version = 10 if source_schema_version == 9 else 12
    source_root_digest_version = 5 if source_schema_version == 9 else 6
    migration_id = f"rh-staging-{migration_operation_id}-schema{target_schema_version}"
    migrated_backup_id = f"{expected_backup_id}.schema{target_schema_version}"
    supplied = Path(backup_directory).expanduser()
    if not supplied.is_absolute():
        raise RecoveryError(
            "portable_migration_path_invalid",
            "staging portable migration source path must be absolute",
        )
    _reject_production_path(supplied, label="portable migration source")
    verified, _source_report = inspect_portable_backup_set(
        supplied,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    manifest = verified.manifest
    paths = _workspace_paths_for_backup(verified.path, expected_backup_id)
    operation_root = paths.root
    if (
        verified.path.name != expected_backup_id
        or operation_root.name != migration_operation_id
        or operation_root.parent.name != "imports"
        or operation_root.parent.parent.name != _STAGING_PATH_COMPONENT
    ):
        raise RecoveryError(
            "portable_migration_path_invalid",
            "portable migration source must be the exact staging import BackupSet child",
        )
    _require_provider_free_staging_environment(
        target_root=operation_root,
        mission_id=mission_id,
    )
    _reject_staging_credential_material(operation_root)
    if (
        manifest.backup_id != expected_backup_id
        or manifest.manifest_sha256 != expected_manifest_sha256
    ):
        raise RecoveryError(
            "portable_migration_source_mismatch",
            "portable migration source differs from the exact staging request",
        )
    store = manifest.store
    if (
        store.project_id != _RH_STAGING_PROJECT_ID
        or store.schema_version != source_schema_version
        or store.root_digest_version != source_root_digest_version
        or store.operating_mode != "mission_runtime"
        or store.current_writer_epoch is not None
        or store.writer_lifecycle
        not in {RecoveryLifecycle.OFFLINE.value, RecoveryLifecycle.QUIESCED.value}
    ):
        raise RecoveryError(
            "portable_migration_source_unsupported",
            "staging portable migration source differs from its exact admitted RH pair",
        )
    source_mission = _staging_mission_binding_from_database(
        verified.path / "workspace.sqlite3",
        mission_id=mission_id,
    )
    _reject_production_path(installed_release_root, label="installed release root")
    (
        release_binding_sha256,
        release_bundle_sha256,
        release_record_sha256,
    ) = _verify_installed_release_canonical_archive(
        manifest.canonical,
        installed_release_root,
        executing_release_sha=installed_release_sha,
    )
    backups_root = paths.backups.resolve(strict=True)
    migrated_path = _contained(
        backups_root,
        backups_root / migrated_backup_id,
    )
    if migrated_path.parent != backups_root or migrated_path == verified.path:
        raise RecoveryError(
            "portable_migration_path_invalid",
            "migrated BackupSet path escaped the staging import root",
        )
    lock_path = operation_root.parent / (
        ".portable-staging-migration-"
        + _digest(
            canonical_json_bytes(
                {
                    "format": "research-portable-staging-migration-name-v1",
                    "operation_root": os.path.normcase(str(operation_root)),
                }
            )
        )[:32]
        + ".lock"
    )
    scope = {
        "source_generation_id": source_generation_id,
        "migration_operation_id": migration_operation_id,
        "migration_operation_sha256": migration_operation_sha256,
        "migration_executor_release_sha": migration_executor_release_sha,
        "mission_id": mission_id,
        "source_backup_id": expected_backup_id,
        "source_manifest_sha256": expected_manifest_sha256,
        "migration_id": migration_id,
        "migrated_backup_id": migrated_backup_id,
        "source_release_sha": installed_release_sha,
        "release_binding_sha256": release_binding_sha256,
        "release_bundle_sha256": release_bundle_sha256,
        "release_record_sha256": release_record_sha256,
    }
    try:
        with acquire_kernel_lease(
            lock_path,
            format_name="research-portable-staging-migration-lock-v2",
            scope=scope,
        ) as operation_lease:
            operation_lease.assert_held()
            locked = _revalidate_unchanged_portable_backup(verified)
            if locked != verified:
                raise RecoveryError(
                    "portable_migration_source_changed",
                    "portable migration source changed before execution",
                )
            _verify_installed_release_canonical_archive(
                manifest.canonical,
                installed_release_root,
                executing_release_sha=installed_release_sha,
            )
            verification_scope = _StagingPortableMigrationScope(
                backups_root=backups_root,
                source_generation_id=source_generation_id,
                source_backup_id=expected_backup_id,
                source_manifest_sha256=expected_manifest_sha256,
                source_root_identity=manifest.store.root_identity,
                migration_operation_id=migration_operation_id,
                migration_executor_release_sha=migration_executor_release_sha,
                migrated_backup_id=migrated_backup_id,
                source_schema_version=source_schema_version,
                _issuer=_STAGING_MIGRATION_SCOPE_ISSUER,
            )
            scope_token = _STAGING_PORTABLE_MIGRATION_SCOPE.set(verification_scope)
            try:
                plan = _prepare_offline_migration_from_verified(
                    backup=locked,
                    target_schema_version=target_schema_version,
                    lifecycle=RecoveryLifecycle(store.writer_lifecycle),
                )
                from .migration_executor import execute_offline_migration

                migration = execute_offline_migration(
                    plan=plan,
                    backup=locked,
                    migration_id=migration_id,
                    actor=stable_principal_owner_binding(attest_current_principal()),
                    observation_environment=ObservationEnvironment.STAGING,
                )
                migrated = rebackup_verified_offline_migration(
                    source_backup=locked,
                    migration_result=migration,
                    backup_id=migrated_backup_id,
                    authority_repo_root=installed_release_root,
                    installed_release_sha=installed_release_sha,
                )
            finally:
                _STAGING_PORTABLE_MIGRATION_SCOPE.reset(scope_token)
            operation_lease.assert_held()
            source_after = _revalidate_unchanged_portable_backup(verified)
            source_report = _portable_backup_report_from_verified(source_after)
            migrated_report = _portable_backup_report_from_unchanged_verified(migrated)
            target_mission = _staging_mission_binding_from_database(
                migrated.path / "workspace.sqlite3",
                mission_id=mission_id,
            )
            if (
                source_after != verified
                or source_mission != target_mission
                or migrated.manifest.store.schema_version != target_schema_version
                or migrated.manifest.store.root_digest_version != 6
                or migrated.manifest.store.root_identity != store.root_identity
                or migrated.manifest.canonical != manifest.canonical
                or migrated.manifest.closure != manifest.closure
                or migrated.manifest.evidence_inventory != manifest.evidence_inventory
                or migration.source_schema_version != source_schema_version
                or migration.target_schema_version != target_schema_version
                or migration.backup_manifest_sha256 != manifest.manifest_sha256
                or migration.selected_live_root
                or migration.mission_auto_resume
                or migration.canonical_effect != "none"
            ):
                raise RecoveryError(
                    "portable_migration_result_invalid",
                    "staging migration changed source or failed to preserve meaning",
                )
            _verify_installed_release_canonical_archive(
                manifest.canonical,
                installed_release_root,
                executing_release_sha=installed_release_sha,
            )
            _reject_staging_credential_material(operation_root)
            operation_lease.assert_held()
    except KernelLeaseError as exc:
        code = (
            "portable_migration_busy"
            if exc.code == "kernel_lease_busy"
            else "portable_migration_lock_unsafe"
        )
        raise RecoveryError(code, str(exc)) from exc
    return StagingPortableMigrationResult(
        backup=migrated,
        mission_id=mission_id,
        source_generation_id=source_generation_id,
        source_release_sha=installed_release_sha,
        migration_operation_id=migration_operation_id,
        migration_operation_sha256=migration_operation_sha256,
        migration_executor_release_sha=migration_executor_release_sha,
        source=source_report,
        source_root_digest_version=store.root_digest_version,
        migration_id=migration.migration_id,
        migration_attempt_id=migration.attempt_id,
        migration_plan_sha256=migration.plan_sha256,
        pre_logical_sha256=migration.pre_logical_sha256,
        post_logical_sha256=migration.post_logical_sha256,
        migrated=migrated_report,
        migrated_root_digest_version=migrated.manifest.store.root_digest_version,
    )
